"""
UPI + Litecoin payment bot for Discord  -  Crafted with \u2764\ufe0f by esco.md
Requires: discord.py >= 2.6 (Components V2), qrcode[pil], python-dotenv
Every command works as a prefix command AND as a slash command.

Order flow
  1. Customer:  !order  -> button -> popup (product, quantity)       (sellers can't use it)
  2. Bot pings the sellers, posts the order embed (with a Claim button) and DMs the sellers
  3. A seller presses "Claim Order" -> popup asks for the price -> customer sees seller + price
     and the bot automatically sends the seller's UPI QR for that exact amount
  4. Seller:    !done [@customer] + proof image
  5. Customer confirms -> proof message goes to the proof channel, order-complete message (no price)
     goes to the order-complete channel, customer gets the role, a DM receipt and a /vouch message
"""
import asyncio
import hashlib
import io
import json
import os
import re
import sqlite3
import time
from typing import Optional
from urllib.parse import quote

import discord
import qrcode
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from common import BOT_NAME, E, FOOTER, GREEN, M, PURPLE, RED, YELLOW, msg_embed

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "!")
DB_PATH = os.getenv("DB_PATH", "payments.db")
OWNER_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("OWNER_IDS", ""))}  # no-prefix users
DEFAULT_NOTE = "I authorize this payment and won't put any chargeback."


UPI_RE = re.compile(r"^[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z]{2,64}$")
LTC_RE = re.compile(r"^(ltc1[023456789acdefghjklmnpqrstuvwxyz]{11,87}|[LM3][a-km-zA-HJ-NP-Z1-9]{25,34})$")
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")


