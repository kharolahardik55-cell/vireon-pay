"""Shared constants and helpers for Vireon Pay (payments + music).  Crafted with \u2764\ufe0f by esco.md"""
import discord

BOT_NAME = "Vireon Pay"
FOOTER = "Crafted with \u2764\ufe0f by esco.md"


class E:
    TICK = "<a:emoji_15:1555534704743161876>"
    CROSS = "<a:Cross:1555534753506001017>"
    CARD = "<:card:1555534876592177202>"
    ERROR = "<a:tmr_warning_animated:1555535841256935424>"
    LINK = "<:link:1555535758742523977>"
    BULLET = "<a:Animated_Arrow_Bluelite:1555537213654302720>"
    MONEY = "<a:money:1555537937112768602>"
    TICK_TICK = "<a:TICK_TICK:1555554626864943235>"


class M:
    """Music emojis (DURATION is also the "pending" emoji everywhere in the bot)."""
    TIMER = "<:timer:1556279997176619141>"          # song duration / progress
    LOOP = "<:loop:1556279375689941022>"
    PAUSE = "<:pause:1556279503293128766>"
    PLAY = "<:play:1556279732830470215>"
    QUEUE = "<:queue:1556279414277546084>"
    PREVIOUS = "<:previous:1556279462029426729>"
    NEXT = "<:next:1556279435991318558>"
    SONG_NAME = "<:song_name:1556279767068708945>"
    NOW_PLAYING = "<a:now_playing:1556279687687184444>"
    VOLUME = "<:volume:1556279791546802186>"
    AUTHOR = "<:author:1556279555214544979>"
    AUTOPLAY = "<:autoplay:1556279821326352475>"
    CLEAR_QUEUE = "<:clear_queue:1556279911650566144>"
    DURATION = "<:duration:1556279242051035226>"
    LIKE = "<:like:1556279882932158545>"
    SONG_VIBING = "<a:song_vibing:1556279292780871862>"
    REQUESTED_BY = "<:requested_by:1556279853974818967>"
    SHUFFLE = "<:shuffle:1556279604510072872>"
    ROUND_BOOST = "<:RoundBoost:1556292291755253881>"  # bass boost


PURPLE, GREEN, RED, YELLOW = 0x7B61FF, 0x2ECC71, 0xED4245, 0xFEE75C


def msg_embed(text: str, kind: str = "ok") -> discord.Embed:
    icon, color = {"ok": (E.TICK, GREEN), "err": (E.CROSS, RED), "warn": (E.ERROR, YELLOW)}[kind]
    return discord.Embed(description=f"{icon} {text}", color=color).set_footer(text=FOOTER)
