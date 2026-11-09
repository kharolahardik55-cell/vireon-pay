"""
Vireon Pay music system  -  Crafted with \u2764\ufe0f by esco.md
Lavalink v4 through Wavelink 3.x. Loaded automatically by bot.py (load_extension("music")).

Control panel (one message per server, always the newest message in the channel):
  big song artwork | title link | author | progress bar | volume / loop / autoplay / filter
  next song (queue or suggestion) | buttons: previous, pause/resume, skip, loop, favorite
  menus: Queue | "Select a suggested song" | "More Features..."
"""
import asyncio
import os
import sqlite3
from typing import Literal, Optional

import discord
import wavelink
from discord import app_commands
from discord.ext import commands

from common import E, FOOTER, M, msg_embed

MUSIC_BLUE = 0x3B82F6
FILTER_ICON = "\U0001f39a\ufe0f"

# Lavalink connection: only the URL and the password come from .env
LAVALINK_URL = os.getenv("LAVALINK_URL", "127.0.0.1:2333")
LAVALINK_PASS = os.getenv("LAVALINK_PASS", "youshallnotpass")
NODE_NAME = "Vireon Pay"  # name
SECURE = False  # secure: false

DEFAULT_SOURCE = "ytsearch"
DEFAULT_VOLUME = 80
# start a search with one of these words to pick the source, e.g.  /play spotify blinding lights
SOURCES = {
    "spotify": "spsearch", "sp": "spsearch",
    "youtube": "ytsearch", "yt": "ytsearch",
    "ytmusic": "ytmsearch", "ytm": "ytmsearch",
    "soundcloud": "scsearch", "sc": "scsearch",
}

LOOP_NAMES = {wavelink.QueueMode.normal: "Off", wavelink.QueueMode.loop: "Track", wavelink.QueueMode.loop_all: "Queue"}


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def text(s: str) -> discord.ui.TextDisplay:
    return discord.ui.TextDisplay(s)


def sep() -> discord.ui.Separator:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