# ----------------------------------------------------------------------------
# Database (sqlite)
# ----------------------------------------------------------------------------
class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS profiles(
                user_id INTEGER PRIMARY KEY, payee_name TEXT, upi_id TEXT,
                pay_note TEXT, ltc_address TEXT);
            CREATE TABLE IF NOT EXISTS config(
                guild_id INTEGER PRIMARY KEY, complete_channel INTEGER, customer_role INTEGER,
                mod_users TEXT DEFAULT '[]', mod_roles TEXT DEFAULT '[]');
            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER,
                seller_id INTEGER, buyer_id INTEGER, product TEXT, quantity INTEGER, price REAL,
                created_at INTEGER, completed_at INTEGER,
                confirmed INTEGER DEFAULT 0, message_id INTEGER);
            """
        )
        # migrate databases created by older versions of this bot
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(orders)")}
        if "message_id" not in cols:
            self.conn.execute("ALTER TABLE orders ADD COLUMN message_id INTEGER")
        for col, kind in (("proof_data", "BLOB"), ("proof_name", "TEXT")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {kind}")
        ccols = {r["name"] for r in self.conn.execute("PRAGMA table_info(config)")}
        if "proof_channel" not in ccols:
            self.conn.execute("ALTER TABLE config ADD COLUMN proof_channel INTEGER")
        self.conn.execute("CREATE TABLE IF NOT EXISTS slash_cache(name TEXT PRIMARY KEY, hash TEXT)")
        self.conn.commit()

    # confirmed: 0 = open, 1 = confirmed by customer, 2 = customer said "not completed"
    def _upsert(self, table, key, key_val, fields):
        self.conn.execute(f"INSERT OR IGNORE INTO {table}({key}) VALUES(?)", (key_val,))
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE {table} SET {cols} WHERE {key}=?", (*fields.values(), key_val))
        self.conn.commit()

    def profile(self, uid):
        return self.conn.execute("SELECT * FROM profiles WHERE user_id=?", (uid,)).fetchone()

    def save_profile(self, uid, **f):
        self._upsert("profiles", "user_id", uid, f)

    def config(self, gid):
        return self.conn.execute("SELECT * FROM config WHERE guild_id=?", (gid,)).fetchone()

    def save_config(self, gid, **f):
        self._upsert("config", "guild_id", gid, f)

    def add_order(self, gid, cid, buyer, product, qty, now):
        cur = self.conn.execute(
            "INSERT INTO orders(guild_id,channel_id,buyer_id,product,quantity,created_at) VALUES(?,?,?,?,?,?)",
            (gid, cid, buyer, product, qty, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def order(self, oid):
        return self.conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()

    def set_message(self, oid, message_id):
        self.conn.execute("UPDATE orders SET message_id=? WHERE id=?", (message_id, oid))
        self.conn.commit()

    def claim_order(self, oid, seller_id, price) -> bool:
        """Atomic: only the first seller to submit a price wins."""
        cur = self.conn.execute(
            "UPDATE orders SET seller_id=?, price=? WHERE id=? AND seller_id IS NULL AND confirmed IN (0,2)",
            (seller_id, price, oid),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def unclaimed(self):
        return self.conn.execute(
            "SELECT * FROM orders WHERE seller_id IS NULL AND confirmed=0 AND message_id IS NOT NULL"
        ).fetchall()

    def latest_open(self, gid, channel_id, buyer_id, seller_id):
        """Newest not-yet-confirmed order that THIS seller claimed."""
        q, args = "SELECT * FROM orders WHERE guild_id=? AND seller_id=? AND confirmed IN (0,2)", [gid, seller_id]
        if buyer_id:
            q += " AND buyer_id=?"
            args.append(buyer_id)
        else:
            q += " AND channel_id=?"
            args.append(channel_id)
        return self.conn.execute(q + " ORDER BY id DESC LIMIT 1", args).fetchone()

    def set_status(self, oid, status):
        self.conn.execute("UPDATE orders SET confirmed=? WHERE id=?", (status, oid))
        self.conn.commit()

    def save_proof(self, oid, data, name):
        self.conn.execute("UPDATE orders SET proof_data=?, proof_name=? WHERE id=?", (data, name, oid))
        self.conn.commit()

    def slash_hash(self, name):
        row = self.conn.execute("SELECT hash FROM slash_cache WHERE name=?", (name,)).fetchone()
        return row["hash"] if row else None

    def set_slash_hash(self, name, digest):
        self.conn.execute("INSERT OR REPLACE INTO slash_cache(name, hash) VALUES(?, ?)", (name, digest))
        self.conn.commit()

    def complete(self, oid, now):
        self.conn.execute("UPDATE orders SET confirmed=1, completed_at=? WHERE id=?", (now, oid))
        self.conn.commit()


db = DB(DB_PATH)
bg_tasks: set = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    bg_tasks.add(t)
    t.add_done_callback(bg_tasks.discard)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def inr(x: Optional[float]) -> str:
    if x is None:
        return "-"
    return f"\u20b9{x:,.0f}" if float(x).is_integer() else f"\u20b9{x:,.2f}"


def make_qr(data: str, filename: str) -> discord.File:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=12, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename=filename)


def upi_uri(p, amount: Optional[float]) -> str:
    uri = f"upi://pay?pa={quote(p['upi_id'], safe='@')}&pn={quote(p['payee_name'])}&cu=INR"
    if amount:
        uri += f"&am={amount:.2f}"
    note = (p["pay_note"] or DEFAULT_NOTE)[:50]  # UPI apps allow at most 50 characters in the QR note
    uri += f"&tn={quote(note)}"
    return uri


def save_upi(uid: int, payee: str, upi: str, note: Optional[str]) -> Optional[str]:
    """Validates + saves. Returns an error string, or None on success."""
    upi = upi.strip()
    if not UPI_RE.match(upi):
        return "That doesn't look like a valid UPI ID (example: `name@bank`)."
    db.save_profile(uid, payee_name=payee.strip(), upi_id=upi, pay_note=(note or "").strip() or None)
    return None


def upi_qr_message(uid: int, amount: Optional[float] = None, description: Optional[str] = None):
    """(embed, file, view) with the saved UPI QR of user `uid`, or None if nothing is saved."""
    p = db.profile(uid)
    if not p or not p["upi_id"]:
        return None
    file = make_qr(upi_uri(p, amount), "upi_qr.png")
    e = discord.Embed(
        title=f"{E.CARD} UPI QR",
        description=description or "Scan this QR to complete payment securely.",
        color=PURPLE,
    )
    e.add_field(name="Recipient", value=p["payee_name"], inline=True)
    e.add_field(name="UPI ID", value=f"`{p['upi_id']}`", inline=True)
    e.add_field(name="Amount", value=f"INR {amount:,.2f}" if amount else "Any amount", inline=True)
    e.add_field(name="Pay Note", value=(p["pay_note"] or DEFAULT_NOTE)[:1000], inline=False)
    e.set_image(url="attachment://upi_qr.png").set_footer(text=FOOTER)
    view = discord.ui.View(timeout=None)
    view.add_item(CopyButton("upi", uid))
    return e, file, view


def sep() -> discord.ui.Separator:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


def text(s: str) -> discord.ui.TextDisplay:
    return discord.ui.TextDisplay(s)


def seller_ids(cfg):
    return (json.loads(cfg["mod_users"]), json.loads(cfg["mod_roles"])) if cfg else ([], [])


def is_seller(member: discord.Member) -> bool:
    """Member is one of the sellers chosen in /setup (user or role)."""
    users, roles = seller_ids(db.config(member.guild.id))
    return member.id in users or any(r.id in roles for r in member.roles)


def can_manage(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or member.id == member.guild.owner_id or is_seller(member)


def vouch_text(o) -> str:
    product = o["product"].replace("`", "'")
    return f"/vouch <@{o['seller_id']}> legit got {product} | Legit seller"


def vouch_embed(o, intro: str) -> discord.Embed:
    return discord.Embed(
        description=(
            f"{E.TICK_TICK} {intro}\n\n"
            f"{E.BULLET} **Vouch message** - use it in the vouch channel:\n"
            f"```\n{vouch_text(o)}\n```\n"
            "-# Slash commands can't be pasted - type `/vouch`, pick the seller, then paste the rest."
        ),
        color=GREEN,
    ).set_footer(text=FOOTER)


# ----------------------------------------------------------------------------
# Message designs
# ----------------------------------------------------------------------------
class OrderView(discord.ui.LayoutView):
    """The live order embed. Shows a Claim button until a seller claims it."""

    def __init__(self, o):
        super().__init__(timeout=None)
        self.oid = o["id"]
        claimed = o["seller_id"] is not None
        seller = f"<@{o['seller_id']}>" if claimed else "*Waiting for a seller...*"
        status = f"{E.TICK} In progress" if claimed else f"{M.DURATION} Pending"

        c = discord.ui.Container(accent_colour=discord.Colour(PURPLE))
        c.add_item(text(f"## {E.CARD} Order #{o['id']}\n-# Placed <t:{o['created_at']}:R>"))
        c.add_item(sep())
        c.add_item(text(f"{E.BULLET} **Buyer:** <@{o['buyer_id']}>\n{E.BULLET} **Seller:** {seller}"))
        c.add_item(sep())
        lines = [f"{E.BULLET} **Product:** {o['product']}", f"{E.BULLET} **Quantity:** {o['quantity']}"]
        if claimed and o["price"] is not None:
            lines.append(f"{E.MONEY} **Price:** {inr(o['price'])}")
        c.add_item(text("\n".join(lines)))
        c.add_item(sep())
        c.add_item(text(f"**Status:** {status}"))
        if not claimed:
            c.add_item(sep())
            btn = discord.ui.Button(
                label="Claim Order", emoji=E.TICK, style=discord.ButtonStyle.success, custom_id=f"claim:{o['id']}"
            )
            btn.callback = self._claim
            c.add_item(discord.ui.ActionRow(btn))
        c.add_item(sep())
        c.add_item(text(f"-# {FOOTER}"))
        self.add_item(c)

    async def _claim(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not can_manage(interaction.user):
            return await interaction.response.send_message(
                embed=msg_embed("Only sellers can claim orders.", "err"), ephemeral=True
            )
        o = db.order(self.oid)
        if not o or o["seller_id"]:
            who = f"<@{o['seller_id']}>" if o and o["seller_id"] else "another seller"
            return await interaction.response.send_message(
                embed=msg_embed(f"This order was already claimed by {who}.", "warn"), ephemeral=True
            )
        await interaction.response.send_modal(PriceModal(self.oid))  # ask the seller for the price


class PriceModal(discord.ui.Modal, title="Claim Order"):
    def __init__(self, oid: int):
        super().__init__()
        self.oid = oid
        self.price = discord.ui.TextInput(label="Price (\u20b9)", placeholder="e.g. 500", max_length=10)
        self.add_item(self.price)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            price = float(self.price.value.replace(",", "").replace("\u20b9", "").strip())
        except ValueError:
            price = 0
        if price <= 0:
            return await interaction.response.send_message(
                embed=msg_embed("Enter a valid price, for example `500`.", "err"), ephemeral=True
            )
        if not isinstance(interaction.user, discord.Member) or not can_manage(interaction.user):
            return await interaction.response.send_message(
                embed=msg_embed("Only sellers can claim orders.", "err"), ephemeral=True
            )
        if not db.claim_order(self.oid, interaction.user.id, price):
            o = db.order(self.oid)
            who = f"<@{o['seller_id']}>" if o and o["seller_id"] else "another seller"
            return await interaction.response.send_message(
                embed=msg_embed(f"This order was already claimed by {who}.", "warn"), ephemeral=True
            )
        o = db.order(self.oid)
        await interaction.response.send_message(
            content=f"<@{o['buyer_id']}>",
            embed=msg_embed(
                f"<@{o['seller_id']}> has claimed your order and will be your seller.\nPrice: **{inr(price)}**", "ok"
            ),
        )
        # automatically send the seller's UPI QR for exactly this amount
        qr = upi_qr_message(interaction.user.id, price, f"Scan this QR to pay **{inr(price)}** for your order.")
        if qr:
            embed, file, view = qr
            await interaction.followup.send(embed=embed, file=file, view=view)
        else:
            await interaction.followup.send(
                embed=msg_embed(
                    f"You haven't saved a UPI QR yet, so no QR was sent. Use `{PREFIX}set qr`, then `{PREFIX}qr {price:g}`.",
                    "warn",
                ),
                ephemeral=True,
            )

        # update the original order message: button disappears, seller + price show up
        origin = interaction.guild.get_channel_or_thread(o["channel_id"])
        if origin and o["message_id"]:
            try:
                await origin.get_partial_message(o["message_id"]).edit(view=OrderView(o))
            except discord.HTTPException:
                pass


def receipt_view(o) -> discord.ui.LayoutView:
    """Final receipt (shown in the order message + DM'd to the customer)."""
    c = discord.ui.Container(accent_colour=discord.Colour(PURPLE))
    c.add_item(text(f"## {E.CARD} Receipt #{o['id']}\n-# Completed <t:{o['completed_at']}:R>"))
    c.add_item(sep())
    c.add_item(text(f"{E.BULLET} **Seller:** <@{o['seller_id']}>\n{E.BULLET} **Buyer:** <@{o['buyer_id']}>"))
    c.add_item(sep())
    lines = [f"{E.BULLET} **Product:** {o['product']}", f"{E.BULLET} **Quantity:** {o['quantity']}"]
    if o["price"] is not None:
        lines.append(f"{E.MONEY} **Price:** {inr(o['price'])}")
    c.add_item(text("\n".join(lines)))
    c.add_item(sep())
    c.add_item(text(f"**Created At:** <t:{o['created_at']}:F>\n**Completed At:** <t:{o['completed_at']}:F>"))
    c.add_item(sep())
    c.add_item(text(f"-# {FOOTER}"))
    v = discord.ui.LayoutView(timeout=None)
    v.add_item(c)
    return v


def complete_view(guild: discord.Guild, o, seller_pfp: str, proof_name: Optional[str]) -> discord.ui.LayoutView:
    c = discord.ui.Container(accent_colour=discord.Colour(GREEN))
    c.add_item(
        discord.ui.Section(
            text(f"## {E.TICK_TICK} Order Complete!"),
            text(f"Order **#{o['id']}** has been successfully completed on **{guild.name}**."),
            accessory=discord.ui.Thumbnail(seller_pfp),  # seller's profile picture
        )
    )
    c.add_item(sep())
    lines = [
        f"{E.BULLET} **Seller:** <@{o['seller_id']}>",
        f"{E.BULLET} **Buyer:** <@{o['buyer_id']}>",
        f"{E.BULLET} **Product:** {o['product']} x {o['quantity']}",
    ]
    c.add_item(text("\n".join(lines)))
    if proof_name:
        c.add_item(sep())
        c.add_item(text(f"{E.LINK} **Proof**"))
        c.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{proof_name}")))
    c.add_item(sep())
    c.add_item(text(f"-# {FOOTER}"))
    v = discord.ui.LayoutView(timeout=None)
    v.add_item(c)
    return v


# ----------------------------------------------------------------------------
# Posting an order + notifying sellers
# ----------------------------------------------------------------------------
async def dm_sellers(guild: discord.Guild, o, jump_url: str):
    users, roles = seller_ids(db.config(guild.id))
    ids = set(users)
    for rid in roles:
        role = guild.get_role(rid)
        if role:
            ids.update(m.id for m in role.members)
    ids.discard(o["buyer_id"])

    e = discord.Embed(
        title=f"{E.CARD} New Order",
        description=f"<@{o['buyer_id']}> placed an order in **{guild.name}**.",
        color=PURPLE,
    )
    e.add_field(name="Product", value=o["product"], inline=True)
    e.add_field(name="Quantity", value=str(o["quantity"]), inline=True)
    e.set_footer(text=FOOTER)
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Go to order", emoji=E.LINK, style=discord.ButtonStyle.link, url=jump_url))

    for uid in ids:
        member = guild.get_member(uid)
        if not member or member.bot:
            continue
        try:
            await member.send(embed=e, view=view)
        except discord.HTTPException:
            pass  # DMs closed
        await asyncio.sleep(0.5)  # be gentle with rate limits


async def post_order(send, guild: discord.Guild, channel_id: int, buyer: discord.Member, product: str, qty: int):
    users, roles = seller_ids(db.config(guild.id))
    if not users and not roles:
        return await send(
            embed=msg_embed(f"No sellers are set up yet. An admin must run `{PREFIX}setup` first.", "err"),
            ephemeral=True,
        )
    pings = " ".join([f"<@&{r}>" for r in roles] + [f"<@{u}>" for u in users])
    allowed = discord.AllowedMentions(
        users=[discord.Object(u) for u in users], roles=[discord.Object(r) for r in roles]
    )
    oid = db.add_order(guild.id, channel_id, buyer.id, product[:100], qty, int(time.time()))

    # 1) normal ping for the sellers
    await send(content=f"{E.BULLET} {pings} - new order from {buyer.mention}! Claim it below.", allowed_mentions=allowed)
    # 2) then the order embed with the Claim button
    msg = await send(view=OrderView(db.order(oid)), allowed_mentions=discord.AllowedMentions.none())
    db.set_message(oid, msg.id)
    # 3) DM the sellers in the background
    spawn(dm_sellers(guild, db.order(oid), f"https://discord.com/channels/{guild.id}/{channel_id}/{msg.id}"))


# ----------------------------------------------------------------------------
# Completion: customer confirmation buttons (persistent, survive restarts)
# ----------------------------------------------------------------------------
class CopyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"copy:(?P<kind>upi|ltc):(?P<uid>[0-9]+)"):
    def __init__(self, kind: str, uid: int):
        super().__init__(
            discord.ui.Button(
                label="Copy UPI ID" if kind == "upi" else "Copy LTC Address",
                emoji=E.CARD if kind == "upi" else E.MONEY,
                style=discord.ButtonStyle.secondary,
                custom_id=f"copy:{kind}:{uid}",
            )
        )
        self.kind, self.uid = kind, uid

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["kind"], int(match["uid"]))

    async def callback(self, interaction: discord.Interaction):
        p = db.profile(self.uid)
        value = p and (p["upi_id"] if self.kind == "upi" else p["ltc_address"])
        if not value:
            return await interaction.response.send_message(embed=msg_embed("Nothing saved anymore.", "err"), ephemeral=True)
        await interaction.response.send_message(value, ephemeral=True)  # plain text = easy long-press copy


class DoneButton(discord.ui.DynamicItem[discord.ui.Button], template=r"done:(?P<action>yes|no):(?P<oid>[0-9]+)"):
    def __init__(self, action: str, oid: int):
        yes = action == "yes"
        super().__init__(
            discord.ui.Button(
                label="Confirm" if yes else "Not Completed",
                emoji=E.TICK if yes else E.CROSS,
                style=discord.ButtonStyle.success if yes else discord.ButtonStyle.danger,
                custom_id=f"done:{action}:{oid}",
            )
        )
        self.action, self.oid = action, oid

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["action"], int(match["oid"]))

    async def callback(self, interaction: discord.Interaction):
        o = db.order(self.oid)
        if not o:
            return await interaction.response.send_message(embed=msg_embed("Order not found.", "err"), ephemeral=True)
        if interaction.user.id != o["buyer_id"]:
            return await interaction.response.send_message(
                embed=msg_embed("Only the customer of this order can respond.", "err"), ephemeral=True
            )
        if o["confirmed"] != 0:
            return await interaction.response.send_message(
                embed=msg_embed("This order was already answered.", "warn"), ephemeral=True
            )

        if self.action == "no":
            db.set_status(self.oid, 2)
            await interaction.response.edit_message(
                content=None, embed=msg_embed("Marked as **not completed**. The seller has been notified.", "err"), view=None
            )
            await interaction.followup.send(
                content=f"<@{o['seller_id']}>",
                embed=msg_embed(f"<@{o['buyer_id']}> says their order is **not completed**.", "warn"),
            )
            return

        db.complete(self.oid, int(time.time()))
        o = db.order(self.oid)
        proof = (bytes(o["proof_data"]), o["proof_name"]) if o["proof_data"] else None
        if proof is None and interaction.message and interaction.message.attachments:  # orders from older versions
            try:
                att = interaction.message.attachments[0]
                proof = (await att.read(), att.filename)
            except Exception as exc:
                print(f"[proof] could not read the attachment: {exc!r}")

        await interaction.response.edit_message(
            content=None,
            embed=vouch_embed(o, "Thank you! Your order is confirmed as **completed**."),
            view=None,
        )
        problems = await finish_order(interaction.guild, o, proof)
        if problems:
            await interaction.followup.send(
                content=f"<@{o['seller_id']}>",
                embed=msg_embed(
                    "The order is confirmed, but some messages could not be sent:\n"
                    + "\n".join(f"{E.BULLET} {p}" for p in problems),
                    "warn",
                ),
            )