def parse_time(value: str) -> Optional[int]:
    """'90' -> 90 s, '1:30' -> 90 s, '1:02:03' -> 1 h 2 min 3 s.  Returns milliseconds (or None)."""
    try:
        parts = [int(x) for x in value.strip().split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3 or any(x < 0 for x in parts):
        return None
    seconds = 0
    for x in parts:
        seconds = seconds * 60 + x
    return seconds * 1000


def vibe_embed(message: str) -> discord.Embed:
    return discord.Embed(description=f"{M.SONG_VIBING} {message}", color=MUSIC_BLUE).set_footer(text=FOOTER)


def split_source(query: str):
    """'spotify blinding lights' -> ('spsearch', 'blinding lights'). Links are passed through untouched."""
    head, _, rest = query.strip().partition(" ")
    key = head.lower().rstrip(":")
    if rest.strip() and key in SOURCES:
        return SOURCES[key], rest.strip()
    return DEFAULT_SOURCE, query.strip()


def clip(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "..."


def fmt_time(ms: int) -> str:
    s = int(ms // 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def bar(pos: int, length: int, size: int = 14) -> str:
    if not length:
        return "\u25ac" * size
    idx = min(int(pos / length * size), size - 1)
    return "\u25ac" * idx + "\U0001f518" + "\u25ac" * (size - idx - 1)


def link(track) -> str:
    title = clip(track.title, 70).replace("[", "(").replace("]", ")")
    return f"[{title}]({track.uri})" if track.uri else title


def artwork(track) -> Optional[str]:
    if track.artwork:
        return track.artwork
    if track.source in ("youtube", "youtubemusic") and track.identifier:
        return f"https://i.ytimg.com/vi/{track.identifier}/hqdefault.jpg"
    return None


class Favorites:
    """Songs saved with the heart button (stored in the same sqlite file as the payments)."""

    def __init__(self):
        self.conn = sqlite3.connect(os.getenv("DB_PATH", "payments.db"))
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS favorites("
            "user_id INTEGER, uri TEXT, title TEXT, author TEXT, PRIMARY KEY(user_id, uri))"
        )
        self.conn.commit()

    def toggle(self, uid: int, track) -> bool:
        """True = saved, False = removed."""
        uri = track.uri or track.identifier
        if self.conn.execute("SELECT 1 FROM favorites WHERE user_id=? AND uri=?", (uid, uri)).fetchone():
            self.conn.execute("DELETE FROM favorites WHERE user_id=? AND uri=?", (uid, uri))
            self.conn.commit()
            return False
        self.conn.execute("INSERT INTO favorites VALUES(?,?,?,?)", (uid, uri, track.title, track.author))
        self.conn.commit()
        return True

    def count(self, track) -> int:
        """How many people saved this song with the heart button."""
        uri = track.uri or track.identifier
        return self.conn.execute("SELECT COUNT(*) FROM favorites WHERE uri=?", (uri,)).fetchone()[0]

    def all(self, uid: int):
        return self.conn.execute(
            "SELECT uri, title, author FROM favorites WHERE user_id=? ORDER BY rowid DESC LIMIT 25", (uid,)
        ).fetchall()


# ----------------------------------------------------------------------------
# The control panel
# ----------------------------------------------------------------------------
class PanelView(discord.ui.LayoutView):
    """Music control panel: big artwork, one fact per line, plain grey buttons (4 per row, 8 in total)."""

    def __init__(self, cog: "Music", player: wavelink.Player):
        super().__init__(timeout=None)
        self.cog, self.player = cog, player
        self.rebuild()

    # --- layout ------------------------------------------------------------
    def rebuild(self):
        """Re-draw the panel from the player's current state (same view object, so buttons stay valid)."""
        self.clear_items()
        p = self.player
        track = p.current
        c = discord.ui.Container(accent_colour=discord.Colour(MUSIC_BLUE))
        c.add_item(text(f"## {M.NOW_PLAYING} Now Playing {M.SONG_VIBING}"))
        if track is None:
            c.add_item(text("Nothing is playing right now."))
            self.add_item(c)
            return

        queue_items = list(p.queue)
        autoplay = p.autoplay is wavelink.AutoPlayMode.enabled
        requester = getattr(track.extras, "requester", None)

        # 1) the song: big image, name, author, time
        art = artwork(track)
        if art:
            c.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(art)))
        c.add_item(text(f"### {M.SONG_NAME} {link(track)}\n{M.AUTHOR} {clip(track.author, 60)}"))
        if track.is_stream:
            c.add_item(text("\U0001f534 **LIVE**"))
        else:
            c.add_item(text(f"{M.TIMER} `{fmt_time(p.position)}` {bar(p.position, track.length)} `{fmt_time(track.length)}`"))
        c.add_item(sep())

        # 2) one fact per line
        lines = [
            f"{M.PAUSE} **Status** - Paused" if p.paused else f"{M.PLAY} **Status** - Playing",
            f"{M.VOLUME} **Volume** - {p.volume}%",
            f"{M.LOOP} **Loop** - {LOOP_NAMES.get(p.queue.mode, 'Off')}",
            f"{M.LIKE} **Likes** - {self.cog.favs.count(track)}",
            f"{M.REQUESTED_BY} **Requested by** - {f'<@{requester}>' if requester else 'Autoplay'}",
            f"\U0001f3a7 **Voice channel** - {p.channel.mention}",
            f"{M.AUTOPLAY} **Autoplay** - {'On' if autoplay else 'Off'}",
        ]
        if p.filter_name != "Off":
            lines.append(f"{M.ROUND_BOOST if p.filter_name == 'Bass boost' else FILTER_ICON} **Filter** - {p.filter_name}")
        c.add_item(text("\n".join(lines)))
        c.add_item(sep())

        # 3) next song (queue first, otherwise the first suggestion)
        if queue_items:
            nxt, label = queue_items[0], "Up next"
        elif p.suggestions:
            nxt, label = p.suggestions[0], "Suggested next"
        else:
            nxt, label = None, "Up next"
        if nxt:
            c.add_item(text(f"{M.NEXT} **{label}** - {link(nxt)}\n{M.AUTHOR} {clip(nxt.author, 40)}"))
        else:
            c.add_item(text(f"{M.NEXT} **Up next** - *{'nothing queued yet' if p.suggestions_ready else 'loading suggestions...'}*"))
        c.add_item(sep())

        # 4) buttons: all plain grey, 4 per row, 8 in total
        def button(emoji, cid, cb, label=None):
            b = discord.ui.Button(emoji=emoji, label=label, style=discord.ButtonStyle.secondary, custom_id=cid)
            b.callback = cb
            return b

        c.add_item(
            discord.ui.ActionRow(
                button(M.PREVIOUS, "mp:prev", self.on_prev),
                button(M.PLAY if p.paused else M.PAUSE, "mp:pause", self.on_pause),
                button(M.NEXT, "mp:skip", self.on_skip),
                button(M.LOOP, "mp:loop", self.on_loop),
            )
        )
        c.add_item(
            discord.ui.ActionRow(
                button(M.SHUFFLE, "mp:shuffle", self.on_shuffle),
                button(M.VOLUME, "mp:vol_down", self.on_vol_down, "-"),
                button(M.VOLUME, "mp:vol_up", self.on_vol_up, "+"),
                button(M.LIKE, "mp:fav", self.on_fav),
            )
        )

        # 5) menus: queue, suggestions, more
        q_opts = [
            discord.SelectOption(label=clip(f"{i}. {t.title}", 100), description=clip(t.author, 100) or None, value=str(i - 1), emoji=M.SONG_NAME)
            for i, t in enumerate(queue_items[:25], 1)
        ]
        q_sel = discord.ui.Select(
            custom_id="mp:queue",
            placeholder=f"Queue | {len(queue_items)} track(s) - pick one to play now" if queue_items else "No tracks",
            options=q_opts or [discord.SelectOption(label="No tracks", value="none")],
            disabled=not queue_items,
        )
        q_sel.callback = self.on_queue
        c.add_item(discord.ui.ActionRow(q_sel))

        s_opts = [
            discord.SelectOption(label=clip(t.title, 100), description=clip(t.author, 100) or None, value=str(i), emoji=M.SONG_NAME)
            for i, t in enumerate(p.suggestions[:10])
        ]
        s_sel = discord.ui.Select(
            custom_id="mp:suggest",
            placeholder="Select a suggested song",
            options=s_opts or [discord.SelectOption(label="Loading suggestions...", value="none")],
            disabled=not s_opts,
        )
        s_sel.callback = self.on_suggest
        c.add_item(discord.ui.ActionRow(s_sel))

        more = [
            ("Seek +10s", M.NEXT, "seek_fwd"),
            ("Seek -10s", M.PREVIOUS, "seek_back"),
            ("Toggle autoplay", M.AUTOPLAY, "autoplay"),
            ("Bass boost", M.ROUND_BOOST, "bass"),
            ("Nightcore", "\U0001f319", "nightcore"),
            ("Reset filters", "\u267b\ufe0f", "reset"),
            ("Clear queue", M.CLEAR_QUEUE, "clear"),
            ("Stop & leave", E.CROSS, "stop"),
        ]
        m_sel = discord.ui.Select(
            custom_id="mp:more",
            placeholder="More Features...",
            options=[discord.SelectOption(label=l, emoji=e, value=v) for l, e, v in more],
        )
        m_sel.callback = self.on_more
        c.add_item(discord.ui.ActionRow(m_sel))

        c.add_item(text(f"-# {FOOTER}"))
        self.add_item(c)

    async def redraw(self, interaction: discord.Interaction):
        self.rebuild()
        await interaction.response.edit_message(view=self)

    # --- guards ------------------------------------------------------------
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        user, vc = interaction.user, self.player.channel
        if not isinstance(user, discord.Member) or not user.voice or user.voice.channel != vc:
            await interaction.response.send_message(
                embed=msg_embed(f"Join {vc.mention} to use the controls.", "err"), ephemeral=True
            )
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        print(f"[music] control panel error: {error!r}")
        embed = msg_embed("That control failed - is the Lavalink server still running?", "err")
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- buttons -----------------------------------------------------------
    async def on_prev(self, interaction: discord.Interaction):
        if not self.player.prev_stack:
            return await interaction.response.send_message(embed=msg_embed("There's no previous track.", "warn"), ephemeral=True)
        await interaction.response.defer()
        await self.cog.previous_track(self.player)

    async def on_pause(self, interaction: discord.Interaction):
        await self.player.pause(not self.player.paused)
        await self.redraw(interaction)

    async def on_skip(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.player.skip(force=True)

    async def on_loop(self, interaction: discord.Interaction):
        order = [wavelink.QueueMode.normal, wavelink.QueueMode.loop, wavelink.QueueMode.loop_all]
        q = self.player.queue
        q.mode = order[(order.index(q.mode) + 1) % len(order)]
        await self.redraw(interaction)

    async def on_shuffle(self, interaction: discord.Interaction):
        if len(self.player.queue) < 2:
            return await interaction.response.send_message(
                embed=msg_embed("There aren't enough songs in the queue to shuffle.", "warn"), ephemeral=True
            )
        self.player.queue.shuffle()
        await self.redraw(interaction)

    async def on_vol_down(self, interaction: discord.Interaction):
        await self.player.set_volume(max(0, self.player.volume - 10))
        await self.redraw(interaction)

    async def on_vol_up(self, interaction: discord.Interaction):
        await self.player.set_volume(min(150, self.player.volume + 10))
        await self.redraw(interaction)

    async def on_fav(self, interaction: discord.Interaction):
        track = self.player.current
        if not track:
            return await interaction.response.defer()
        saved = self.cog.favs.toggle(interaction.user.id, track)
        msg = (
            f"Saved **{clip(track.title, 80)}** to your favorites. Play them with `/favorites`."
            if saved
            else f"Removed **{clip(track.title, 80)}** from your favorites."
        )
        await interaction.response.send_message(embed=msg_embed(msg, "ok"), ephemeral=True)
        await self.cog.refresh(self.player)  # updates the Likes line

    # --- menus -------------------------------------------------------------
    async def on_queue(self, interaction: discord.Interaction):
        idx = int(interaction.data["values"][0])
        items = list(self.player.queue)
        if idx >= len(items):
            return await interaction.response.defer()
        track = items.pop(idx)
        self.cog.set_queue(self.player, items)
        await interaction.response.defer()
        await self.player.play(track)  # jump straight to the chosen track

    async def on_suggest(self, interaction: discord.Interaction):
        idx = int(interaction.data["values"][0])
        if idx >= len(self.player.suggestions):
            return await interaction.response.defer()
        track = self.player.suggestions.pop(idx)
        track.extras = {"requester": interaction.user.id}
        self.player.queue.put(track)
        await self.redraw(interaction)

    async def on_more(self, interaction: discord.Interaction):
        p = self.player
        action = interaction.data["values"][0]

        if action == "stop":
            await interaction.response.defer()
            return await self.cog.cleanup(p)
        if action == "clear":
            p.queue.clear()
        elif action == "autoplay":
            p.autoplay = (
                wavelink.AutoPlayMode.partial
                if p.autoplay is wavelink.AutoPlayMode.enabled
                else wavelink.AutoPlayMode.enabled
            )
        elif action in ("seek_fwd", "seek_back") and p.current and p.current.is_seekable:
            delta = 10_000 if action == "seek_fwd" else -10_000
            await p.seek(max(0, min(p.current.length - 1000, p.position + delta)))
        elif action == "bass":
            await self.cog.apply_filter(p, "bassboost")
        elif action == "nightcore":
            await self.cog.apply_filter(p, "nightcore")
        elif action == "reset":
            await self.cog.apply_filter(p, "off")
        await self.redraw(interaction)


class FavoritesView(discord.ui.View):
    def __init__(self, cog: "Music", user_id: int, rows):
        super().__init__(timeout=120)
        self.cog, self.user_id, self.rows = cog, user_id, rows
        sel = discord.ui.Select(
            placeholder="Pick a favorite to play",
            options=[
                discord.SelectOption(label=clip(title, 100), description=clip(author, 100) or None, value=str(i), emoji=M.LIKE)
                for i, (_, title, author) in enumerate(rows)
            ],
        )
        sel.callback = self.pick
        self.add_item(sel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=msg_embed("These are someone else's favorites.", "err"), ephemeral=True)
            return False
        return True

    async def pick(self, interaction: discord.Interaction):
        uri = self.rows[int(interaction.data["values"][0])][0]
        await interaction.response.defer(ephemeral=True)
        embed = await self.cog.play_query(interaction.guild, interaction.user, interaction.channel, uri)
        await interaction.followup.send(embed=embed, ephemeral=True)


# ----------------------------------------------------------------------------
# The cog
# ----------------------------------------------------------------------------
class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.favs = Favorites()
        self.tasks: set = set()

    async def cog_load(self):
        url = LAVALINK_URL.strip().rstrip("/")
        if "://" not in url:
            url = f"{'https' if SECURE else 'http'}://{url}"
        node = wavelink.Node(
            identifier=NODE_NAME,
            uri=url,
            password=LAVALINK_PASS,
            inactive_player_timeout=180,  # leave the voice channel after 3 idle minutes
        )
        try:
            await wavelink.Pool.connect(nodes=[node], client=self.bot, cache_capacity=100)
        except Exception as exc:
            print(f"[music] Could not connect to Lavalink at {LAVALINK_URL}: {exc!r}")

    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    @staticmethod
    def init_player(player: wavelink.Player):
        player.autoplay = wavelink.AutoPlayMode.partial  # play the queue automatically (no random recommendations)
        player.home = None  # text channel the panel is posted in
        player.panel = None
        player.panel_view = None
        player.ticker = None
        player.suggestions = []
        player.suggestions_ready = False
        player.prev_stack = []
        player.last_track = None
        player.going_back = False
        player.filter_name = "Off"

    # --- panel management --------------------------------------------------
    async def drop_panel(self, player):
        if player.panel_view:
            player.panel_view.stop()
        if player.panel:
            try:
                await player.panel.delete()
            except discord.HTTPException:
                pass
        player.panel = player.panel_view = None

    async def send_panel(self, player):
        if player.home is None:
            return
        await self.drop_panel(player)
        view = PanelView(self, player)
        try:
            player.panel = await player.home.send(view=view)
        except discord.HTTPException:
            return
        player.panel_view = view
        if player.ticker:
            player.ticker.cancel()
        player.ticker = asyncio.create_task(self._tick(player))

    async def refresh(self, player):
        if player.panel and player.panel_view:
            player.panel_view.rebuild()
            try:
                await player.panel.edit(view=player.panel_view)
            except discord.HTTPException:
                pass

    async def _tick(self, player):
        """Keeps the progress bar moving."""
        try:
            while player.connected:
                await asyncio.sleep(15)
                if player.playing and not player.paused:
                    await self.refresh(player)
        except asyncio.CancelledError:
            pass

    async def show_ended(self, player):
        if player.ticker:
            player.ticker.cancel()
            player.ticker = None
        if not player.panel:
            return
        if player.panel_view:
            player.panel_view.stop()
        c = discord.ui.Container(accent_colour=discord.Colour(MUSIC_BLUE))
        c.add_item(text(f"## {E.ERROR} Queue ended"))
        c.add_item(text(f"{E.BULLET} Add more songs with `/play`."))
        c.add_item(text(f"-# {FOOTER}"))
        v = discord.ui.LayoutView(timeout=None)
        v.add_item(c)
        try:
            await player.panel.edit(view=v)
        except discord.HTTPException:
            pass
        player.panel = player.panel_view = None

    async def cleanup(self, player):
        if player.ticker:
            player.ticker.cancel()
            player.ticker = None
        await self.drop_panel(player)
        try:
            await player.disconnect()
        except Exception:
            pass

    # --- suggestions -------------------------------------------------------
    async def related(self, track) -> list:
        try:
            if track.source in ("youtube", "youtubemusic") and track.identifier:
                result = await wavelink.Playable.search(
                    f"https://www.youtube.com/watch?v={track.identifier}&list=RD{track.identifier}"
                )
            else:
                source = "spsearch" if track.source == "spotify" else DEFAULT_SOURCE
                result = await wavelink.Playable.search(f"{track.author} {track.title}", source=source)
            tracks = list(result.tracks) if isinstance(result, wavelink.Playlist) else list(result)
        except Exception as exc:
            print(f"[music] suggestions failed: {exc!r}")
            return []
        return [t for t in tracks if t.identifier != track.identifier][:10]

    async def load_suggestions(self, player, track):
        tracks = await self.related(track)
        if player.current and player.current.identifier == track.identifier:
            player.suggestions = tracks
            player.suggestions_ready = True
            await self.refresh(player)

    # --- Lavalink events ---------------------------------------------------
    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload):
        print(f"[music] Lavalink node '{payload.node.identifier}' is ready (resumed={payload.resumed})")

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload):
        player = payload.player
        if not player or not hasattr(player, "panel"):
            return
        prev = player.last_track
        if prev is not None and not player.going_back and prev.identifier != payload.track.identifier:
            player.prev_stack.append(prev)
            del player.prev_stack[:-20]
        player.going_back = False
        player.last_track = payload.track
        player.suggestions, player.suggestions_ready = [], False
        await self.send_panel(player)
        self.spawn(self.load_suggestions(player, payload.track))

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        player = payload.player
        if not player or not hasattr(player, "panel"):
            return
        await asyncio.sleep(2)  # give autoplay a moment to start the next song
        if player.connected and not player.playing:
            await self.show_ended(player)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload):
        player = payload.player
        if player and getattr(player, "home", None):
            reason = (payload.exception or {}).get("message", "unknown error")
            try:
                await player.home.send(embed=msg_embed(f"Couldn't play **{clip(payload.track.title, 80)}**: {reason}", "warn"))
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player: wavelink.Player):
        if getattr(player, "home", None):
            try:
                await player.home.send(embed=msg_embed("Left the voice channel because nothing was playing for a while.", "warn"))
            except discord.HTTPException:
                pass
        await self.cleanup(player)

    # --- shared playing logic ----------------------------------------------
    async def play_query(self, guild: discord.Guild, member: discord.Member, channel, query: str) -> discord.Embed:
        voice = getattr(member, "voice", None)
        if not voice or not voice.channel:
            return msg_embed("Join a voice channel first.", "err")
        vc = guild.voice_client
        if vc and not isinstance(vc, wavelink.Player):
            return msg_embed("I'm busy with another voice feature right now.", "err")
        if vc and vc.channel != voice.channel:
            return msg_embed(f"I'm already playing in {vc.channel.mention}.", "err")

        source, term = split_source(query)
        try:
            result = await wavelink.Playable.search(term, source=source)
        except Exception as exc:
            print(f"[music] search failed: {exc!r}")
            return msg_embed("Couldn't search right now - is the Lavalink server running?", "err")
        if not result:
            return msg_embed("No results found for that search.", "warn")

        if vc is None:
            try:
                vc = await voice.channel.connect(cls=wavelink.Player, self_deaf=True)
            except Exception as exc:
                print(f"[music] voice connect failed: {exc!r}")
                return msg_embed("I couldn't join your voice channel (check my Connect/Speak permissions).", "err")
            self.init_player(vc)
        vc.home = channel

        if isinstance(result, wavelink.Playlist):
            tracks, name = list(result.tracks), result.name
        else:
            tracks, name = [result[0]], None
        for t in tracks:
            t.extras = {"requester": member.id}
            vc.queue.put(t)

        if not vc.playing:
            await vc.play(vc.queue.get(), volume=DEFAULT_VOLUME)
            if name:
                return vibe_embed(f"Loaded playlist **{clip(name, 80)}** - {len(tracks)} tracks. Starting now...")
            return vibe_embed(f"Loading **{clip(tracks[0].title, 80)}**...")

        # already playing: "N tracks in queue" card
        e = discord.Embed(title=f"{M.QUEUE} {len(vc.queue)} track(s) in queue", color=MUSIC_BLUE)
        if name:
            e.description = f"{M.SONG_NAME} Added playlist **{clip(name, 80)}** - {len(tracks)} tracks\n{M.REQUESTED_BY} *Requested by* {member.mention}"
        else:
            t = tracks[0]
            e.description = f"{M.SONG_NAME} {link(t)} - {M.DURATION} [`{fmt_time(t.length)}`]\n{M.REQUESTED_BY} *Requested by* {member.mention}"
            art = artwork(t)
            if art:
                e.set_thumbnail(url=art)
        e.set_footer(text=f"Volume: {vc.volume}% | {FOOTER}")
        return e

    async def active_player(self, ctx: commands.Context) -> Optional[wavelink.Player]:
        player = ctx.guild.voice_client
        if not isinstance(player, wavelink.Player) or not player.connected:
            await ctx.send(embed=msg_embed("Nothing is playing right now.", "err"), ephemeral=True)
            return None
        if not ctx.author.voice or ctx.author.voice.channel != player.channel:
            await ctx.send(embed=msg_embed(f"Join {player.channel.mention} first.", "err"), ephemeral=True)
            return None
        return player

    # --- helpers for the commands -------------------------------------------
    async def previous_track(self, player) -> bool:
        if not player.prev_stack:
            return False
        prev = player.prev_stack.pop()
        rest = list(player.queue)
        player.queue.clear()
        if player.current:
            player.queue.put(player.current)  # current song goes back to the front of the queue
        for t in rest:
            player.queue.put(t)
        player.going_back = True
        await player.play(prev)
        return True

    @staticmethod
    def set_queue(player, items):
        player.queue.clear()
        for t in items:
            player.queue.put(t)

    async def apply_filter(self, player, effect: str):
        if effect == "bassboost":
            f = wavelink.Filters()
            f.equalizer.set(bands=[{"band": 0, "gain": 0.25}, {"band": 1, "gain": 0.20}, {"band": 2, "gain": 0.15}, {"band": 3, "gain": 0.10}])
            await player.set_filters(f)
            player.filter_name = "Bass boost"
        elif effect == "nightcore":
            f = wavelink.Filters()
            f.timescale.set(speed=1.2, pitch=1.2, rate=1.0)
            await player.set_filters(f)
            player.filter_name = "Nightcore"
        else:
            await player.set_filters()
            player.filter_name = "Off"

    @staticmethod
    async def say(ctx, message: str, kind: str = "ok", quiet: bool = False):
        """Reply with a small card. Errors (and 'quiet' replies) are only visible to the user in slash commands."""
        await ctx.send(embed=msg_embed(message, kind), ephemeral=quiet or kind != "ok")

    # --- connection ----------------------------------------------------------
    @commands.hybrid_command(name="join", aliases=["connect", "j"], description="Join your voice channel")
    @commands.guild_only()
    async def join(self, ctx: commands.Context):
        voice = ctx.author.voice
        if not voice or not voice.channel:
            return await self.say(ctx, "Join a voice channel first.", "err")
        vc = ctx.guild.voice_client
        if vc and not isinstance(vc, wavelink.Player):
            return await self.say(ctx, "I'm busy with another voice feature right now.", "err")
        try:
            if vc is None:
                vc = await voice.channel.connect(cls=wavelink.Player, self_deaf=True)
                self.init_player(vc)
            elif vc.channel != voice.channel:
                if vc.playing:
                    return await self.say(ctx, f"I'm already playing in {vc.channel.mention}.", "err")
                await vc.move_to(voice.channel)
        except Exception as exc:
            print(f"[music] join failed: {exc!r}")
            return await self.say(ctx, "I couldn't join your voice channel (check my Connect/Speak permissions).", "err")
        vc.home = ctx.channel
        await self.say(ctx, f"Joined {voice.channel.mention}. Use `/play` to start the music.")

    @commands.hybrid_command(name="leave", aliases=["disconnect", "dc"], description="Leave the voice channel")
    @commands.guild_only()
    async def leave(self, ctx: commands.Context):
        player = await self.active_player(ctx)
        if not player:
            return
        await self.cleanup(player)
        await self.say(ctx, "Left the voice channel.")

    # --- playback -----------------------------------------------------------
    @commands.hybrid_command(name="play", aliases=["p"], description="Play a song or add it to the queue")
    @app_commands.describe(query="Song name or link. Prefix with spotify / ytmusic / soundcloud to pick a source")
    @commands.guild_only()
    async def play(self, ctx: commands.Context, *, query: str):
        await ctx.defer()
        await ctx.send(embed=await self.play_query(ctx.guild, ctx.author, ctx.channel, query))

    @commands.hybrid_command(name="pause", description="Pause the music")
    @commands.guild_only()
    async def pause(self, ctx: commands.Context):
        player = await self.active_player(ctx)
        if not player:
            return
        if player.paused:
            return await self.say(ctx, "The music is already paused.", "warn")
        await player.pause(True)
        await self.refresh(player)
        await self.say(ctx, "Paused.", quiet=True)

    @commands.hybrid_command(name="resume", aliases=["unpause"], description="Resume the music")
    @commands.guild_only()
    async def resume(self, ctx: commands.Context):
        player = await self.active_player(ctx)
        if not player:
            return
        if not player.paused:
            return await self.say(ctx, "The music isn't paused.", "warn")
        await player.pause(False)
        await self.refresh(player)
        await self.say(ctx, "Resumed.", quiet=True)

    @commands.hybrid_command(name="skip", aliases=["s", "next"], description="Skip the current song")
    @commands.guild_only()
    async def skip(self, ctx: commands.Context):
        player = await self.active_player(ctx)
        if not player:
            return
        if not player.current:
            return await self.say(ctx, "Nothing is playing right now.", "err")
        await player.skip(force=True)
        await self.say(ctx, "Skipped.", quiet=True)

    @commands.hybrid_command(name="previous", aliases=["prev", "back"], description="Play the previous song")
    @commands.guild_only()
    async def previous(self, ctx: commands.Context):
        player = await self.active_player(ctx)
        if not player:
            return
        if not await self.previous_track(player):
            return await self.say(ctx, "There's no previous track.", "warn")
        await self.say(ctx, "Playing the previous song.", quiet=True)

    @commands.hybrid_command(name="stop", description="Stop the music and clear the queue (stays in the voice channel)")
    @commands.guild_only()
    async def stop(self, ctx: commands.Context):
        player = await self.active_player(ctx)
        if not player:
            return
        player.autoplay = wavelink.AutoPlayMode.partial
        player.queue.clear()
        player.queue.mode = wavelink.QueueMode.normal
        if player.playing:
            await player.skip(force=True)
        await self.say(ctx, "Stopped the music and cleared the queue. Use `/leave` to disconnect me.")

    @commands.hybrid_command(name="replay", aliases=["restart"], description="Restart the current song")
    @commands.guild_only()
    async def replay(self, ctx: commands.Context):
        player = await self.active_player(ctx)
        if not player:
            return
        if not player.current or not player.current.is_seekable:
            return await self.say(ctx, "I can't restart this track.", "err")
        await player.seek(0)
        await self.refresh(player)
        await self.say(ctx, "Restarted the song.", quiet=True)

    @commands.hybrid_command(name="seek", description="Jump to a time in the song (like 90 or 1:30)")
    @app_commands.describe(position="Time like 90 (seconds) or 1:30")
    @commands.guild_only()
    async def seek(self, ctx: commands.Context, position: str):
        player = await self.active_player(ctx)
        if not player:
            return
        track = player.current
        if not track or not track.is_seekable:
            return await self.say(ctx, "I can't seek in this track.", "err")
        ms = parse_time(position)
        if ms is None:
            return await self.say(ctx, "Use a time like `90` or `1:30`.", "err")
        await player.seek(min(ms, max(0, track.length - 1000)))
        await self.refresh(player)
        await self.say(ctx, f"Jumped to **{fmt_time(min(ms, track.length))}**.", quiet=True)

    # --- queue --------------------------------------------------------------
    @commands.hybrid_command(name="loop", aliases=["repeat"], description="Set the loop mode (off, track or queue)")
    @app_commands.describe(mode="off, track or queue. Leave empty to cycle through them")
    @commands.guild_only()
    async def loop(self, ctx: commands.Context, mode: Optional[Literal["off", "track", "queue"]] = None):
        player = await self.active_player(ctx)
        if not player:
            return
        modes = {"off": wavelink.QueueMode.normal, "track": wavelink.QueueMode.loop, "queue": wavelink.QueueMode.loop_all}
        if mode:
            player.queue.mode = modes[mode]
        else:
            order = list(modes.values())
            player.queue.mode = order[(order.index(player.queue.mode) + 1) % len(order)]
        await self.refresh(player)
        await self.say(ctx, f"Loop mode: **{LOOP_NAMES[player.queue.mode]}**.")

    @commands.hybrid_command(name="autoplay", aliases=["ap"], description="Turn autoplay on or off")
    @app_commands.describe(mode="on or off. Leave empty to toggle")
    @commands.guild_only()
    async def autoplay(self, ctx: commands.Context, mode: Optional[Literal["on", "off"]] = None):
        player = await self.active_player(ctx)
        if not player:
            return
        enabled = player.autoplay is wavelink.AutoPlayMode.enabled
        target = (mode == "on") if mode else not enabled
        player.autoplay = wavelink.AutoPlayMode.enabled if target else wavelink.AutoPlayMode.partial
        await self.refresh(player)
        await self.say(ctx, f"Autoplay is now **{'on' if target else 'off'}**.")

    @commands.hybrid_command(name="shuffle", description="Shuffle the queue")
    @commands.guild_only()
    async def shuffle(self, ctx: commands.Context):
        player = await self.active_player(ctx)
        if not player:
            return
        if len(player.queue) < 2:
            return await self.say(ctx, "There aren't enough songs in the queue to shuffle.", "warn")
        player.queue.shuffle()
        await self.refresh(player)
        await self.say(ctx, "Shuffled the queue.")

    @commands.hybrid_command(name="clear", aliases=["clearqueue", "cq"], description="Clear the queue")
    @commands.guild_only()
    async def clear(self, ctx: commands.Context):
        player = await self.active_player(ctx)
        if not player:
            return
        player.queue.clear()
        await self.refresh(player)
        await self.say(ctx, "Cleared the queue.")

    @commands.hybrid_command(name="remove", aliases=["rm"], description="Remove a song from the queue")
    @app_commands.describe(position="Position of the song in the queue (see /queue)")
    @commands.guild_only()
    async def remove(self, ctx: commands.Context, position: commands.Range[int, 1, 500]):
        player = await self.active_player(ctx)
        if not player:
            return
        items = list(player.queue)
        if position > len(items):
            return await self.say(ctx, f"The queue only has {len(items)} song(s).", "err")
        track = items.pop(position - 1)
        self.set_queue(player, items)
        await self.refresh(player)
        await self.say(ctx, f"Removed **{clip(track.title, 80)}** from the queue.")

    @commands.hybrid_command(name="skipto", aliases=["jump"], description="Skip ahead to a song in the queue")
    @app_commands.describe(position="Position of the song in the queue (see /queue)")
    @commands.guild_only()
    async def skipto(self, ctx: commands.Context, position: commands.Range[int, 1, 500]):
        player = await self.active_player(ctx)
        if not player:
            return
        items = list(player.queue)
        if position > len(items):
            return await self.say(ctx, f"The queue only has {len(items)} song(s).", "err")
        track = items[position - 1]
        self.set_queue(player, items[position:])  # songs before it are dropped
        await player.play(track)
        await self.say(ctx, f"Skipping to **{clip(track.title, 80)}**.", quiet=True)

    @commands.hybrid_command(name="queue", aliases=["q"], description="Show the song queue")
    @commands.guild_only()
    async def queue_cmd(self, ctx: commands.Context):
        player = await self.active_player(ctx)
        if not player:
            return
        items = list(player.queue)
        lines = [
            f"`{i}.` {link(t)} - {M.AUTHOR} {clip(t.author, 30)} {M.DURATION} `{fmt_time(t.length)}`"
            for i, t in enumerate(items[:10], 1)
        ]
        e = discord.Embed(title=f"{M.QUEUE} Queue", color=MUSIC_BLUE)
        if player.current:
            e.add_field(
                name=f"{M.NOW_PLAYING} Now playing {M.SONG_VIBING}",
                value=f"{link(player.current)} - {M.AUTHOR} {clip(player.current.author, 30)}",
                inline=False,
            )
        e.add_field(
            name=f"Up next ({len(items)})",
            value="\n".join(lines) + (f"\n...and {len(items) - 10} more" if len(items) > 10 else "") if lines else "*Nothing queued.*",
            inline=False,
        )
        await ctx.send(embed=e.set_footer(text=FOOTER))

    # --- sound --------------------------------------------------------------
    @commands.hybrid_command(name="volume", aliases=["vol", "v"], description="Set the music volume (0-150)")
    @app_commands.describe(level="Volume from 0 to 150")
    @commands.guild_only()
    async def volume(self, ctx: commands.Context, level: commands.Range[int, 0, 150]):
        player = await self.active_player(ctx)
        if not player:
            return
        await player.set_volume(level)
        await self.refresh(player)
        await self.say(ctx, f"Volume set to **{level}%**.", quiet=True)

    @commands.hybrid_command(name="filter", aliases=["filters", "fx"], description="Apply an audio filter")
    @app_commands.describe(effect="bassboost, nightcore or off")
    @commands.guild_only()
    async def filter_cmd(self, ctx: commands.Context, effect: Literal["bassboost", "nightcore", "off"]):
        player = await self.active_player(ctx)
        if not player:
            return
        await self.apply_filter(player, effect)
        await self.refresh(player)
        await self.say(ctx, f"Filter: **{player.filter_name}**.")

    # --- panel / favorites --------------------------------------------------
    @commands.hybrid_command(name="nowplaying", aliases=["np"], description="Bring the music control panel back to the bottom")
    @commands.guild_only()
    async def nowplaying(self, ctx: commands.Context):
        player = await self.active_player(ctx)
        if not player:
            return
        if not player.current:
            return await self.say(ctx, "Nothing is playing right now.", "err")
        player.home = ctx.channel
        await self.send_panel(player)
        await self.say(ctx, "Control panel refreshed.", quiet=True)

    @commands.hybrid_command(name="favorites", aliases=["favs"], description="Play the songs you saved with the heart button")
    @commands.guild_only()
    async def favorites(self, ctx: commands.Context):
        rows = self.favs.all(ctx.author.id)
        if not rows:
            return await self.say(ctx, "You have no favorites yet - press the heart button on the music panel.", "warn")
        e = discord.Embed(
            title=f"{M.LIKE} Your favorites",
            description=f"{E.BULLET} Pick a song below and I'll queue it (you must be in a voice channel).",
            color=MUSIC_BLUE,
        ).set_footer(text=FOOTER)
        await ctx.send(embed=e, view=FavoritesView(self, ctx.author.id, rows), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