def done_view(oid: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(DoneButton("yes", oid))
    v.add_item(DoneButton("no", oid))
    return v


def complete_embed(guild: discord.Guild, o, seller_pfp: str, proof_name: Optional[str]) -> discord.Embed:
    """Plain-embed twin of complete_view (only used if the fancy card can't be sent)."""
    e = discord.Embed(
        title=f"{E.TICK_TICK} Order Complete!",
        description=f"Order **#{o['id']}** has been successfully completed on **{guild.name}**.",
        color=GREEN,
    )
    e.set_thumbnail(url=seller_pfp)
    e.add_field(name="Seller", value=f"<@{o['seller_id']}>", inline=True)
    e.add_field(name="Buyer", value=f"<@{o['buyer_id']}>", inline=True)
    e.add_field(name="Product", value=f"{o['product']} x {o['quantity']}", inline=False)
    if proof_name:
        e.set_image(url=f"attachment://{proof_name}")
    return e.set_footer(text=FOOTER)


def proof_view(o, name: str) -> discord.ui.LayoutView:
    c = discord.ui.Container(accent_colour=discord.Colour(GREEN))
    c.add_item(text(f"## {E.CARD} Order Proof #{o['id']}\n-# Confirmed <t:{o['completed_at']}:R>"))
    c.add_item(sep())
    c.add_item(
        text(
            f"{E.BULLET} **Seller:** <@{o['seller_id']}>\n"
            f"{E.BULLET} **Buyer:** <@{o['buyer_id']}>\n"
            f"{E.BULLET} **Product:** {o['product']} x {o['quantity']}\n"
            f"{E.TICK} **Confirmed by customer:** <t:{o['completed_at']}:F>"
        )
    )
    c.add_item(sep())
    c.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{name}")))
    c.add_item(sep())
    c.add_item(text(f"-# {FOOTER}"))
    v = discord.ui.LayoutView(timeout=None)
    v.add_item(c)
    return v


def proof_embed(o, name: str) -> discord.Embed:
    e = discord.Embed(title=f"{E.CARD} Order Proof #{o['id']}", color=GREEN)
    e.add_field(name="Seller", value=f"<@{o['seller_id']}>", inline=True)
    e.add_field(name="Buyer", value=f"<@{o['buyer_id']}>", inline=True)
    e.add_field(name="Product", value=f"{o['product']} x {o['quantity']}", inline=False)
    e.add_field(name="Confirmed by customer", value=f"<t:{o['completed_at']}:F>", inline=False)
    e.set_image(url=f"attachment://{name}")
    return e.set_footer(text=FOOTER)


async def send_with_proof(target, build_card, build_embed, proof):
    """Send a card that shows the proof image. If the fancy card fails, fall back to a plain embed with the image."""
    data, name = proof if proof else (None, None)

    async def attempt(**kwargs):
        if data:
            kwargs["file"] = discord.File(io.BytesIO(data), filename=name)
        return await target.send(allowed_mentions=discord.AllowedMentions.none(), **kwargs)

    try:
        return await attempt(view=build_card(name))
    except discord.Forbidden:
        raise  # a permissions problem: the plain embed would fail the same way
    except Exception as exc:
        print(f"[proof] card failed ({exc!r}) - sending a plain embed instead")
        return await attempt(embed=build_embed(name))


def describe_error(exc: Exception) -> str:
    if isinstance(exc, discord.Forbidden):
        return "I'm missing permissions there (I need View Channel, Send Messages, Embed Links and Attach Files)"
    return clip_(str(exc))


def delivery_problem(guild: discord.Guild) -> Optional[str]:
    """Checks that the order-complete and proof channels are set and that I can post in them."""
    cfg = db.config(guild.id)
    for label, key in (("order-complete", "complete_channel"), ("proof", "proof_channel")):
        cid = cfg[key] if cfg else None
        chan = guild.get_channel_or_thread(cid) if cid else None
        if not chan:
            return f"The **{label} channel** isn't set, so that message can't be posted. An admin must pick it in `/setup`."
        perms = chan.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links and perms.attach_files):
            return f"I can't post in {chan.mention}. I need View Channel, Send Messages, Embed Links and Attach Files there."
    return None


async def finish_order(guild: discord.Guild, o, proof) -> list:
    """Role -> customer, order-complete card + proof card -> their channels, receipt -> customer DM.

    Returns a list of problems (empty = everything worked) so the seller is told instead of failing silently.
    """
    problems = []
    cfg = db.config(guild.id)
    buyer = guild.get_member(o["buyer_id"])
    seller = guild.get_member(o["seller_id"])
    pfp = (seller or bot.user).display_avatar.url

    if cfg and cfg["customer_role"] and buyer:
        role = guild.get_role(cfg["customer_role"])
        if role:
            try:
                await buyer.add_roles(role, reason=f"Order #{o['id']} completed")
            except discord.HTTPException:
                problems.append("I couldn't give the customer role (my role must be above it and I need Manage Roles).")

    async def deliver(label, channel_id, build_card, build_embed, with_proof):
        if not channel_id:
            problems.append(f"The **{label} channel** isn't set. An admin must pick it in `/setup`.")
            return
        chan = guild.get_channel_or_thread(channel_id)
        if not chan:
            problems.append(f"The **{label} channel** no longer exists. Pick a new one in `/setup`.")
            return
        try:
            await send_with_proof(chan, build_card, build_embed, with_proof)
        except Exception as exc:
            print(f"[order] could not post the {label} message: {exc!r}")
            problems.append(f"I couldn't post the **{label}** message in {chan.mention}: {describe_error(exc)}.")

    await deliver(
        "order-complete", cfg["complete_channel"] if cfg else None,
        lambda n: complete_view(guild, o, pfp, n), lambda n: complete_embed(guild, o, pfp, n), proof,
    )
    if proof:
        await deliver(
            "proof", cfg["proof_channel"] if cfg else None,
            lambda n: proof_view(o, n), lambda n: proof_embed(o, n), proof,
        )
    else:
        problems.append("No proof image was saved for this order, so no proof message was posted.")

    # turn the original order message into the final receipt
    if o["message_id"]:
        origin = guild.get_channel_or_thread(o["channel_id"])
        if origin:
            try:
                await origin.get_partial_message(o["message_id"]).edit(view=receipt_view(o))
            except discord.HTTPException:
                pass

    if buyer:
        try:
            await send_with_proof(buyer, lambda n: complete_view(guild, o, pfp, n), lambda n: complete_embed(guild, o, pfp, n), proof)
            await buyer.send(view=receipt_view(o))
            await buyer.send(embed=vouch_embed(o, f"Your order on **{guild.name}** is complete. Thank you!"))
        except discord.HTTPException:
            pass  # the customer's DMs are closed
    return problems


# ----------------------------------------------------------------------------
# Set UPI QR form
# ----------------------------------------------------------------------------
class UPIModal(discord.ui.Modal, title="Set UPI QR"):
    def __init__(self, existing=None):
        super().__init__()
        g = lambda k: existing[k] if existing and existing[k] else None  # noqa: E731
        self.payee = discord.ui.TextInput(label="Payee Name", placeholder="e.g. Oewe", default=g("payee_name"), max_length=60)
        self.upi = discord.ui.TextInput(label="UPI ID", placeholder="name@bank", default=g("upi_id"), max_length=80)
        self.note = discord.ui.TextInput(
            label="Pay Note (optional, any language)", placeholder=DEFAULT_NOTE,
            default=g("pay_note"), required=False, max_length=200, style=discord.TextStyle.paragraph,
        )
        for item in (self.payee, self.upi, self.note):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        err = save_upi(interaction.user.id, self.payee.value, self.upi.value, self.note.value)
        if err:
            return await interaction.response.send_message(embed=msg_embed(err, "err"), ephemeral=True)
        await interaction.response.send_message(
            embed=msg_embed(f"UPI QR saved. Use `{PREFIX}qr` to display it.", "ok"), ephemeral=True
        )


def interaction_sender(interaction: discord.Interaction):
    """ctx.send-like helper for modal submits: first call responds, later calls follow up."""

    async def send(**kwargs):
        if not interaction.response.is_done():
            await interaction.response.send_message(**kwargs)
            return await interaction.original_response()
        return await interaction.followup.send(wait=True, **kwargs)

    return send


class OrderModal(discord.ui.Modal, title="Place Order"):
    def __init__(self):
        super().__init__()
        self.product = discord.ui.TextInput(label="Product", placeholder="What do you want to order?", max_length=100)
        self.quantity = discord.ui.TextInput(label="Quantity", placeholder="e.g. 1", default="1", max_length=4)
        for item in (self.product, self.quantity):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        if isinstance(interaction.user, discord.Member) and is_seller(interaction.user):
            return await interaction.response.send_message(
                embed=msg_embed("Sellers can't place orders - this is for customers.", "err"), ephemeral=True
            )
        try:
            qty = int(self.quantity.value.strip())
        except ValueError:
            qty = 0
        if not 1 <= qty <= 1000:
            return await interaction.response.send_message(
                embed=msg_embed("Quantity must be a number between 1 and 1000.", "err"), ephemeral=True
            )
        await post_order(
            interaction_sender(interaction), interaction.guild, interaction.channel_id,
            interaction.user, self.product.value.strip(), qty,
        )


class ModalLauncher(discord.ui.View):
    """Prefix-command fallback: a button that opens a form (only for the invoker)."""

    def __init__(self, author_id: int, label: str, factory):
        super().__init__(timeout=120)
        self.author_id, self.factory = author_id, factory
        btn = discord.ui.Button(label=label, emoji=E.CARD, style=discord.ButtonStyle.primary)
        btn.callback = self._open
        self.add_item(btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(embed=msg_embed("This isn't your panel.", "err"), ephemeral=True)
            return False
        return True

    async def _open(self, interaction: discord.Interaction):
        await interaction.response.send_modal(self.factory())


# ----------------------------------------------------------------------------
# /setup panel
# ----------------------------------------------------------------------------
def setup_embed(guild: discord.Guild) -> discord.Embed:
    cfg = db.config(guild.id)
    chan = f"<#{cfg['complete_channel']}>" if cfg and cfg["complete_channel"] else "`Not set`"
    proof_chan = f"<#{cfg['proof_channel']}>" if cfg and cfg["proof_channel"] else "`Not set`"
    role = f"<@&{cfg['customer_role']}>" if cfg and cfg["customer_role"] else "`Not set`"
    users, roles = seller_ids(cfg)
    sellers = " ".join([f"<@{u}>" for u in users] + [f"<@&{r}>" for r in roles]) or "`Not set`"
    e = discord.Embed(
        title=f"{E.CARD} {BOT_NAME} Setup",
        description="Use the menus below to configure the bot for this server.",
        color=PURPLE,
    )
    e.add_field(name=f"{E.LINK} Order Complete Channel", value=chan, inline=False)
    e.add_field(name=f"{E.CARD} Proof Channel (proof message after customer confirms)", value=proof_chan, inline=False)
    e.add_field(name=f"{E.BULLET} Sellers (pinged + DM'd on orders, can claim and use `{PREFIX}done`)", value=sellers, inline=False)
    e.add_field(name=f"{E.MONEY} Customer Role (given after order complete)", value=role, inline=False)
    return e.set_footer(text=FOOTER)


class SetupView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=300)
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(embed=msg_embed("This isn't your panel.", "err"), ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction):
        await interaction.response.edit_message(embed=setup_embed(interaction.guild), view=self)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="Order complete channel",
        row=0,
    )
    async def pick_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        db.save_config(interaction.guild_id, complete_channel=select.values[0].id)
        await self._refresh(interaction)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="Proof channel",
        row=1,
    )
    async def pick_proof_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        db.save_config(interaction.guild_id, proof_channel=select.values[0].id)
        await self._refresh(interaction)

    @discord.ui.select(
        cls=discord.ui.MentionableSelect,
        placeholder="Sellers (users or roles)",
        min_values=1,
        max_values=10,
        row=2,
    )
    async def pick_sellers(self, interaction: discord.Interaction, select: discord.ui.MentionableSelect):
        roles = [v.id for v in select.values if isinstance(v, discord.Role)]
        users = [v.id for v in select.values if not isinstance(v, discord.Role)]
        db.save_config(interaction.guild_id, mod_users=json.dumps(users), mod_roles=json.dumps(roles))
        await self._refresh(interaction)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Role given to customer after order complete", row=3)
    async def pick_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        db.save_config(interaction.guild_id, customer_role=select.values[0].id)
        await self._refresh(interaction)


# ----------------------------------------------------------------------------
# Bot
# ----------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True


def get_prefix(bot_, message: discord.Message):
    prefixes = [PREFIX]
    if message.author.id in OWNER_IDS:
        prefixes.append("")  # no-prefix for owners (must stay LAST so "!qr" still matches "!")
    return commands.when_mentioned_or(*prefixes)(bot_, message)


class PayBot(commands.Bot):
    async def setup_hook(self):
        self.add_dynamic_items(CopyButton, DoneButton)
        for o in db.unclaimed():  # keep Claim buttons alive after a restart
            self.add_view(OrderView(o), message_id=o["message_id"])
        try:
            await self.load_extension("music")  # Lavalink music system (music.py)
        except Exception as exc:  # e.g. wavelink not installed -> payments keep working
            print(f"[music] music system disabled: {exc!r}")
        # slash commands are registered in the background after login (see on_ready) - nothing is ever deleted

    def _trim_descriptions(self):
        """Discord rejects a command whose description is longer than 100 characters."""

        def cut(value):
            return value if not isinstance(value, str) or len(value) <= 100 else value[:99] + "..."

        for cmd in self.tree.walk_commands():
            cmd.description = cut(cmd.description)
            for param in getattr(cmd, "parameters", []):
                param.description = cut(param.description)

    async def register_slash_commands(self, force: bool = False):
        """Add / update every slash command. This NEVER deletes anything.

        Each command is uploaded on its own, so one bad command can't block the others, and commands that
        belong to other code using the same application are left alone. Unchanged commands are skipped.
        """
        self._trim_descriptions()
        try:
            remote = {c.name for c in await self.tree.fetch_commands()}
        except discord.DiscordException as exc:
            print(f"[slash] could not read the current commands (continuing anyway): {exc}")
            remote = set()
        ready, failed = [], {}
        for cmd in self.tree.get_commands():
            try:
                payload = cmd.to_dict(self.tree)
                digest = hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
                if force or cmd.name not in remote or db.slash_hash(cmd.name) != digest:
                    await self.http.upsert_global_command(self.application_id, payload)
                    db.set_slash_hash(cmd.name, digest)
                ready.append(cmd.name)
            except Exception as exc:  # keep going with the next command
                failed[cmd.name] = str(exc)
                print(f"[slash] /{cmd.name} could not be registered: {exc}")
        print(
            f"[slash] {len(ready)} slash commands ready"
            + (f" | {len(failed)} failed: {', '.join(failed)}" if failed else "")
        )
        return ready, failed


bot = PayBot(command_prefix=get_prefix, intents=intents, help_command=None)


def is_bot_user():
    """Admins/owner or sellers chosen in /setup."""

    async def pred(ctx: commands.Context):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if not can_manage(ctx.author):
            raise commands.CheckFailure("Only sellers set in `/setup` can use this command.")
        return True

    return commands.check(pred)


@bot.event
async def on_ready():
    if bot.user.name != BOT_NAME:
        try:
            await bot.user.edit(username=BOT_NAME)
        except discord.HTTPException as exc:  # Discord limits username changes (about 2 per hour)
            print(f"Could not rename the bot to {BOT_NAME}: {exc}")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name=f"orders | {PREFIX}help")
    )
    if not getattr(bot, "slash_started", False):  # on_ready can fire again after reconnects
        bot.slash_started = True
        spawn(bot.register_slash_commands())
    print(f"Logged in as {bot.user} ({bot.user.id}) | owners without prefix: {len(OWNER_IDS)}")


# ---- sync (bot owners only) ------------------------------------------------
@bot.command(name="sync")
async def sync_cmd(ctx, mode: str = ""):
    """Bot owners only: add/update the slash commands. Never deletes anything. `sync force` re-uploads all of them."""
    if ctx.author.id not in OWNER_IDS and not await bot.is_owner(ctx.author):
        return
    async with ctx.typing():
        ready, failed = await bot.register_slash_commands(force=mode.lower() == "force")
    text_ = f"**{len(ready)}** slash commands are registered. Nothing was deleted."
    if failed:
        text_ += "\n\nThese failed:\n" + "\n".join(f"`/{n}` - {clip_(r)}" for n, r in failed.items())
    await ctx.send(embed=msg_embed(text_, "warn" if failed else "ok"))


def clip_(s: str, n: int = 150) -> str:
    return s if len(s) <= n else s[: n - 1] + "..."


# ---- help ------------------------------------------------------------------
@bot.hybrid_command(name="help", description="Show all commands")
async def help_cmd(ctx):
    e = discord.Embed(title=f"{E.CARD} {BOT_NAME} - Command Menu", color=PURPLE)
    e.description = (
        f"**Customers**\n"
        f"{E.BULLET} `{PREFIX}order` - place an order (a popup asks for product + quantity)\n\n"
        f"**Sellers**\n"
        f"{E.BULLET} `Claim Order` button - take an order and set its price\n"
        f"{E.BULLET} `{PREFIX}done [@customer]` + proof image - finish your order\n"
        f"{E.BULLET} `{PREFIX}qr [amount]` - show your saved UPI QR\n"
        f"{E.BULLET} `{PREFIX}ltc [amount]` - show your saved LTC address + QR\n"
        f"{E.BULLET} `{PREFIX}set qr` / `{PREFIX}set ltc <address>` - save your payment details\n\n"
        f"**Music**\n"
        f"{E.BULLET} `{PREFIX}play` (`p`) | `join` | `leave` | `skip` (`s`) | `previous` | `pause` | `resume` | `stop`\n"
        f"{E.BULLET} `{PREFIX}loop` | `autoplay` | `shuffle` | `clear` | `remove` | `skipto` | `seek` | `replay`\n"
        f"{E.BULLET} `{PREFIX}volume` | `filter` | `queue` (`q`) | `nowplaying` (`np`) | `favorites`\n\n"
        f"**Admins**\n"
        f"{E.BULLET} `{PREFIX}setup` - channels, sellers and customer role\n\n"
        f"{E.LINK} Every command also works as a slash command. Add the app to your account to use `/qr` and `/ltc` in any DM."
    )
    await ctx.send(embed=e.set_footer(text=FOOTER))


# ---- qr / ltc --------------------------------------------------------------
@bot.hybrid_command(name="qr", description="Show your saved UPI QR")
@app_commands.describe(amount="Optional amount in INR")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def qr_cmd(ctx, amount: Optional[float] = None):
    msg = upi_qr_message(ctx.author.id, amount)
    if not msg:
        return await ctx.send(embed=msg_embed(f"No UPI QR saved. Use `{PREFIX}set qr` first.", "err"), ephemeral=True)
    embed, file, view = msg
    await ctx.send(embed=embed, file=file, view=view)


@bot.hybrid_command(name="ltc", description="Show your saved Litecoin address with QR")
@app_commands.describe(amount="Optional amount in LTC")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ltc_cmd(ctx, amount: Optional[float] = None):
    p = db.profile(ctx.author.id)
    if not p or not p["ltc_address"]:
        return await ctx.send(
            embed=msg_embed(f"No LTC address saved. Use `{PREFIX}set ltc <address>` first.", "err"), ephemeral=True
        )
    uri = f"litecoin:{p['ltc_address']}" + (f"?amount={amount:g}" if amount else "")
    file = make_qr(uri, "ltc_qr.png")
    e = discord.Embed(
        title=f"{E.MONEY} Litecoin (LTC)", description="Scan this QR or send LTC to the address below.", color=PURPLE
    )
    e.add_field(name="Network", value="Litecoin", inline=True)
    e.add_field(name="Amount", value=f"{amount:g} LTC" if amount else "Any amount", inline=True)
    e.add_field(name="Address", value=f"`{p['ltc_address']}`", inline=False)
    e.set_image(url="attachment://ltc_qr.png").set_footer(text=FOOTER)
    view = discord.ui.View(timeout=None)
    view.add_item(CopyButton("ltc", ctx.author.id))
    await ctx.send(embed=e, file=file, view=view)


# ---- set qr / set ltc ------------------------------------------------------
@bot.hybrid_group(name="set", description="Save your payment details", invoke_without_command=True)
async def set_group(ctx):
    await ctx.send(
        embed=msg_embed(f"Usage:\n{E.BULLET} `{PREFIX}set qr`\n{E.BULLET} `{PREFIX}set ltc <address>`", "warn"),
        ephemeral=True,
    )


@set_group.command(name="qr", description="Save your UPI QR (payee name, UPI ID, optional pay note)")
@app_commands.describe(payee_name="Name shown to the payer", upi_id="Your UPI ID, e.g. name@bank", pay_note="Optional pay note")
async def set_qr(ctx, payee_name: Optional[str] = None, upi_id: Optional[str] = None, *, pay_note: Optional[str] = None):
    if not payee_name or not upi_id:  # missing details -> open the form
        factory = lambda: UPIModal(db.profile(ctx.author.id))  # noqa: E731
        if ctx.interaction:
            return await ctx.interaction.response.send_modal(factory())
        e = discord.Embed(
            title=f"{E.CARD} Set UPI QR",
            description=(
                f"{E.BULLET} **Payee Name** *(required)*\n"
                f"{E.BULLET} **UPI ID** *(required)*\n"
                f"{E.BULLET} **Pay Note** *(optional)*\n\nPress the button below to fill these in."
            ),
            color=PURPLE,
        ).set_footer(text=FOOTER)
        return await ctx.send(embed=e, view=ModalLauncher(ctx.author.id, "Enter UPI details", factory))
    err = save_upi(ctx.author.id, payee_name, upi_id, pay_note)
    if err:
        return await ctx.send(embed=msg_embed(err, "err"), ephemeral=True)
    await ctx.send(embed=msg_embed(f"UPI QR saved. Use `{PREFIX}qr` to display it.", "ok"), ephemeral=True)


@set_group.command(name="ltc", description="Save your Litecoin address")
@app_commands.describe(address="Your Litecoin address")
async def set_ltc(ctx, address: str):
    if not LTC_RE.match(address):
        return await ctx.send(embed=msg_embed("That doesn't look like a valid Litecoin address.", "err"), ephemeral=True)
    db.save_profile(ctx.author.id, ltc_address=address)
    await ctx.send(embed=msg_embed(f"LTC address saved. Use `{PREFIX}ltc` to display it.", "ok"), ephemeral=True)


# ---- order (customers only) ------------------------------------------------
@bot.hybrid_command(name="order", description="Place an order - the sellers will be pinged")
@commands.guild_only()
async def order_cmd(ctx):
    if is_seller(ctx.author):
        return await ctx.send(embed=msg_embed("Sellers can't place orders - this command is for customers.", "err"), ephemeral=True)
    users, roles = seller_ids(db.config(ctx.guild.id))
    if not users and not roles:
        return await ctx.send(
            embed=msg_embed(f"No sellers are set up yet. An admin must run `{PREFIX}setup` first.", "err"), ephemeral=True
        )
    if ctx.interaction:  # slash command: open the popup straight away
        return await ctx.interaction.response.send_modal(OrderModal())
    e = discord.Embed(
        title=f"{E.CARD} Place Order",
        description=f"{E.BULLET} Press the button below and enter the **product** and **quantity**.",
        color=PURPLE,
    ).set_footer(text=FOOTER)
    await ctx.send(embed=e, view=ModalLauncher(ctx.author.id, "Place Order", OrderModal))


# ---- done (sellers) --------------------------------------------------------
@bot.hybrid_command(name="done", description="Mark your claimed order done and upload proof", usage="[@customer] + proof image")
@app_commands.describe(
    proof="Proof image (screenshot)",
    customer="Customer (default: your latest claimed order in this channel)",
)
@is_bot_user()
async def done_cmd(ctx, proof: discord.Attachment, customer: Optional[discord.Member] = None):
    if not (proof.content_type or "").startswith("image/"):
        return await ctx.send(embed=msg_embed("The proof must be an **image** (screenshot).", "err"), ephemeral=True)
    o = db.latest_open(ctx.guild.id, ctx.channel.id, customer.id if customer else None, ctx.author.id)
    if not o:
        return await ctx.send(
            embed=msg_embed("You have no claimed open order here. Press **Claim Order** on the order first.", "err"),
            ephemeral=True,
        )

    await ctx.defer()  # downloading the proof can take a moment
    ext = os.path.splitext(proof.filename)[1].lower()
    ext = ext if ext in IMG_EXT else ".png"
    fname = f"proof{ext}"
    data = await proof.read()
    file = discord.File(io.BytesIO(data), filename=fname)
    db.save_proof(o["id"], data, fname)
    db.set_status(o["id"], 0)
    o = db.order(o["id"])

    e = discord.Embed(
        title=f"{E.CARD} Order Confirmation",
        description=(
            f"<@{o['buyer_id']}>, has your order been **completed** by <@{o['seller_id']}>?\n"
            "Check the proof below and confirm using the buttons."
        ),
        color=PURPLE,
    )
    e.add_field(name="Product", value=o["product"], inline=True)
    e.add_field(name="Quantity", value=str(o["quantity"]), inline=True)
    if o["price"] is not None:
        e.add_field(name="Price", value=inr(o["price"]), inline=True)
    e.set_image(url=f"attachment://proof{ext}").set_footer(text=FOOTER)
    await ctx.send(content=f"<@{o['buyer_id']}>", embed=e, file=file, view=done_view(o["id"]))
    warning = delivery_problem(ctx.guild)  # tell the seller NOW if the proof/complete messages can't be posted later
    if warning:
        await ctx.send(embed=msg_embed(warning, "warn"), ephemeral=True)



# ---- setup -----------------------------------------------------------------
@bot.hybrid_command(name="setup", description="Set channels, sellers and customer role")
@app_commands.default_permissions(manage_guild=True)
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def setup_cmd(ctx):
    await ctx.send(embed=setup_embed(ctx.guild), view=SetupView(ctx.author.id))


# ---- errors ----------------------------------------------------------------
EXAMPLES = {
    "done": f"Example: `{PREFIX}done @customer` with a screenshot attached",
}


@bot.event
async def on_command_error(ctx, error):
    error = getattr(error, "original", error)
    if isinstance(error, commands.CommandNotFound):
        return
    usage = f"`{PREFIX}{ctx.command.qualified_name} {ctx.command.usage or ctx.command.signature}`" if ctx.command else ""
    example = EXAMPLES.get(ctx.command.name, "") if ctx.command else ""
    if isinstance(error, commands.MissingRequiredAttachment):
        text_, kind = "Attach a **proof image** to your message (slash command: use the `proof` option).", "warn"
    elif isinstance(error, commands.MissingRequiredArgument):
        text_, kind = f"Missing `{error.param.name}`.\nUsage: {usage}\n{example}", "warn"
    elif isinstance(error, (commands.BadArgument, commands.BadUnionArgument)):
        text_, kind = f"Invalid input.\nUsage: {usage}\n{example}", "warn"
    elif isinstance(error, commands.CheckFailure):
        text_, kind = str(error) or "You can't use this command.", "err"
    else:
        print(f"Unhandled error in {ctx.command}: {error!r}")
        text_, kind = "Something went wrong. Please try again.", "err"
    await ctx.send(embed=msg_embed(text_.strip(), kind), ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file.")
    bot.run(TOKEN)
