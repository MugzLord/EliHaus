import os, sqlite3, random, json, traceback
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands
from discord import app_commands
from zoneinfo import ZoneInfo  # proper DST (e.g., Europe/London)
import io, time

# ---------------- Config ----------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN")

GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0"))

TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/London")
try:
    TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
    TZ = timezone.utc

WL_COINS_PER_GIFT = int(os.getenv("WL_COINS_PER_GIFT", "5000"))  # default 10k coins = 1 WL
MIN_WL_GIFTS = int(os.getenv("MIN_WL_GIFTS", "1"))
MAX_WL_GIFTS = int(os.getenv("MAX_WL_GIFTS", "40"))
ROULETTE_STATE = {"resolved": False}

STICKY_AFTER_MSGS = 15  # bump after this many chat messages
STICKY_COUNT: dict[int, int] = {}  # channel_id -> counter since last bump

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))

from collections import defaultdict

MESSAGE_COUNTER = defaultdict(int)   # channel_id -> count since last bump
ACTIVE_PANEL_MSG: dict[int, int] = {}  # channel_id -> betting panel message_id
# Only this user can use /eh_deposit
DEPOSITOR_ID = int(os.getenv("DEPOSITOR_ID", "0"))  # set your Discord User ID in env

from pathlib import Path
BASE_DIR = Path(__file__).parent
ASSETS_DIR = BASE_DIR / "assets"

# Economy
DAILY_AMOUNT = 1_800
WEEKLY_AMOUNT = 6_000
STARTER_AMOUNT = 5_000

# Lotto
TICKET_COST = 10_000
LOTTO_WINNERS = 1
LOTTO_WL_COUNT = 10
SHOP_NAME = "Shop YaEli"
# Keep SHOP_YAELI_URL defined first
SHOP_YAELI_URL = os.getenv(
    "SHOP_YAELI_URL",
    "https://www.imvu.com/shop/web_search.php?manufacturers_id=360644281"
)

# Then define the policy (can be overridden via ELIHAUS_POLICY env var)
DEFAULT_POLICY_TEXT = (
    f"**Policy:** To claim your winnings, you must have **10 items** added from "
    f"**[Shop YaEli]({SHOP_YAELI_URL})**. Failure to comply is subject to **disqualification**."
)
POLICY_TEXT = os.getenv("ELIHAUS_POLICY", DEFAULT_POLICY_TEXT)

import json

POLICY_STATE_KEY = "policy_config"

def get_policy_config() -> dict:
    """
    Returns current policy config from DB state, or sensible defaults.
    Keys: shop_name, shop_url, min_items.
    """
    raw = get_state(POLICY_STATE_KEY)
    if not raw:
        # fallback to env / defaults
        return {
            "shop_name": SHOP_NAME,
            "shop_url": SHOP_YAELI_URL,
            "min_items": 10,
        }
    try:
        data = json.loads(raw)
    except Exception:
        return {
            "shop_name": SHOP_NAME,
            "shop_url": SHOP_YAELI_URL,
            "min_items": 10,
        }

    # fill any missing keys
    data.setdefault("shop_name", SHOP_NAME)
    data.setdefault("shop_url", SHOP_YAELI_URL)
    data.setdefault("min_items", 10)
    return data

def set_policy_config(shop_name: str, shop_url: str, min_items: int):
    cfg = {
        "shop_name": shop_name,
        "shop_url": shop_url,
        "min_items": int(min_items),
    }
    set_state(POLICY_STATE_KEY, json.dumps(cfg))

def build_policy_text() -> str:
    cfg = get_policy_config()
    sname = cfg["shop_name"]
    surl = cfg["shop_url"]
    n    = cfg["min_items"]
    return (
        f"**Policy:** To claim your winnings, you must have **{n} items** added from "
        f"**[{sname}]({surl})**. Failure to comply is subject to **disqualification**."
    )

# Roulette (admin-led)
ROUND_SECONDS_DEFAULT = 120
PAYOUT_RED_BLACK = 2.0
PAYOUT_GREEN = 14.0
PAYOUT_NUMBER = 36.0  # straight number pays 35:1 (returns 36x)
MAX_STAKE = 50_000
ONE_BET_PER_ROUND = True

# Tickets category for WL claims
TICKETS_CATEGORY_ID = int(os.getenv("TICKETS_CATEGORY_ID", "0"))
TICKETS_CATEGORY_NAME = os.getenv("TICKETS_CATEGORY_NAME", "🎟️ wl-claims")
TICKETS_STAFF_ROLE_ID = int(os.getenv("TICKETS_STAFF_ROLE_ID", "0"))

# ---------------- DB ----------------
DB_PATH = os.getenv("ELIHAUS_DB", "elihause.db")

def db():
    return sqlite3.connect(DB_PATH, isolation_level=None)

def init_db():
    with db() as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            discord_id TEXT UNIQUE,
            balance INTEGER DEFAULT 0,
            last_daily TEXT,
            last_weekly TEXT,
            joined_at TEXT,
            tutorial_done INTEGER DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS tx(
            id INTEGER PRIMARY KEY,
            discord_id TEXT,
            kind TEXT,
            amount INTEGER,
            meta TEXT,
            ts TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS state(
            key TEXT PRIMARY KEY,
            val TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS rounds(
            rid TEXT PRIMARY KEY,
            channel_id TEXT,
            status TEXT,       -- OPEN|RESOLVED|CANCELLED
            opened_by TEXT,
            opened_at TEXT,
            expires_at TEXT,
            outcome TEXT,
            seed TEXT,
            resolved_at TEXT,
            message_id TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS bets(
            id INTEGER PRIMARY KEY,
            rid TEXT,
            channel_id TEXT,
            discord_id TEXT,
            choice TEXT,
            stake INTEGER,
            ts TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS tickets(
            id INTEGER PRIMARY KEY,
            week_id TEXT,
            discord_id TEXT,
            ts TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS lotto_draws(
            id INTEGER PRIMARY KEY,
            week_id TEXT,
            run_at TEXT,
            winner_id TEXT,
            seed TEXT,
            status TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS prizes(
            id INTEGER PRIMARY KEY,
            winner_id TEXT,
            kind TEXT,
            amount INTEGER,
            meta TEXT,
            status TEXT,
            created_ts TEXT,
            updated_ts TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS prize_queue(
            id INTEGER PRIMARY KEY,
            prize_id INTEGER,
            winner_id TEXT,
            imvu_name TEXT,
            imvu_profile TEXT,
            note TEXT,
            status TEXT,         -- 'waiting_claim','ready','fulfilled','failed'
            created_ts TEXT,
            updated_ts TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS withdraw_requests(
            id INTEGER PRIMARY KEY,
            discord_id TEXT,
            coins INTEGER,
            gifts INTEGER,
            imvu_name TEXT,
            imvu_profile TEXT,  -- wishlist or profile URL
            note TEXT,
            status TEXT,        -- 'pending','approved','rejected'
            ticket_channel_id TEXT,
            message_id TEXT,    -- review message id inside ticket
            reviewer_id TEXT,   -- admin who approved/rejected
            review_note TEXT,
            created_ts TEXT,
            updated_ts TEXT
        )""")

init_db()


# ---- Slots helpers (state) ----
def _slots_pot_key(channel_id: int) -> str:
    return f"slots_pot:{int(channel_id)}"

def _slots_msg_key(channel_id: int) -> str:
    return f"slots_msg:{int(channel_id)}"   # used when you store the panel message id

def get_slots_pot(channel_id: int) -> int:
    v = get_state(_slots_pot_key(channel_id))
    try:
        return int(v)
    except (TypeError, ValueError):
        return SLOTS_SEED   # default when nothing stored yet

def set_slots_pot(channel_id: int, value: int) -> None:
    set_state(_slots_pot_key(channel_id), int(value))

def now_local():
    return datetime.now(TZ)

def iso(dt: datetime) -> str:
    return dt.astimezone(TZ).isoformat()

def set_state(key: str, val: str | None):
    with db() as conn:
        c = conn.cursor()
        if val is None:
            c.execute("DELETE FROM state WHERE key=?", (key,))
        else:
            c.execute("""INSERT INTO state(key,val) VALUES(?,?)
                         ON CONFLICT(key) DO UPDATE SET val=excluded.val""", (key, val))

def get_state(key: str) -> str | None:
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT val FROM state WHERE key=?", (key,))
        r = c.fetchone()
        return r[0] if r else None

def round_key(channel_id: int) -> str:
    return f"round:{channel_id}"

def week_id(dt: datetime | None = None) -> str:
    dt = dt or now_local()
    y, w, _ = dt.isocalendar()
    return f"{y}-{w:02d}"

def _is_admin_member(guild: discord.Guild, member: discord.Member) -> bool:
    if member.guild_permissions.manage_guild or guild.owner_id == member.id:
        return True
    if 'ADMIN_ROLE_ID' in globals() and ADMIN_ROLE_ID:
        role = guild.get_role(ADMIN_ROLE_ID)
        if role and role in member.roles:
            return True
    return False

LONDON_TZ = ZoneInfo("Europe/London")

def now_london() -> datetime:
    return datetime.now(LONDON_TZ)

def next_draw_dt(ref: datetime | None = None) -> datetime:
    """Next Saturday 20:00 London time."""
    ref = ref or now_london()
    target_wd = 5  # 0=Mon ... 5=Sat
    days_ahead = (target_wd - ref.weekday()) % 7
    candidate = (ref + timedelta(days=days_ahead)).replace(
        hour=20, minute=0, second=0, microsecond=0
    )
    if candidate <= ref:
        candidate += timedelta(days=7)
    return candidate

def human_left(dt: datetime, ref: datetime | None = None) -> str:
    ref = ref or now_london()
    secs = max(0, int((dt - ref).total_seconds()))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m and not d: parts.append(f"{m}m")
    return " ".join(parts) or "less than 1m"

# --- Roulette result embed builder ---
ROULETTE_THUMB_URL = os.getenv("ROULETTE_THUMB_URL", "")  # optional small logo for vibe

def _result_color(outcome: str) -> discord.Color:
    if outcome == "red":
        return discord.Color.red()
    if outcome == "black":
        return discord.Color.dark_grey()
    return discord.Color.green()

#added for winners embed
def _result_emoji(outcome: str) -> str:
    return {"red": "🟥", "black": "⬛", "green": "🟩"}.get(outcome, "🎯")

def build_roulette_result_embed(
    rlabel: str,
    outcome: str,
    roll: int,
    total_bets: int,
    total_pool: int,
    winners_mentions: list[str],
    seed_display: str,
) -> discord.Embed:
    label = f"{_result_emoji(outcome)} {outcome.upper()} • #{roll}"
    e = discord.Embed(
        title=f"🎰 EliHaus Roulette — Round {rlabel}",
        description=f"**RESULT:** {label}",
        color=_result_color(outcome),
        timestamp=now_local(),
    )
    e.add_field(name="Total Bets", value=str(total_bets))
    e.add_field(name="Pool", value=str(total_pool))
    top = "—" if not winners_mentions else "\n".join(winners_mentions[:5])
    e.add_field(name="Winners (top)", value=top, inline=False)
    e.set_footer(text=seed_display)
    return e

# === interaction reply helpers (paste once; keep above commands) ===
async def safe_ack(interaction: discord.Interaction, ephemeral: bool = True):
    """Defer quickly so the command never times out."""
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral, thinking=True)
    except Exception:
        pass

async def safe_followup(interaction: discord.Interaction, content: str, ephemeral: bool = True):
    """Send a reply whether or not we already deferred."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)
    except Exception:
        pass

# ---------- Roulette badge rendering ----------
from io import BytesIO

# where your 3 base chips live
CHIP_ASSETS = {
    "RED":    str(ASSETS_DIR / "chip_red.png"),
    "BLACK":  str(ASSETS_DIR / "chip_black.png"),
    "GREEN":  str(ASSETS_DIR / "chip_green.png"),
}


def roulette_color_from_number(n: int) -> str:
    red = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    if n == 0:
        return "GREEN"
    return "RED" if n in red else "BLACK"

# requires: from PIL import Image, ImageDraw, ImageFont
# and import io

def render_chip_badge(result_color: str, result_number: int) -> io.BytesIO | None:
    # normalise key and resolve asset
    key = str(result_color).strip().upper()
    base_path = CHIP_ASSETS.get(key)
    if not base_path:
        print(f"[chips] no asset mapping for color '{key}'")
        return None

    try:  # <-- THIS is the 'first try' I was referring to
        chip = Image.open(base_path).convert("RGBA")
    except Exception as e:
        print(f"[chips] failed to open asset '{base_path}': {e}")
        return None

    # upscale chip so text is crisp
    SCALE = 5
    W, H = chip.size
    chip = chip.resize((W*SCALE, H*SCALE), Image.LANCZOS)

    draw = ImageDraw.Draw(chip)

    # font selection (second try is for fonts only)
    font = None
    for fp in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "DejaVuSans-Bold.ttf",
        "Arial.ttf",
        "arial.ttf",
    ):
        try:
            font = ImageFont.truetype(fp, size=int(chip.width * 0.42))
            break
        except Exception:
            pass
    if font is None:
        font = ImageFont.load_default()

    # text + outline
    text = str(int(result_number))
    cx, cy = chip.width // 2, chip.height // 2
    draw.text(
        (cx, cy),
        text,
        font=font,
        fill=(255, 255, 255, 255),
        anchor="mm",
        stroke_width=max(2, chip.width // 60),
        stroke_fill=(0, 0, 0, 255),
    )

    # export to BytesIO for discord.File
    out = io.BytesIO()
    chip.save(out, format="PNG")
    out.seek(0)
    return out



    # open base chip
    img = Image.open(base_path).convert("RGBA")
    W, H = img.size
    draw = ImageDraw.Draw(img)

    # choose a font; DejaVuSans is commonly available in Linux containers
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", int(H * 0.44))
    except Exception:
        font = ImageFont.load_default()

    text = str(int(number))
    # center the number
    tw, th = draw.textlength(text, font=font), font.size
    x = (W - tw) / 2
    y = (H - th) / 2 - H * 0.02

    # draw with slight stroke so it pops
    draw.text((x, y), text, font=font, fill=(245, 241, 235, 255), stroke_width=int(H * 0.035), stroke_fill=(0, 0, 0, 140))

    buf = BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf

# ---------------- Admin check helpers ----------------
def user_is_admin(member: discord.Member) -> bool:
    if getattr(member.guild_permissions, "manage_guild", False) or member.id == getattr(member.guild, "owner_id", 0):
        return True
    if ADMIN_ROLE_ID and hasattr(member, "roles"):
        return any(getattr(r, "id", 0) == ADMIN_ROLE_ID for r in member.roles)
    return False


async def _edit_round_message(bot, channel, rid: int, embed, view=None):
    """Edits the ONE official roulette message for this round. If missing, sends and records it."""
    # fetch message_id for this round
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT message_id FROM rounds WHERE rid=?", (rid,))
        row = c.fetchone()
    msg_id = int(row[0]) if row and row[0] else None

    # resolve channel object
    ch = channel
    if isinstance(channel, int):
        ch = bot.get_channel(channel) or await bot.fetch_channel(channel)

    # try edit the original message
    if msg_id:
        try:
            msg = await ch.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view)
            return
        except Exception:
            pass  # message was deleted or not found

    # fallback: send a new one and save its id
    new_msg = await ch.send(embed=embed, view=view)
    with db() as conn:
        c = conn.cursor()
        c.execute("UPDATE rounds SET message_id=? WHERE rid=?", (str(new_msg.id), rid))

# ---------------- Tickets category helper ----------------
async def _get_or_create_tickets_category(guild: discord.Guild) -> discord.CategoryChannel | None:
    if TICKETS_CATEGORY_ID:
        cat = guild.get_channel(TICKETS_CATEGORY_ID)
        if isinstance(cat, discord.CategoryChannel):
            return cat
    for ch in guild.categories:
        if ch.name == TICKETS_CATEGORY_NAME:
            return ch
    try:
        return await guild.create_category(TICKETS_CATEGORY_NAME, reason="EliHaus WL claims")
    except Exception:
        return None

# ---------------- Prize state keys ----------------
def _prize_msg_key(prize_id: int) -> str:
    return f"prize_msg:{prize_id}"

def _prize_ticket_key(prize_id: int) -> str:
    return f"prize_ticket:{prize_id}"

# ------------- Views & Modals -------------
# --------- Roulette buttons (colored) -----
class RouletteBetView(discord.ui.View):
    def __init__(self, rid: int, timeout: float | None = None):
        super().__init__(timeout=timeout)
        self.rid = rid

    # 🔴 RED
    @discord.ui.button(label="Bet RED", style=discord.ButtonStyle.danger, emoji="🎯", custom_id="eh_roul_red")
    async def bet_red(self, itx: discord.Interaction, _: discord.ui.Button):
        uid = str(itx.user.id)

        # 👉 1 bet per round
        with db() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM bets WHERE rid=? AND discord_id=? LIMIT 1", (self.rid, uid))
            if c.fetchone():
                await itx.response.send_message("⚠️ You already placed a bet this spin. Wait for the next wheel.", ephemeral=True)
                return

        try:
            await itx.response.send_modal(RedBetModal(self.rid))
        except Exception as e:
            if not itx.response.is_done():
                await itx.response.send_message(f"❌ Failed to open Red bet modal: `{e}`", ephemeral=True)
            else:
                await itx.followup.send(f"❌ Failed to open Red bet modal: `{e}`", ephemeral=True)

    # ⚫ BLACK
    @discord.ui.button(label="Bet BLACK", style=discord.ButtonStyle.primary, emoji="🎯", custom_id="eh_roul_black")
    async def bet_black(self, itx: discord.Interaction, _: discord.ui.Button):
        uid = str(itx.user.id)

        # 👉 1 bet per round
        with db() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM bets WHERE rid=? AND discord_id=? LIMIT 1", (self.rid, uid))
            if c.fetchone():
                await itx.response.send_message("⚠️ You already locked in a bet this round.", ephemeral=True)
                return

        try:
            await itx.response.send_modal(BlackBetModal(self.rid))
        except Exception as e:
            if not itx.response.is_done():
                await itx.response.send_message(f"❌ Failed to open Black bet modal: `{e}`", ephemeral=True)
            else:
                await itx.followup.send(f"❌ Failed to open Black bet modal: `{e}`", ephemeral=True)

    # 💚 GREEN
    @discord.ui.button(label="Bet GREEN", style=discord.ButtonStyle.success, emoji="🎯", custom_id="eh_roul_green")
    async def bet_green(self, itx: discord.Interaction, _: discord.ui.Button):
        uid = str(itx.user.id)

        # 👉 1 bet per round
        with db() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM bets WHERE rid=? AND discord_id=? LIMIT 1", (self.rid, uid))
            if c.fetchone():
                await itx.response.send_message("⚠️ Bet already placed. Try next spin, boss.", ephemeral=True)
                return

        try:
            await itx.response.send_modal(GreenBetModal(self.rid))
        except Exception as e:
            if not itx.response.is_done():
                await itx.response.send_message(f"❌ Failed to open Green bet modal: `{e}`", ephemeral=True)
            else:
                await itx.followup.send(f"❌ Failed to open Green bet modal: `{e}`", ephemeral=True)

    # 🔢 NUMBER
    @discord.ui.button(label="Bet NUMBER", style=discord.ButtonStyle.secondary, emoji="🎯", custom_id="eh_roul_number")
    async def bet_number(self, itx: discord.Interaction, _: discord.ui.Button):
        uid = str(itx.user.id)

        # 👉 1 bet per round
        with db() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM bets WHERE rid=? AND discord_id=? LIMIT 1", (self.rid, uid))
            if c.fetchone():
                await itx.response.send_message("⚠️ You’ve already got a ticket on this wheel.", ephemeral=True)
                return

        try:
            await itx.response.send_modal(NumberBetModal(self.rid))
        except Exception as e:
            if not itx.response.is_done():
                await itx.response.send_message(f"❌ Failed to open Number bet modal: `{e}`", ephemeral=True)
            else:
                await itx.followup.send(f"❌ Failed to open Number bet modal: `{e}`", ephemeral=True)

    # ❓ My Bet
    @discord.ui.button(label="My Bet", style=discord.ButtonStyle.secondary, emoji="📍", custom_id="eh_roul_mybet")
    async def my_bet(self, itx: discord.Interaction, _: discord.ui.Button):
        await itx.response.defer(ephemeral=True, thinking=True)
        uid = str(itx.user.id)
        with db() as conn:
            c = conn.cursor()
            c.execute("SELECT choice, stake FROM bets WHERE rid=? AND discord_id=? LIMIT 1", (self.rid, uid))
            row = c.fetchone()
        bal = get_balance(uid)
        if not row:
            return await itx.followup.send(f"You have **no bet** this round.\nBalance: **{bal}**", ephemeral=True)
        choice, stake = row
        await itx.followup.send(f"🧾 Your bet: **{choice}** → **{stake}** coins.\nBalance: **{bal}**", ephemeral=True)


class DisabledClaimView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        btn = discord.ui.Button(label="Claim WL Gifts", style=discord.ButtonStyle.secondary, disabled=True)
        self.add_item(btn)

class ClaimView(discord.ui.View):
    """Also hosts round-label helpers; we call them via ClaimView.* to avoid NameError."""
    def __init__(self, prize_id: int, timeout: int = 600):
        super().__init__(timeout=timeout)
        self.prize_id = prize_id

    # ---- Winner ID lookup for this prize ----
    def _winner_id_from_prize(self, pid: int) -> str:
        with db() as conn:
            c = conn.cursor()
            c.execute("SELECT winner_id FROM prizes WHERE id=?", (pid,))
            row = c.fetchone()
            return row[0] if row else ""

    # ---- Pretty round labels (per channel) ----
    @staticmethod
    def _round_counter_key(channel_id: int) -> str:
        return f"rcount:{channel_id}"

    @staticmethod
    def _round_label_key(rid: str) -> str:
        return f"rlabel:{rid}"

    @staticmethod
    def next_round_number(channel_id: int) -> int:
        cur = int(get_state(ClaimView._round_counter_key(channel_id)) or 0)
        cur += 1
        set_state(ClaimView._round_counter_key(channel_id), str(cur))
        return cur

    @staticmethod
    def set_round_label(rid: str, label: str):
        set_state(ClaimView._round_label_key(rid), label)

    @staticmethod
    def get_round_label(rid: str) -> str:
        return get_state(ClaimView._round_label_key(rid)) or rid

    @staticmethod
    def short_seed(s: str, n: int = 6) -> str:
        return f"{s[:n]}…{s[-n:]}" if s and len(s) > 2 * n else (s or "")

    # ---- Claim button ----
    @discord.ui.button(label="Claim WL Gifts", style=discord.ButtonStyle.primary)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_state(_prize_msg_key(self.prize_id), str(interaction.message.id))

        if str(interaction.user.id) != self._winner_id_from_prize(self.prize_id):
            return await interaction.response.send_message("Only the winner can claim this prize.", ephemeral=True)

        try:
            await interaction.message.edit(view=DisabledClaimView())
        except Exception:
            pass

        await interaction.response.send_modal(ClaimModal(self.prize_id))

class ClaimModal(discord.ui.Modal, title="Claim WL Gifts"):
    handle_or_url = discord.ui.TextInput(
        label="IMVU Username OR Profile URL",
        placeholder="e.g. YaEli   OR   https://www.imvu.com/…",
        required=True,
        max_length=200
    )
    note = discord.ui.TextInput(
        label="Notes (optional)",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=200,
        placeholder="Anything staff should know"
    )

    def __init__(self, prize_id: int):
        super().__init__()
        self.prize_id = prize_id

    def _extract_username(self, text: str):
        """Returns (username, profile_url, wishlist_url)"""
        raw = (text or "").strip()
        if not raw:
            return None, None, None
        if raw.startswith(("http://", "https://")):
            url = raw
            import urllib.parse as _u
            # we tried to parse a pasted URL above and set: url, p = urlparse(raw), etc.
            try:
                p = urlparse(raw)
                q = parse_qs(p.query)
                uname = (
                    (q.get("nick", [None])[0])
                    or (q.get("avatar_name", [None])[0])
                    or (q.get("user", [None])[0])
                    or (p.path.strip("/").split("/")[-1] or None)
                )
                # normalise username token
                if uname:
                    uname = re.sub(r"[^A-Za-z0-9_-]", "", uname)
            except Exception:
                uname = None
            
            # build canonical links
            if uname:
                profile_url  = f"https://www.imvu.com/catalog/web_profile.php?nick={uname}"
                wishlist_url = f"https://www.imvu.com/catalog/web_wishlist.php?nick={uname}"
            else:
                # fallback: they typed a URL we couldn't parse, or some random text
                uname = raw
                if re.fullmatch(r"[A-Za-z0-9_-]{2,32}", uname):
                    profile_url  = f"https://www.imvu.com/catalog/web_profile.php?nick={uname}"
                    wishlist_url = f"https://www.imvu.com/catalog/web_wishlist.php?nick={uname}"
                else:
                    profile_url  = raw   # keep whatever they pasted
                    wishlist_url = None
            
            return uname, profile_url, wishlist_url


    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)

        existing_ticket_id = get_state(_prize_ticket_key(self.prize_id))
        if existing_ticket_id:
            ch = interaction.guild.get_channel(int(existing_ticket_id))
            if ch:
                return await interaction.response.send_message(f"You already opened a ticket: {ch.mention}", ephemeral=True)

        uname, profile_url, wishlist_url = self._extract_username(str(self.handle_or_url))
        if not uname:
            return await interaction.response.send_message("Please enter a valid IMVU username or profile link.", ephemeral=True)

        with db() as conn:
            c = conn.cursor()
            c.execute("""INSERT INTO prize_queue(prize_id,winner_id,imvu_name,imvu_profile,note,status,created_ts,updated_ts)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (self.prize_id, uid, uname, wishlist_url or profile_url or "", str(self.note or ""),
                       "ready", iso(now_local()), iso(now_local())))
            c.execute("UPDATE prizes SET status='claimed', updated_ts=? WHERE id=?", (iso(now_local()), self.prize_id))

        cat = await _get_or_create_tickets_category(interaction.guild)
        if not cat:
            return await interaction.response.send_message("Could not create a ticket channel. Please ping an admin.", ephemeral=True)

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
        }
        if TICKETS_STAFF_ROLE_ID:
            role = interaction.guild.get_role(TICKETS_STAFF_ROLE_ID)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

        ticket_name = f"wl-{interaction.user.name[:16].lower()}-{self.prize_id}"
        ticket = await interaction.guild.create_text_channel(ticket_name, category=cat, overwrites=overwrites, reason="EliHaus WL claim ticket")
        set_state(_prize_ticket_key(self.prize_id), str(ticket.id))

        staff_tag = f"<@&{TICKETS_STAFF_ROLE_ID}>" if TICKETS_STAFF_ROLE_ID else "@here"
        profile_line = f"[{uname}]({profile_url})" if profile_url else uname
        wishlist_line = f"[Open Wishlist]({wishlist_url})" if wishlist_url else "—"
        policy = (
            f"**Policy:** To claim your winnings, you must have **10 items** added from **[Shop YaEli]({SHOP_YAELI_URL})**. "
            f"Failure to comply is subject to **disqualification**."
        )

        # --- announce + staff tag in the TICKET channel (you already have this)
        await ticket.send(
            f"{staff_tag} New WL claim for {interaction.user.mention}\n"
            f"**IMVU:** {profile_line}\n"
            f"**Wishlist:** {wishlist_line}\n"
            f"**Notes:** {self.note or '—'}\n"
            f"{policy}"
        )
        
        # --- approval embed + buttons (send to the TICKET, not interaction.channel)
        embed = discord.Embed(
            title="Withdraw Request • Pending Approval",
            description=(
                f"**Requester:** {interaction.user.mention}\n"
                f"**Coins to convert:** `{self.amount}`\n"
                f"**IMVU:** {profile_line}\n"
                f"**Wishlist:** {wishlist_line}"
            ),
            colour=discord.Colour.gold()
        )
        embed.set_footer(text="Use the buttons below to Approve or Reject")
        
        view = WithdrawApprovalView(
            requester_id=interaction.user.id,
            amount=self.amount,
            imvu_url=self.profile_url or "",
            note=self.note or ""
        )
        
        try:
            await ticket.send(embed=embed, view=view)
        except Exception as e:
            await interaction.response.send_message(
                f"Bot couldn’t post in the ticket. Please allow **Send Messages / Embed Links** for the bot here.\nError: `{e}`",
                ephemeral=True
            )
            return
        
        # --- acknowledge the modal exactly once
        try:
            await interaction.response.send_message(f"✅ Ticket created: {ticket.mention}", ephemeral=True)
        except discord.InteractionResponded:
            await interaction.followup.send(f"✅ Ticket created: {ticket.mention}", ephemeral=True)


# ===== Roulette Betting UI — Colour + Number Modals (mobile-friendly) =====
# Uses: db(), now_local(), iso(), get_balance(uid), ONE_BET_PER_ROUND, MAX_STAKE,
#       RED_NUMBERS (set), PAYOUT_RED_BLACK, PAYOUT_GREEN

# If missing, define the red set once near your config:
# RED_NUMBERS   = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
# BLACK_NUMBERS = {2,4,6,8,10,11,13,15,17,20,22,24,26,28,29,31,33,35}

def _round_open_and_timeleft(rid: str) -> int:
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT status, expires_at FROM rounds WHERE rid=?", (rid,))
        row = c.fetchone()
    if not row or row[0] != "OPEN":
        return 0
    try:
        exp = datetime.fromisoformat(row[1])
    except Exception:
        exp = now_local()
    return max(0, int((exp - now_local()).total_seconds()))

def _has_existing_bet(rid: str, uid: str) -> tuple[bool, tuple[str,int] | None]:
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT choice, stake FROM bets WHERE rid=? AND discord_id=? LIMIT 1", (rid, uid))
        r = c.fetchone()
    return (r is not None, (r[0], r[1]) if r else None)

def _deduct(uid: str, amount: int):
    # Use the central helper so all logging & checks stay consistent
    change_balance(uid, -amount, "bet", "roulette")

def _insert_bet(rid: str, channel_id: int, uid: str, choice: str, stake: int):
    # take the coins first
    _deduct(uid, stake)

    with db() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO bets(rid,channel_id,discord_id,choice,stake,ts) VALUES(?,?,?,?,?,?)",
            (rid, str(channel_id), uid, choice, stake, iso(now_local()))
        )


# ===== Roulette bet limits =====
ROUL_MIN_BET = int(os.getenv("ROUL_MIN_BET", "100"))
ROUL_MAX_BET = int(os.getenv("ROUL_MAX_BET", "1000000000"))

# ===== Bet modals (use ROUL_MIN_BET / ROUL_MAX_BET) =====

class RedBetModal(discord.ui.Modal, title="Bet: RED"):
    stake = discord.ui.TextInput(
        label="Stake (coins)", placeholder="enter amount", required=True, max_length=10
    )
    def __init__(self, rid: int):
        super().__init__(timeout=180)
        self.rid = rid
        self.stake.placeholder = f"Min {ROUL_MIN_BET}, Max {ROUL_MAX_BET}"

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amt = int(str(self.stake).strip())
        except Exception:
            return await interaction.response.send_message("Enter a valid number of coins.", ephemeral=True)

        if not (ROUL_MIN_BET <= amt <= ROUL_MAX_BET):
            return await interaction.response.send_message(
                f"Stake must be between {ROUL_MIN_BET} and {ROUL_MAX_BET}.", ephemeral=True
            )

        uid = str(interaction.user.id)
        bal = get_balance(uid)
        if bal < amt:
            return await interaction.response.send_message(
                f"Insufficient coins. Need **{amt}**, you have **{bal}**.", ephemeral=True
            )

        # 🔻 DEDUCT + INSERT BET
        _insert_bet(str(self.rid), interaction.channel.id, uid, "RED", amt)

        return await interaction.response.send_message(
            f"🟥 Bet placed: **RED** — **{amt}** coins.", ephemeral=True
        )


class BlackBetModal(discord.ui.Modal, title="Bet: BLACK"):
    stake = discord.ui.TextInput(label="Stake (coins)", placeholder="enter amount", required=True, max_length=10)
    def __init__(self, rid: int):
        super().__init__(timeout=180)
        self.rid = rid
        self.stake.placeholder = f"Min {ROUL_MIN_BET}, Max {ROUL_MAX_BET}"

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amt = int(str(self.stake).strip())
        except Exception:
            return await interaction.response.send_message("Enter a valid number of coins.", ephemeral=True)

        if not (ROUL_MIN_BET <= amt <= ROUL_MAX_BET):
            return await interaction.response.send_message(
                f"Stake must be between {ROUL_MIN_BET} and {ROUL_MAX_BET}.", ephemeral=True
            )

        uid = str(interaction.user.id)
        bal = get_balance(uid)
        if bal < amt:
            return await interaction.response.send_message(
                f"Insufficient coins. Need **{amt}**, you have **{bal}**.", ephemeral=True
            )

        # 🔻 DEDUCT + INSERT BET
        _insert_bet(str(self.rid), interaction.channel.id, uid, "BLACK", amt)

        return await interaction.response.send_message(
            f"⬛ Bet placed: **BLACK** — **{amt}** coins.", ephemeral=True
        )

class GreenBetModal(discord.ui.Modal, title="Bet: GREEN"):
    stake = discord.ui.TextInput(label="Stake (coins)", placeholder="enter amount", required=True, max_length=10)
    def __init__(self, rid: int):
        super().__init__(timeout=180)
        self.rid = rid
        self.stake.placeholder = f"Min {ROUL_MIN_BET}, Max {ROUL_MAX_BET}"

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amt = int(str(self.stake).strip())
        except Exception:
            return await interaction.response.send_message("Enter a valid number of coins.", ephemeral=True)

        if not (ROUL_MIN_BET <= amt <= ROUL_MAX_BET):
            return await interaction.response.send_message(
                f"Stake must be between {ROUL_MIN_BET} and {ROUL_MAX_BET}.", ephemeral=True
            )

        uid = str(interaction.user.id)
        bal = get_balance(uid)
        if bal < amt:
            return await interaction.response.send_message(
                f"Insufficient coins. Need **{amt}**, you have **{bal}**.", ephemeral=True
            )

        # 🔻 DEDUCT + INSERT BET
        _insert_bet(str(self.rid), interaction.channel.id, uid, "GREEN", amt)

        return await interaction.response.send_message(
            f"🟩 Bet placed: **GREEN** — **{amt}** coins.", ephemeral=True
        )

class NumberBetModal(discord.ui.Modal, title="Bet: NUMBER"):
    number = discord.ui.TextInput(label="Number (0–36)", placeholder="e.g., 17", required=True, max_length=2)
    stake  = discord.ui.TextInput(label="Stake (coins)", placeholder="enter amount", required=True, max_length=10)
    def __init__(self, rid: int):
        super().__init__(timeout=180)
        self.rid = rid
        self.stake.placeholder = f"Min {ROUL_MIN_BET}, Max {ROUL_MAX_BET}"

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n   = int(str(self.number).strip())
            amt = int(str(self.stake).strip())
        except Exception:
            return await interaction.response.send_message("Enter valid number and stake.", ephemeral=True)

        if not (0 <= n <= 36):
            return await interaction.response.send_message("Number must be between 0 and 36.", ephemeral=True)

        if not (ROUL_MIN_BET <= amt <= ROUL_MAX_BET):
            return await interaction.response.send_message(
                f"Stake must be between {ROUL_MIN_BET} and {ROUL_MAX_BET}.", ephemeral=True
            )

        uid = str(interaction.user.id)
        bal = get_balance(uid)
        if bal < amt:
            return await interaction.response.send_message(
                f"Insufficient coins. Need **{amt}**, you have **{bal}**.", ephemeral=True
            )

        # 🔻 DEDUCT + INSERT BET
        _insert_bet(str(self.rid), interaction.channel.id, uid, f"NUM:{n}", amt)

        return await interaction.response.send_message(
            f"🎲 Bet placed: **#{n}** — **{amt}** coins.", ephemeral=True
        )

    @discord.ui.button(label="My Bet", style=discord.ButtonStyle.secondary,emoji="❓", custom_id="eh_roul_mybet")
    async def my_bet(self, itx: discord.Interaction, _: discord.ui.Button):
        await itx.response.defer(ephemeral=True, thinking=True)   # add this line
    
        uid = str(itx.user.id)
        with db() as conn:
            c = conn.cursor()
            c.execute("SELECT choice, stake FROM bets WHERE rid=? AND discord_id=? LIMIT 1", (self.rid, uid))
            row = c.fetchone()
    
        bal = get_balance(uid)
    
        if not row:
            await itx.followup.send(f"You have **no bet** this round.\nBalance: **{bal}**", ephemeral=True)
            return
    
        choice, stake = row
        await itx.followup.send(f"🎲 Your bet: **{choice}** — **{stake}** coins.\nBalance: **{bal}**", ephemeral=True)

class AdminApproveWithdrawModal(discord.ui.Modal, title="Approve WL Withdraw"):
    coins = discord.ui.TextInput(
        label="Confirm coins to deduct",
        placeholder="e.g. 20000",
        required=True,
        max_length=12
    )
    note = discord.ui.TextInput(
        label="Internal note (optional)",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=200
    )

    def __init__(self, request_id: int):
        super().__init__(timeout=180)
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction):
        if not _is_admin_member(interaction.guild, interaction.user):
            return await interaction.response.send_message("You don’t have permission to approve.", ephemeral=True)

        # load request
        with db() as conn:
            c = conn.cursor()
            c.execute("""SELECT discord_id, coins, gifts, status, ticket_channel_id, message_id, imvu_name, imvu_profile
                         FROM withdraw_requests WHERE id=?""", (self.request_id,))
            row = c.fetchone()
        if not row:
            return await interaction.response.send_message("Request not found.", ephemeral=True)

        uid, coins_req, gifts_req, status, tchid, mid, uname, prof = row
        if status != "pending":
            return await interaction.response.send_message(f"Request is already **{status}**.", ephemeral=True)

        # parse confirmed coins
        try:
            coins_final = int(str(self.coins).strip().replace("_",""))
        except Exception:
            return await interaction.response.send_message("Enter a valid coin amount.", ephemeral=True)
        if coins_final <= 0 or coins_final % WL_COINS_PER_GIFT != 0:
            return await interaction.response.send_message(
                f"Amount must be a positive multiple of **{WL_COINS_PER_GIFT}**.", ephemeral=True
            )
        gifts_final = coins_final // WL_COINS_PER_GIFT
        if gifts_final < MIN_WL_GIFTS or gifts_final > MAX_WL_GIFTS:
            return await interaction.response.send_message(
                f"Gift count must be between **{MIN_WL_GIFTS}** and **{MAX_WL_GIFTS}**.", ephemeral=True
            )

        # balance check at approval time
        bal = get_balance(uid)
        if bal < coins_final:
            return await interaction.response.send_message(
                f"User balance changed. Needs **{coins_final}**, has **{bal}**. Adjust and try again.", ephemeral=True
            )

        # deduct & create prize + queue
        with db() as conn:
            c = conn.cursor()
            # deduct
            c.execute("UPDATE users SET balance=balance-? WHERE discord_id=?", (coins_final, uid))
            c.execute("INSERT INTO tx(discord_id,kind,amount,meta,ts) VALUES(?,?,?,?,?)",
                      (uid, "adjust", -coins_final, f"withdraw_to_wl:{gifts_final} gifts", iso(now_local())))
            # prize + queue
            c.execute("""INSERT INTO prizes(winner_id,kind,amount,meta,status,created_ts,updated_ts)
                         VALUES(?,?,?,?,?,?,?)""",
                      (uid, "wl", gifts_final, json.dumps({"shop": SHOP_NAME, "source": "user_withdraw"}), "pending",
                       iso(now_local()), iso(now_local())))
            prize_id = c.lastrowid
            c.execute("""INSERT INTO prize_queue(prize_id,winner_id,imvu_name,imvu_profile,note,status,created_ts,updated_ts)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (prize_id, uid, uname, prof or "", str(self.note or ""), "ready", iso(now_local()), iso(now_local())))
            # mark request
            c.execute("""UPDATE withdraw_requests SET status='approved', reviewer_id=?, review_note=?, coins=?, gifts=?, updated_ts=?
                         WHERE id=?""",
                      (str(interaction.user.id), str(self.note or ""), coins_final, gifts_final, iso(now_local()), self.request_id))

        # update the ticket message (disable buttons)
        try:
            channel = interaction.guild.get_channel(int(tchid)) if tchid else None
            if channel and mid:
                msg = await channel.fetch_message(int(mid))
                if msg.embeds:
                    e = msg.embeds[0]
                else:
                    e = discord.Embed(color=discord.Color.gold())
                e.add_field(name="Status", value=f"✅ **Approved** by {interaction.user.mention}\n"
                                                 f"Coins: {coins_final} → WL: {gifts_final}", inline=False)
                await msg.edit(embed=e, view=DisabledReviewView())
        except Exception:
            pass

        await interaction.response.send_message("Approved and deducted. Prize queued for fulfilment. ✅", ephemeral=True)

class AdminRejectWithdrawModal(discord.ui.Modal, title="Reject WL Withdraw"):
    reason = discord.ui.TextInput(label="Reason (shown to user)", required=True, max_length=200)

    def __init__(self, request_id: int):
        super().__init__(timeout=180)
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction):
        if not _is_admin_member(interaction.guild, interaction.user):
            return await interaction.response.send_message("You don’t have permission to reject.", ephemeral=True)

        with db() as conn:
            c = conn.cursor()
            c.execute("""SELECT ticket_channel_id, message_id, status FROM withdraw_requests WHERE id=?""",
                      (self.request_id,))
            row = c.fetchone()
        if not row:
            return await interaction.response.send_message("Request not found.", ephemeral=True)
        tchid, mid, status = row
        if status != "pending":
            return await interaction.response.send_message(f"Request is already **{status}**.", ephemeral=True)

        with db() as conn:
            c = conn.cursor()
            c.execute("""UPDATE withdraw_requests SET status='rejected', reviewer_id=?, review_note=?, updated_ts=?
                         WHERE id=?""",
                      (str(interaction.user.id), str(self.reason), iso(now_local()), self.request_id))

        try:
            channel = interaction.guild.get_channel(int(tchid)) if tchid else None
            if channel and mid:
                msg = await channel.fetch_message(int(mid))
                if msg.embeds:
                    e = msg.embeds[0]
                else:
                    e = discord.Embed(color=discord.Color.gold())
                e.add_field(name="Status", value=f"❌ **Rejected** by {interaction.user.mention}\n"
                                                 f"Reason: {str(self.reason)}", inline=False)
                await msg.edit(embed=e, view=DisabledReviewView())
        except Exception:
            pass

        await interaction.response.send_message("Rejected and left balance unchanged. ❌", ephemeral=True)

class DisabledReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for label, style in [("Approved", discord.ButtonStyle.success),
                             ("Rejected", discord.ButtonStyle.danger)]:
            self.add_item(discord.ui.Button(label=label, style=style, disabled=True))

class AdminWithdrawReviewView(discord.ui.View):
    def __init__(self, request_id: int):
        super().__init__(timeout=None)
        self.request_id = request_id

    @discord.ui.button(label="Approve & Deduct", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_admin_member(interaction.guild, interaction.user):
            return await interaction.response.send_message("You don’t have permission.", ephemeral=True)
        await interaction.response.send_modal(AdminApproveWithdrawModal(self.request_id))

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, emoji="🛑")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_admin_member(interaction.guild, interaction.user):
            return await interaction.response.send_message("You don’t have permission.", ephemeral=True)
        await interaction.response.send_modal(AdminRejectWithdrawModal(self.request_id))

# ---------------- Roulette core ----------------
def open_round(channel_id: int, seconds: int, opener_id: str) -> tuple[str, datetime]:
    rid = f"{channel_id}-{int(now_local().timestamp())}"
    expires = now_local() + timedelta(seconds=max(5, seconds))
    with db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO rounds(rid,channel_id,status,opened_by,opened_at,expires_at) VALUES(?,?,?,?,?,?)",
                  (rid, str(channel_id), "OPEN", opener_id, iso(now_local()), iso(expires)))
    set_state(round_key(channel_id), rid)
    return rid, expires

def get_open_round(channel_id: int):
    rk = round_key(channel_id)
    rid = get_state(rk)
    if not rid:
        return None
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT status, expires_at FROM rounds WHERE rid=? LIMIT 1", (rid,))
        row = c.fetchone()
    if not row:
        set_state(rk, None)
        return None
    status, exp = row
    try:
        exp_dt = datetime.fromisoformat(exp)
    except Exception:
        exp_dt = now_local()
    if status != "OPEN" or now_local() > exp_dt:
        set_state(rk, None)
        return None
    return rid, exp_dt
    
def get_open_or_last_round(channel_id: int):
    """Return the current open round, or the latest OPEN row even if the timer already elapsed."""
    rk = round_key(channel_id)
    rid = get_state(rk)
    if rid:
        with db() as conn:
            c = conn.cursor()
            c.execute("SELECT status, expires_at FROM rounds WHERE rid=? LIMIT 1", (rid,))
            row = c.fetchone()
        if row and row[0] == "OPEN":
            try:
                return rid, datetime.fromisoformat(row[1])
            except Exception:
                return rid, now_local()

    # Fallback: latest OPEN round in DB for this channel
    with db() as conn:
        c = conn.cursor()
        c.execute("""SELECT rid, expires_at
                     FROM rounds
                     WHERE channel_id=? AND status='OPEN'
                     ORDER BY opened_at DESC LIMIT 1""", (str(channel_id),))
        row = c.fetchone()
    if row:
        try:
            return row[0], datetime.fromisoformat(row[1])
        except Exception:
            return row[0], now_local()
    return None
async def _bump_round_message(channel, rid: str):
    # read latest totals + the old message id
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT message_id, expires_at FROM rounds WHERE rid=?", (rid,))
        row = c.fetchone()
        if not row or not row[0]:
            return
        old_id, exp_iso = row
        c.execute("SELECT COUNT(*), COALESCE(SUM(stake),0) FROM bets WHERE rid=?", (rid,))
        cnt, pool = c.fetchone()
        c.execute("""SELECT discord_id, choice, stake
                     FROM bets WHERE rid=?
                     ORDER BY ts DESC LIMIT 10""", (rid,))
        last_rows = c.fetchall()

    try:
        exp_dt = datetime.fromisoformat(exp_iso)
    except Exception:
        exp_dt = now_local()
    remain = max(0, int((exp_dt - now_local()).total_seconds()))
    if remain <= 0:
        return  # don't bump if already ended

    # rebuild the embed (same style as your main one)
    e = discord.Embed(
        title=f"🎯 Roulette — Round {ClaimView.get_round_label(rid)}",
        description="Click a button to bet. A modal will ask your amount.",
        color=discord.Color.gold()
    )
    e.add_field(name="Pool", value=str(pool), inline=True)
    e.add_field(name="Time", value=f"{remain}s left", inline=True)
    e.add_field(name="Bets", value=str(cnt), inline=True)

    lines = []
    guild = getattr(channel, "guild", None)
    for uid, ch, st in last_rows:
        m = guild.get_member(int(uid)) if guild else None
        name = m.mention if m else f"<@{uid}>"
        lines.append(f"{name} · {st} on {ch.upper()}")
    e.add_field(name="Players (latest)", value=("\n".join(lines) if lines else "—"), inline=False)

    # send a fresh message with fresh buttons so users can keep betting
    view = RouletteBetView(rid, timeout=remain + 30)
    new_msg = await channel.send(embed=e, view=view)

    # update DB to the new message id
    with db() as conn:
        conn.execute("UPDATE rounds SET message_id=? WHERE rid=?", (str(new_msg.id), rid))

    # try to delete the old one to reduce clutter (requires 'Manage Messages')
    try:
        old_msg = await channel.fetch_message(int(old_id))
        await old_msg.delete()
    except Exception:
        pass

# ---------------- Slash Commands (eh_*) ----------------
def ensure_user(uid: str):
    with db() as conn:
        c = conn.cursor()
        c.execute("""INSERT OR IGNORE INTO users(discord_id,balance,last_daily,last_weekly,joined_at)
                     VALUES(?,?,?,?,?)""", (uid, 0, None, None, iso(now_local())))

def get_balance(uid: str) -> int:
    ensure_user(uid)
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT balance FROM users WHERE discord_id=?", (uid,))
        row = c.fetchone()
        return row[0] if row else 0

ALLOWED_TX_KINDS = {"claim", "bet", "payout", "redeem", "lotto", "starter", "wl_deposit"}

def change_balance(uid: str, delta: int, kind: str, meta: str = "") -> int:
    if kind not in ALLOWED_TX_KINDS:
        raise ValueError(f"Balance change blocked for kind='{kind}'.")
    ensure_user(uid)
    with db() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET balance=balance+? WHERE discord_id=?", (delta, uid))
        c.execute("INSERT INTO tx(discord_id,kind,amount,meta,ts) VALUES(?,?,?,?,?)",
                  (uid, kind, delta, meta, iso(now_local())))
        c.execute("SELECT balance FROM users WHERE discord_id=?", (uid,))
        return c.fetchone()[0]

# Track which channels already have an open dice party
DICE_PARTY_CHANNELS: set[int] = set()


class DicePartyView(discord.ui.View):
    def __init__(self, host_id: int, stake: int, channel_id: int, max_players: int = 10):
        super().__init__(timeout=None)
        self.host_id = host_id
        self.stake = stake
        self.channel_id = channel_id
        self.max_players = max(2, min(max_players, 20))
        # store user IDs
        self.players: list[int] = [host_id]
        self.game_id = f"{channel_id}-{int(now_local().timestamp())}"
        self.started = False

    def _is_admin(self, member: discord.Member) -> bool:
        return (
            getattr(member.guild_permissions, "manage_guild", False)
            or member.id == getattr(member.guild, "owner_id", 0)
        )

    def _can_control(self, user: discord.Member) -> bool:
        return user.id == self.host_id or self._is_admin(user)

    def _player_list_text(self, guild: discord.Guild | None) -> str:
        if not self.players:
            return "—"
        names = []
        for uid in self.players:
            m = guild.get_member(uid) if guild else None
            names.append(m.mention if m else f"<@{uid}>")
        return "\n".join(f"{i+1}. {name}" for i, name in enumerate(names))

    async def _update_message(self, interaction: discord.Interaction):
        """Refresh the lobby embed on the original message."""
        try:
            msg = interaction.message
            e = msg.embeds[0] if msg.embeds else discord.Embed(colour=discord.Colour.gold())
        except Exception:
            return

        e.title = "🎲 EliHaus Dice Party"
        e.description = (
            f"Stake: **{self.stake}** coins per player\n"
            f"Players: **{len(self.players)}/{self.max_players}**\n\n"
            "Click **Join** to enter. Host presses **Start** to roll."
        )
        e.clear_fields()
        e.add_field(name="Players", value=self._player_list_text(interaction.guild), inline=False)
        await msg.edit(embed=e, view=self)

    # --- Buttons ---

    @discord.ui.button(label="Join", style=discord.ButtonStyle.primary, emoji="🙋")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.started:
            return await interaction.response.send_message(
                "This party has already started.", ephemeral=True
            )

        user = interaction.user
        if user.bot:
            return await interaction.response.send_message(
                "Bots can’t join, sorry.", ephemeral=True
            )

        if user.id in self.players:
            return await interaction.response.send_message(
                "You’re already in this dice party.", ephemeral=True
            )

        if len(self.players) >= self.max_players:
            return await interaction.response.send_message(
                f"Party is full (**{self.max_players}** players).", ephemeral=True
            )

        uid = str(user.id)
        ensure_user(uid)
        bal = get_balance(uid)
        if bal < self.stake:
            return await interaction.response.send_message(
                f"You need at least **{self.stake}** coins to join. "
                f"You currently have **{bal}**.",
                ephemeral=True
            )

        self.players.append(user.id)
        await interaction.response.defer(ephemeral=True, thinking=False)
        await self._update_message(interaction)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success, emoji="🚀")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.started:
            return await interaction.response.send_message(
                "This party has already been played.", ephemeral=True
            )

        if not self._can_control(interaction.user):
            return await interaction.response.send_message(
                "Only the host or an admin can start this party.",
                ephemeral=True
            )

        if len(self.players) < 2:
            return await interaction.response.send_message(
                "Need at least **2** players to start.", ephemeral=True
            )

        # Re-check balances and drop anyone who can’t afford now
        valid_players: list[int] = []
        removed: list[int] = []
        for uid_int in self.players:
            uid = str(uid_int)
            bal = get_balance(uid)
            if bal >= self.stake:
                valid_players.append(uid_int)
            else:
                removed.append(uid_int)

        self.players = valid_players

        if len(self.players) < 2:
            # nothing deducted yet, so safe to bail
            return await interaction.response.send_message(
                "After balance check there are fewer than 2 eligible players. "
                "Party cancelled.",
                ephemeral=True
            )

        # Lock the game
        self.started = True
        DICE_PARTY_CHANNELS.discard(self.channel_id)

        # Deduct stake from all valid players
        pot = 0
        for uid_int in self.players:
            uid = str(uid_int)
            change_balance(
                uid,
                -self.stake,
                "bet",
                meta=f"dice_party:{self.game_id}"
            )
            pot += self.stake

        # Roll dice
        rolls: dict[int, int] = {}
        for uid_int in self.players:
            rolls[uid_int] = random.randint(1, 6)

        max_roll = max(rolls.values())
        winners = [uid for uid, r in rolls.items() if r == max_roll]

        # Split pot between winners
        share = pot // len(winners)
        leftover = pot - share * len(winners)

        for i, uid_int in enumerate(winners):
            payout = share + (leftover if i == 0 else 0)
            uid = str(uid_int)
            change_balance(
                uid,
                payout,
                "payout",
                meta=f"dice_party_win:{self.game_id}"
            )

        # Build result embed
        lines = []
        guild = interaction.guild
        for uid_int, r in rolls.items():
            m = guild.get_member(uid_int) if guild else None
            name = m.mention if m else f"<@{uid_int}>"
            mark = "🏆" if uid_int in winners else " "
            lines.append(f"{mark} {name} rolled **{r}**")

        desc = "\n".join(lines)
        if len(winners) == 1:
            w_member = guild.get_member(winners[0]) if guild else None
            w_name = w_member.mention if w_member else f"<@{winners[0]}>"
            result_line = f"\n\n🏆 **{w_name} wins** the pot of **{pot}** coins!"
        else:
            winner_mentions = []
            for uid_int in winners:
                m = guild.get_member(uid_int) if guild else None
                winner_mentions.append(m.mention if m else f"<@{uid_int}>")
            result_line = (
                f"\n\n🤝 Tie on **{max_roll}**. "
                f"{', '.join(winner_mentions)} share the pot (**{pot}** total)."
            )

        embed = discord.Embed(
            title="🎲 EliHaus Dice Party — Result",
            description=desc + result_line,
            colour=discord.Colour.gold(),
            timestamp=now_local()
        )
        embed.add_field(name="Stake per player", value=str(self.stake), inline=True)
        embed.add_field(name="Players", value=str(len(self.players)), inline=True)
        embed.add_field(name="Pot", value=str(pot), inline=True)
        embed.set_footer(text="EliHaus Dice Party")

        # Disable buttons
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.InteractionResponded:
            await interaction.followup.send(embed=embed, ephemeral=False)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="🛑")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.started:
            return await interaction.response.send_message(
                "This party has already been played; you can’t cancel it now.",
                ephemeral=True
            )

        if not self._can_control(interaction.user):
            return await interaction.response.send_message(
                "Only the host or an admin can cancel this party.",
                ephemeral=True
            )

        self.started = True
        DICE_PARTY_CHANNELS.discard(self.channel_id)

        # Disable buttons
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        try:
            msg = interaction.message
            e = msg.embeds[0] if msg.embeds else discord.Embed(colour=discord.Colour.dark_grey())
            e.title = "🎲 EliHaus Dice Party — Cancelled"
            e.description = "The host cancelled this dice party. No coins were taken."
            e.clear_fields()
            await msg.edit(embed=e, view=self)
        except Exception:
            pass

        await interaction.response.send_message("Dice party cancelled.", ephemeral=True)


@bot.tree.command(
    name="eh_dice_party",
    description="Start a multi-player dice party (everyone stakes coins; highest roll wins)"
)
@app_commands.describe(
    stake="Coins each player must stake",
    max_players="Max number of players (including you, default 10, max 20)"
)
async def eh_dice_party(
    interaction: discord.Interaction,
    stake: int,
    max_players: int = 10
):
    if stake <= 0:
        return await interaction.response.send_message(
            "Stake must be a positive number of coins.",
            ephemeral=True
        )

    ch_id = interaction.channel.id
    if ch_id in DICE_PARTY_CHANNELS:
        return await interaction.response.send_message(
            "There’s already an open dice party in this channel. Finish or cancel it first.",
            ephemeral=True
        )

    host = interaction.user
    uid_host = str(host.id)
    ensure_user(uid_host)
    bal_host = get_balance(uid_host)
    if bal_host < stake:
        return await interaction.response.send_message(
            f"You need at least **{stake}** coins to host a party. "
            f"You currently have **{bal_host}**.",
            ephemeral=True
        )

    DICE_PARTY_CHANNELS.add(ch_id)

    view = DicePartyView(
        host_id=host.id,
        stake=stake,
        channel_id=ch_id,
        max_players=max_players
    )

    # Initial lobby embed
    e = discord.Embed(
        title="🎲 EliHaus Dice Party",
        description=(
            f"Stake: **{stake}** coins per player\n"
            f"Players: **1/{view.max_players}**\n\n"
            "Click **Join** to enter. Host presses **Start** to roll."
        ),
        colour=discord.Colour.gold(),
        timestamp=now_local()
    )
    e.add_field(name="Players", value=host.mention, inline=False)
    e.set_footer(text="Everyone stakes; highest roll wins the pot.")

    await interaction.response.send_message(embed=e, view=view)


# ---- Help (slash) ----
@bot.tree.command(name="withdraw_wl", description="Convert your coins to WL gifts (opens a ticket; admin approves)")
async def withdraw_wl(interaction: discord.Interaction):
    await interaction.response.send_modal(WithdrawWLModal())

@bot.tree.command(name="eh_help", description="Show EliHaus commands")
async def eh_help(interaction: discord.Interaction):
    is_admin = user_is_admin(interaction.user)

    public_lines = [
        "🟡 **Core coins**",
        "`/eh_join` – join EliHaus (starter coins)",
        "`/eh_daily` – claim daily coins",
        "`/eh_weekly` – claim weekly coins",
        "`/eh_balance` – check balance",

        "",
        "🎰 **Games**",
        "`/eh_buyticket` – buy lotto tickets",
        "`/eh_lotto` – see lotto status",
        "`/eh_dice_duel` – 1v1 dice duel (stake coins vs someone)",
        "`/eh_dice_party` – group dice game (everyone stakes; highest roll wins)",
        "`/slots_panel` – jump link to the Slots panel",

        "",
        "🎁 **Prizes / WL**",
        "`/eh_withdraw` – request WL gifts using your coins",
        "`/eh_policy` – view EliHaus prize / claim policy",
        "`/eh_leaderboard` – view top balances / roulette net",
    ]

    admin_lines = [
        "🎛️ **Admin only**",
        "`/eh_openround` – open roulette round",

        "`/eh_drawlotto` – draw weekly lotto winner",
        "`/slots_open` – open Slots panel in this channel",
        "`/eh_policy_edit` – edit policy shop / min items",
        "`/eh_deposit` – owner-only manual coin deposit",
        # keep /eh_sync available but not advertised unless you want it:
        # "`/eh_sync` – re-sync slash commands (debug)",
    ]

    embed = discord.Embed(
        title="EliHaus Commands",
        colour=discord.Colour.gold(),
    )

    embed.add_field(
        name="Public",
        value="\n".join(public_lines),
        inline=False,
    )

    if is_admin:
        embed.add_field(
            name="Admin",
            value="\n".join(admin_lines),
            inline=False,
        )

    embed.set_footer(text="Use /eh_policy to read how prizes & WL claims work.")

    await interaction.response.send_message(embed=embed, ephemeral=True)

# ========= Roulette single-card helpers =========
import asyncio

async def _edit_round_message(bot, channel, rid: int, embed: discord.Embed, view: discord.ui.View | None):
    """
    Edit the ONE official roulette message for this round (rid).
    Falls back to sending a new one only if the original was deleted,
    and then records the new message_id in the 'rounds' table.
    """
    # fetch saved message_id
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT message_id FROM rounds WHERE rid=?", (rid,))
        row = c.fetchone()
    msg_id = int(row[0]) if row and row[0] else None

    # resolve channel object if an id was passed
    ch = channel
    if isinstance(channel, int):
        ch = bot.get_channel(channel) or await bot.fetch_channel(channel)

    if msg_id:
        try:
            msg = await ch.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view)
            return
        except Exception:
            pass  # original message missing or cannot be edited

    # fallback: send new, then save id
    new_msg = await ch.send(embed=embed, view=view)
    with db() as conn:
        c = conn.cursor()
        c.execute("UPDATE rounds SET message_id=? WHERE rid=?", (str(new_msg.id), rid))


def _round_stats(rid: int) -> tuple[int, int]:
    """
    Returns (bets_count, pool_sum) for a round from 'bets' table.
    Safe even if there are no bets.
    """
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*), COALESCE(SUM(stake), 0) FROM bets WHERE rid=?", (rid,))
        row = c.fetchone() or (0, 0)
    count = int(row[0] or 0)
    pool = int(row[1] or 0)
    return count, pool

import asyncio
ROUND_TICK_SECONDS = 5
ROUND_TASKS: dict[str, asyncio.Task] = {}

def _choice_label(choice: str) -> str:
    """Pretty label for a stored choice."""
    ch = str(choice).upper()
    if ch.startswith("NUM:"):
        return f"#{ch.split(':', 1)[1]}"
    if ch in ("RED", "BLACK", "GREEN"):
        # same 🎯 icon everywhere for consistency
        return {"RED": "🟥", "BLACK": "⬛", "GREEN": "🟩"}[ch]
    return ch

def _latest_bets_lines(channel, rid: int, limit: int = 6) -> str:
    """Return 'mention: LABEL — amount' lines for the latest bets in this round."""
    rows: list[tuple[str, str, int]] = []
    with db() as conn:
        c = conn.cursor()
        # Works on SQLite even without an explicit timestamp (uses ROWID order)
        c.execute("SELECT discord_id, choice, stake FROM bets WHERE rid=? ORDER BY ROWID DESC LIMIT ?", (rid, limit))
        rows = c.fetchall() or []

    lines = []
    guild = getattr(channel, "guild", None)
    for uid, choice, stake in rows:
        who = f"<@{uid}>"
        if guild:
            m = guild.get_member(int(uid))
            if m:
                who = m.mention
        lines.append(f"{who}: {_choice_label(choice)} — **{int(stake)}**")
    return "\n".join(lines) if lines else "—"


        
# ---- robust countdown that always resolves ----
import asyncio
from datetime import datetime, timezone

async def tick_round(channel, rid: int, exp_iso: str):
    """
    Edits the ONE round panel with a countdown and auto-resolves when time is up.
    Never crashes the task; always removes buttons at the end.
    """
    # parse expiry safely
    try:
        exp = datetime.fromisoformat(exp_iso)
        if exp.tzinfo is None:
            # make it local if your app uses TZ, else fall back to UTC
            exp = (now_local() if 'now_local' in globals() else datetime.now(timezone.utc)).astimezone().tzinfo \
                  and exp.replace(tzinfo=(now_local().tzinfo if 'now_local' in globals() else timezone.utc)) \
                  or exp.replace(tzinfo=timezone.utc)
    except Exception:
        # if parsing fails, end immediately
        exp = (now_local() if 'now_local' in globals() else datetime.now(timezone.utc))

    while True:
        try:
            now = now_local() if 'now_local' in globals() else datetime.now(timezone.utc)
            left = max(0, int((exp - now).total_seconds()))

            # stats
            with db() as conn:
                c = conn.cursor()
                c.execute("SELECT COUNT(*), COALESCE(SUM(stake),0) FROM bets WHERE rid=?", (rid,))
                cnt, pool = c.fetchone() or (0, 0)

            # betting panel (gold)
            open_embed = discord.Embed(
                title=f"🎰 Roulette — Round {ClaimView.get_round_label(rid)}",
                description="Click to bet.",
                color=discord.Color.gold()
            )
            open_embed.add_field(name="Pool", value=str(pool), inline=True)
            open_embed.add_field(name="Time", value=f"{left}s left", inline=True)
            open_embed.add_field(name="Bets", value=str(cnt), inline=True)
            players_txt = _latest_bets_lines(channel, rid, limit=10)
            open_embed.add_field(name="Players (latest)", value=players_txt, inline=False)

            view = RouletteBetView(rid, timeout=left + 30)
            await _edit_round_message(bot, channel, rid, open_embed, view=view)

            if left <= 0:
                break

            await asyncio.sleep(5)
        except Exception:
            # never let a bug kill the countdown
            await asyncio.sleep(5)

          # ----- compute outcome (result_color/result_number already set or roll now) -----
    try:
        result_color, result_number = resolve_spin_result(rid)
    except Exception:
        import random
        n = random.randint(0, 36)
        result_number = n
        result_color = "GREEN" if n == 0 else ("RED" if _outcome_from_spin(n) == "red" else "BLACK")
    
    outcome = result_color.lower()
    
    # Totals + winners (sum payouts if same user has multiple winning bets)
    total_bets = 0
    total_pool = 0
    winners_map: dict[str, int] = {}
    
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT discord_id, choice, stake FROM bets WHERE rid=?", (rid,))
        rows = c.fetchall() or []
        for uid, choice, stake in rows:
            total_bets += 1
            total_pool += int(stake)
    
            ch = str(choice).upper()
            win = 0
            if ch in ("RED","BLACK") and ch.lower() == outcome:
                win = int(stake * PAYOUT_RED_BLACK)
            elif ch == "GREEN" and outcome == "green":
                win = int(stake * PAYOUT_GREEN)
            elif ch.startswith("NUM:"):
                num = int(ch.split(":",1)[1])
                if num == int(result_number):
                    win = int(stake * PAYOUT_NUMBER)
    
            if win > 0:
                winners_map[uid] = winners_map.get(uid, 0) + win
                # credit users (optional if you already do this elsewhere)
                c.execute("UPDATE users SET balance=balance+? WHERE discord_id=?", (win, uid))
                c.execute("INSERT INTO tx(discord_id,kind,amount,meta,ts) VALUES(?,?,?,?,?)",
                          (uid, "payout", win, f"roulette:{rid}|{outcome}:{result_number}", iso(now_local())))
    
    winners_list = list(winners_map.items())
    winners_txt = _format_winners_lines(getattr(channel, "guild", None), winners_list, limit=5)
    
    # Colored result embed
    pill = {"RED":"🔴 **RED**","BLACK":"⬛ **BLACK**","GREEN":"🟩 **GREEN**"}[str(result_color).upper()]
    col_map = {"RED":(220,38,38), "BLACK":(24,24,27), "GREEN":(16,185,129)}
    r,g,b = col_map.get(str(result_color).upper(), (24,24,27))
    
    result_embed = discord.Embed(
        title=f"🎰 EliHaus Roulette — Round {ClaimView.get_round_label(rid)}",
        colour=discord.Colour.from_rgb(r,g,b)
    )
    result_embed.add_field(name="RESULT", value=f"{pill} · **#{int(result_number)}**", inline=False)
    result_embed.add_field(name="Total Bets", value=str(total_bets), inline=True)
    result_embed.add_field(name="Pool", value=str(total_pool), inline=True)
    result_embed.add_field(name="Winners (top)", value=winners_txt, inline=False)

    # 2) render the numbered chip and SEND a new result message
    badge = render_chip_badge(str(result_color).upper(), int(result_number))  # uses assets/chip_*.png
    
    # ------------------------------------------------------------
    # remove buttons and show result (close panel, then send result)
    # ------------------------------------------------------------
    try:
        # 1) close the original betting panel (remove buttons)
        await _edit_round_message(
            bot, channel, rid,
            discord.Embed(
                title=f"🎰 Roulette — Round {ClaimView.get_round_label(rid)}",
                description="Round closed.",
                colour=discord.Colour.dark_grey()
            ),
            view=None
        )
    
        # 2) render the numbered chip and SEND a new result message
        badge = render_chip_badge(str(result_color).upper(), int(result_number))  # uses assets/chip_*.png
        if badge:
            fname = f"chip_{str(result_color).lower()}_{int(result_number)}.png"
            file = discord.File(badge, filename=fname)
            result_embed.set_image(url=f"attachment://{fname}")
            await channel.send(embed=result_embed, file=file)
        else:
            # fallback if Pillow/assets missing
            await channel.send(embed=result_embed)
    
    except Exception:
        # even if something above fails, make sure buttons are gone
        try:
            await _edit_round_message(
                bot, channel, rid,
                discord.Embed(
                    title=f"🎰 EliHaus Roulette — Round {ClaimView.get_round_label(rid)}",
                    description="Round closed.",
                    colour=discord.Colour.dark_grey()
                ),
                view=None
            )
        except Exception:
            pass

    
@bot.tree.command(name="eh_join", description="Join EliHaus and get starter coins")
async def eh_join(interaction: discord.Interaction):
    await safe_ack(interaction, ephemeral=True)  # prevents "The application did not respond"

    try:
        uid = str(interaction.user.id)
        ensure_user(uid)

        # already claimed starter?
        with db() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM tx WHERE discord_id=? AND kind='starter' LIMIT 1", (uid,))
            has_starter = c.fetchone() is not None

        if has_starter:
            return await safe_followup(
                interaction,
                "You’ve already joined EliHaus. Use `/eh_daily` and `/eh_weekly` to build coins.",
                True,
            )

        new_bal = change_balance(uid, STARTER_AMOUNT, "starter", "joinhaus starter")

        return await safe_followup(
            interaction,
            f"Welcome to **EliHaus**. Starter pack: **{STARTER_AMOUNT}** coins. Balance: **{new_bal}**",
            True,
        )

    except Exception as e:
        return await safe_followup(interaction, f"❌ Error: `{e}`", True)

@bot.tree.command(name="eh_daily", description="Claim your daily coins")
async def eh_daily(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    ensure_user(uid)
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT last_daily FROM users WHERE discord_id=?", (uid,))
        row = c.fetchone()
        last = datetime.fromisoformat(row[0]).astimezone(TZ) if row and row[0] else None
        now = now_local()
        if last and (now - last) < timedelta(hours=24):
            left = timedelta(hours=24) - (now - last)
            hrs = int(left.total_seconds() // 3600)
            mins = int((left.total_seconds() % 3600) // 60)
            return await interaction.response.send_message(f"You’ve already claimed. Try again in **{hrs}h {mins}m**.", ephemeral=True)
        new_bal = change_balance(uid, DAILY_AMOUNT, "claim", "daily")
        c.execute("UPDATE users SET last_daily=? WHERE discord_id=?", (iso(now), uid))
    await interaction.response.send_message(f"Daily claimed: **{DAILY_AMOUNT}** coins. New balance: **{new_bal}**", ephemeral=True)

@bot.tree.command(name="eh_weekly", description="Claim your weekly coins")
async def eh_weekly(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    ensure_user(uid)
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT last_weekly FROM users WHERE discord_id=?", (uid,))
        row = c.fetchone()
        nowt = now_local()
        last = datetime.fromisoformat(row[0]).astimezone(TZ) if row and row[0] else None
        if last and (last.isocalendar()[:2] == nowt.isocalendar()[:2]):
            return await interaction.response.send_message("You’ve already claimed your weekly this week.", ephemeral=True)
        new_bal = change_balance(uid, WEEKLY_AMOUNT, "claim", "weekly")
        c.execute("UPDATE users SET last_weekly=? WHERE discord_id=?", (iso(nowt), uid))
    await interaction.response.send_message(f"Weekly claimed: **{WEEKLY_AMOUNT}** coins. New balance: **{new_bal}**", ephemeral=True)

@bot.tree.command(name="eh_balance", description="Check your coin balance (or someone else's)")
@app_commands.describe(user="(optional) member to check")
async def eh_balance(interaction: discord.Interaction, user: discord.Member | None = None):
    target = user or interaction.user
    uid = str(target.id)          
    ensure_user(uid)             

    try:
        bal = get_balance(uid)
    except Exception as e:
        import traceback; print("eh_balance error:\n", traceback.format_exc())
        return await interaction.response.send_message("❌ Couldn't fetch balance.", ephemeral=True)

    # self check = ephemeral
    if user is None:
        await interaction.response.send_message(f"💰 Your balance: **{bal}** coins.", ephemeral=True)
    else:
        await interaction.response.send_message(f"💰 {target.mention} balance: **{bal}** coins.")
   
    # create a private ticket for this user + staff
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
    }
    if TICKETS_STAFF_ROLE_ID:
        role = interaction.guild.get_role(TICKETS_STAFF_ROLE_ID)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

    ticket_name = f"wl-deposit-{interaction.user.name[:16].lower()}-{int(now_local().timestamp())}"
    ticket = await interaction.guild.create_text_channel(ticket_name, category=cat, overwrites=overwrites, reason="EliHaus WL deposit")

    # post details in the ticket
    staff_tag = f"<@&{TICKETS_STAFF_ROLE_ID}>" if TICKETS_STAFF_ROLE_ID else "@here"
    e = discord.Embed(
        title="💳 WL Conversion Request",
        description=f"{interaction.user.mention} deposited **{amount}** coins to convert to wishlist gifts.",
        color=discord.Color.gold(),
        timestamp=now_local()
    )
    e.add_field(name="IMVU", value=imvu, inline=False)
    e.add_field(name="Notes", value=(note or "—"), inline=False)
    e.add_field(name="New Balance", value=str(new_bal), inline=True)

    await ticket.send(content=staff_tag, embed=e)

    # confirm to the user
    await interaction.response.send_message(
        f"✅ Deposited **{amount}** coins. Ticket created: {ticket.mention}\n"
        f"Balance: **{bal} ➜ {new_bal}**",
        ephemeral=True
    )
    
def _result_color(outcome: str) -> discord.Colour:
    m = {"red": (220, 38, 38), "black": (24, 24, 27), "green": (16, 185, 129)}
    r, g, b = m.get(str(outcome).lower(), (24, 24, 27))
    return discord.Colour.from_rgb(r, g, b)
def build_roulette_result_embed(*, rlabel: str, outcome: str, roll: int,
                                total_bets: int, total_pool: int,
                                winners_mentions: list[str], seed_display: str) -> discord.Embed:
    pill = {"red": "🔴 **RED**", "black": "⬛ **BLACK**", "green": "🟩 **GREEN**"}.get(outcome.lower(), "⬛ **BLACK**")
    e = discord.Embed(title=f"🎰 EliHaus Roulette — Round {rlabel}",
                      colour=_result_color(outcome))
    e.add_field(name="RESULT", value=f"{pill} · **#{roll}**", inline=False)
    e.add_field(name="Total Bets", value=str(total_bets), inline=True)
    e.add_field(name="Pool", value=str(total_pool), inline=True)
    e.add_field(name="Winners (top)", value=("• " + "\n• ".join(winners_mentions)) if winners_mentions else "—", inline=False)
    e.set_footer(text=f"Seed: {seed_display}")
    return e

#dice====
@bot.tree.command(name="eh_dice_duel", description="Peer-to-peer dice duel for coins")
@app_commands.describe(
    opponent="Who you want to duel",
    stake="Coins each player stakes (both pay this amount)"
)
async def eh_dice_duel(
    interaction: discord.Interaction,
    opponent: discord.Member,
    stake: int
):
    challenger = interaction.user

    # basic checks
    if opponent.bot:
        return await interaction.response.send_message(
            "You can’t duel a bot, love.",
            ephemeral=True
        )
    if opponent.id == challenger.id:
        return await interaction.response.send_message(
            "You can’t duel yourself. Go play roulette for that.",
            ephemeral=True
        )
    if stake <= 0:
        return await interaction.response.send_message(
            "Stake must be a positive number of coins.",
            ephemeral=True
        )

    uid_chal = str(challenger.id)
    uid_opp  = str(opponent.id)

    ensure_user(uid_chal)
    ensure_user(uid_opp)

    bal_chal = get_balance(uid_chal)
    bal_opp  = get_balance(uid_opp)

    if bal_chal < stake:
        return await interaction.response.send_message(
            f"You don’t have enough coins. Need **{stake}**, you have **{bal_chal}**.",
            ephemeral=True
        )
    if bal_opp < stake:
        return await interaction.response.send_message(
            f"{opponent.mention} doesn’t have enough coins to play.",
            ephemeral=True
        )

    # we’re good – charge both first
    change_balance(
        uid_chal,
        -stake,
        "bet",
        meta=f"dice_duel:vs:{uid_opp}"
    )
    change_balance(
        uid_opp,
        -stake,
        "bet",
        meta=f"dice_duel:vs:{uid_chal}"
    )

    # roll dice
    roll_chal = random.randint(1, 6)
    roll_opp  = random.randint(1, 6)

    # decide outcome
    desc_lines = [
        f"{challenger.mention} rolled **{roll_chal}** 🎲",
        f"{opponent.mention} rolled **{roll_opp}** 🎲",
        ""
    ]

    if roll_chal > roll_opp:
        # challenger wins full pot (2 x stake)
        pot = 2 * stake
        change_balance(
            uid_chal,
            pot,
            "payout",
            meta=f"dice_duel_win:vs:{uid_opp}"
        )
        desc_lines.append(
            f"🏆 **{challenger.mention} wins** the pot of **{pot}** coins!"
        )
    elif roll_opp > roll_chal:
        # opponent wins full pot
        pot = 2 * stake
        change_balance(
            uid_opp,
            pot,
            "payout",
            meta=f"dice_duel_win:vs:{uid_chal}"
        )
        desc_lines.append(
            f"🏆 **{opponent.mention} wins** the pot of **{pot}** coins!"
        )
    else:
        # tie – refund stakes
        change_balance(
            uid_chal,
            stake,
            "payout",
            meta="dice_duel_refund"
        )
        change_balance(
            uid_opp,
            stake,
            "payout",
            meta="dice_duel_refund"
        )
        desc_lines.append(
            "🤝 It’s a tie – stakes refunded to both players."
        )

    embed = discord.Embed(
        title="🎲 EliHaus Dice Duel",
        description="\n".join(desc_lines),
        colour=discord.Colour.gold(),
        timestamp=now_local()
    )
    embed.set_footer(text="EliHaus Dice • peer-to-peer")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="eh_openround", description="(Admin) Open a roulette round")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(seconds="Betting window (10–600s)")
async def eh_openround(interaction: discord.Interaction, seconds: int = ROUND_SECONDS_DEFAULT):
    await safe_ack(interaction, ephemeral=True)  

    try:
        # --- existing perm checks ---
        if not user_is_admin(interaction.user):
            return await safe_followup(interaction, "You don’t have permission.", True)

        seconds = max(10, min(seconds, 600))
        if get_open_round(interaction.channel.id):
            return await safe_followup(interaction, "There’s already an open round in this channel.", True)

        rid, exp = open_round(interaction.channel.id, seconds, str(interaction.user.id))

        # build the opening panel
        rnum = ClaimView.next_round_number(interaction.channel.id)
        rlabel = f"#{rnum}"
        ClaimView.set_round_label(rid, rlabel)

        embed = discord.Embed(
            title=f"🎰 Roulette — Round {ClaimView.get_round_label(rid)}",
            description="Click to bet.",
            color=discord.Color.gold()
        )
        embed.add_field(name="Pool", value="0", inline=True)
        embed.add_field(name="Time", value=f"{seconds}s left", inline=True)
        embed.add_field(name="Bets", value="0", inline=True)

        view = RouletteBetView(rid, timeout=seconds + 30)
        msg = await interaction.channel.send(embed=embed, view=view)
        with db() as conn:
            conn.execute("UPDATE rounds SET message_id=? WHERE rid=?", (str(msg.id), rid))

        # launch ticker as a background task (never block the command)
        try:
            ROUND_TASKS[rid] = bot.loop.create_task(tick_round(interaction.channel, rid, iso(exp)))
        except Exception:
            pass

        return await safe_followup(interaction, f"✅ Opened roulette round {rlabel}.", True)

    except Exception as e:
        # show error to caller so it never silently “doesn’t respond”
        return await safe_followup(interaction, f"❌ Error opening round: `{e}`", True)

  
@bot.tree.command(name="eh_table", description="Show current roulette round status in this channel")
async def eh_table(interaction: discord.Interaction):
    o = get_open_round(interaction.channel.id)
    if not o:
        return await interaction.response.send_message("No open round in this channel.", ephemeral=True)
    rid, exp = o
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*), COALESCE(SUM(stake),0) FROM bets WHERE rid=?", (rid,))
        cnt, pool = c.fetchone()
    remain = max(0, int((exp - now_local()).total_seconds()))
    await interaction.response.send_message(
        f"Round **{ClaimView.get_round_label(rid)}** — Bets: **{cnt}** | Pool: **{pool}** | Time left: **{remain}s**",
        ephemeral=True
    )

@bot.event
async def on_message(message: discord.Message):
    # ignore bots + DMs
    if message.author.bot or message.guild is None:
        return

    # count only real chat messages
    MESSAGE_COUNTER[message.channel.id] += 1

    # bump threshold (change 15 to what you like)
    if MESSAGE_COUNTER[message.channel.id] >= 15:
        MESSAGE_COUNTER[message.channel.id] = 0
        mid = ACTIVE_PANEL_MSG.get(message.channel.id)
        if mid:
            jump = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{mid}"
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Jump to Betting Panel", url=jump))

            bump = discord.Embed(
                title="🎰 Roulette — current round",
                description="Chat flew past. Click below to jump to the betting panel.",
                colour=discord.Colour.dark_grey()
            )
            await message.channel.send(embed=bump, view=view)

    # keep commands working if you use commands.Bot
    try:
        await bot.process_commands(message)
    except Exception:
        pass
async def _roulette_send_result(
    bot, channel, *, rid: int,
    result_color: str,           # "RED" | "BLACK" | "GREEN"
    result_number: int,
    winners_top: list[tuple[str,int]],  # [(discord_id, payout), ...]
    pool_sum: int,
    image_path: str | None = None  # e.g. "assets/roulette/result_red.png"
):
    color_map = {"RED": (220,38,38), "BLACK": (24,24,27), "GREEN": (16,185,129)}
    pill_text = {"RED":"🔴 **RED**","BLACK":"⬛ **BLACK**","GREEN":"🟩 **GREEN**"}

    ck = str(result_color).upper()
    rgb = color_map.get(ck, (24,24,27))
    pill = pill_text.get(ck, "⬛ **BLACK**")

    # winners list (top 5)
    def _format_winners(guild: discord.Guild, winners: list[tuple[str,int]], limit: int = 5) -> str:
        if not winners: return "—"
        winners = sorted(winners, key=lambda x: x[1], reverse=True)[:limit]
        lines = []
        for uid, amount in winners:
            m = guild.get_member(int(uid))
            who = m.mention if m else f"<@{uid}>"
            lines.append(f"{who} — **{amount}**")
        return "\n".join(lines)

    e = discord.Embed(
        title=f"🎰 EliHaus Roulette — Round #{rid}",
        colour=discord.Colour.from_rgb(*rgb),
    )
    e.add_field(name="RESULT", value=f"{pill} · **#{int(result_number)}**", inline=False)
    e.add_field(name="Pool", value=str(pool_sum), inline=True)
    e.add_field(name="Winners (top)", value=_format_winners(channel.guild, winners_top), inline=False)
    e.set_footer(text=f"ROUL-{rid} • {now_local().strftime('%b %d, %H:%M')}")

    # If you want a mockup image, attach it as a separate public message (editing a pin with a new file is awkward).
    if image_path:
        try:
            file = discord.File(image_path)
            await channel.send(embed=e, file=file)
        except Exception:
            # if the file is missing, still update the pinned panel without image
            edit_round_message(bot, channel, rid, e, view=None)
            return

    # Update your pinned/live round panel (no image here)
    edit_round_message(bot, channel, rid, e, view=None)

@bot.tree.command(name="eh_resolve", description="(Admin) Resolve the current roulette round")
@app_commands.default_permissions(manage_guild=True)
async def eh_resolve(interaction: discord.Interaction):
    if not user_is_admin(interaction.user):
        return await interaction.response.send_message("You don’t have permission.", ephemeral=True)
        
    o = get_open_or_last_round(interaction.channel.id)
    if not o:
        return await interaction.response.send_message("No round found to resolve in this channel.", ephemeral=True)
    rid, _exp = o

    # roll an outcome with a reproducible seed
    seed = f"ROUL-{rid}-{int(now_local().timestamp())}-{random.randint(1, 1_000_000)}"
    random.seed(seed)
    roll = random.randint(0, 36)  # 0 = green
    red_nums = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    if roll == 0:
        outcome = "green"
        multiplier = PAYOUT_GREEN
    else:
        outcome = "red" if roll in red_nums else "black"
        multiplier = PAYOUT_RED_BLACK

    # settle
    total_pool = 0
    winners = []
    rows = []
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT discord_id, choice, stake FROM bets WHERE rid=?", (rid,))
        rows = c.fetchall()
        for uid, ch, stake in rows:
            total_pool += stake
        for uid, ch, stake in rows:
            if ch == outcome:
                win = int(stake * multiplier)
                c.execute("UPDATE users SET balance=balance+? WHERE discord_id=?", (win, uid))
                c.execute("INSERT INTO tx(discord_id,kind,amount,meta,ts) VALUES(?,?,?,?,?)",
                          (uid, "payout", win, f"roulette:{rid}|{outcome}", iso(now_local())))
                winners.append((uid, win))
        c.execute("UPDATE rounds SET status='RESOLVED', outcome=?, seed=?, resolved_at=? WHERE rid=?",
                  (f"{outcome}:{roll}", seed, iso(now_local()), rid))
    set_state(round_key(interaction.channel.id), None)

    # update the original embed (remove buttons)
    msg_id = None
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT message_id FROM rounds WHERE rid=?", (rid,))
        r = c.fetchone()
        if r and r[0]:
            msg_id = int(r[0])

    rlabel = ClaimView.get_round_label(rid)
    seed_display = ClaimView.short_seed(seed, 8)
    top_mentions = []
    for uid, _win in sorted(winners, key=lambda x: x[1], reverse=True)[:5]:
        m = interaction.guild.get_member(int(uid))
        top_mentions.append(m.mention if m else f"<@{uid}>")

    # Top winners as (discord_id, payout) for the helper
    winners_top = [(str(uid), int(amount)) for uid, amount in winners][:5]
    pool_sum = total_pool  # whatever var you use for the round pool
    
    await _roulette_send_result(
        bot,
        interaction.channel,    # or `channel` if you already have it
        rid=rid,
        result_color=str(outcome).upper(),  # "RED"/"BLACK"/"GREEN"
        result_number=int(roll),
        winners_top=winners_top,
        pool_sum=int(pool_sum),
        # Optional image mockup:
        # image_path=f"assets/roulette/result_{str(outcome).lower()}.png"
    )

    #await interaction.channel.send(embed=result_embed)
    # Ephemeral confirmation (optional)
    sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    await sender("Round resolved.", ephemeral=True)



@bot.tree.command(name="eh_cancelround", description="(Admin) Cancel the current roulette round and refund")
@app_commands.default_permissions(manage_guild=True)
async def eh_cancelround(interaction: discord.Interaction):
    if not user_is_admin(interaction.user):
        return await interaction.response.send_message("You don’t have permission.", ephemeral=True)
    o = get_open_round(interaction.channel.id)
    if not o:
        return await interaction.response.send_message("No open round to cancel.", ephemeral=True)
    rid, _ = o
    set_state(round_key(interaction.channel.id), None)
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT discord_id, stake FROM bets WHERE rid=?", (rid,))
        rows = c.fetchall()
        for uid, stake in rows:
            c.execute("UPDATE users SET balance=balance+? WHERE discord_id=?", (stake, uid))
            c.execute("INSERT INTO tx(discord_id,kind,amount,meta,ts) VALUES(?,?,?,?,?)",
                      (uid, "payout", stake, f"roulette:{rid}|refund", iso(now_local())))
        c.execute("UPDATE rounds SET status='CANCELLED', resolved_at=? WHERE rid=?", (iso(now_local()), rid))
    await interaction.response.send_message(f"Round **{ClaimView.get_round_label(rid)}** cancelled and bets refunded.", ephemeral=True)

# ---- Lotto ----
@bot.tree.command(name="eh_buyticket", description="Buy tickets for this week’s Lotto")
@app_commands.describe(count="How many tickets (1-100)")
async def eh_buyticket(interaction: discord.Interaction, count: int = 1):
    if count <= 0 or count > 100:
        return await interaction.response.send_message(
            "You can buy between 1 and 100 tickets at once.",
            ephemeral=True
        )

    uid = str(interaction.user.id)
    cost = TICKET_COST * count
    bal = get_balance(uid)

    if bal < cost:
        return await interaction.response.send_message(
            f"Not enough coins. Need **{cost}**, you have **{bal}**.",
            ephemeral=True
        )

    # 🔻 deduct using the central helper
    new_bal = change_balance(uid, -cost, "lotto", meta=f"tickets {count}")

    # record tickets for this week
    wk = week_id()
    with db() as conn:
        c = conn.cursor()
        for _ in range(count):
            c.execute(
                "INSERT INTO tickets(week_id,discord_id,ts) VALUES(?,?,?)",
                (wk, uid, iso(now_local()))
            )

    await interaction.response.send_message(
        f"🎟️ Bought **{count}** ticket(s) for this week’s Lotto. Good luck!\n"
        f"Balance: **{bal} ➜ {new_bal}**",
        ephemeral=True
    )

@bot.tree.command(name="eh_drawlotto", description="(Admin) Draw this week’s lotto")
@app_commands.default_permissions(manage_guild=True)
async def eh_drawlotto(interaction: discord.Interaction):
    if not user_is_admin(interaction.user):
        return await interaction.response.send_message("You don’t have permission.", ephemeral=True)
    wk = week_id()
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, discord_id FROM tickets WHERE week_id=?", (wk,))
        all_tix = c.fetchall()
    if not all_tix:
        return await interaction.response.send_message(f"No tickets for Week {wk}.", ephemeral=True)
    seed = f"LOTTO-{wk}-{int(now_local().timestamp())}-{random.randint(1, 1_000_000)}"
    random.seed(seed)
    winner_ticket = random.choice(all_tix)
    winner_id = winner_ticket[1]
    with db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO lotto_draws(week_id,run_at,winner_id,seed,status) VALUES(?,?,?,?,?)",
                  (wk, iso(now_local()), winner_id, seed, "DONE"))
        c.execute("""INSERT INTO prizes(winner_id,kind,amount,meta,status,created_ts,updated_ts)
                     VALUES(?,?,?,?,?,?,?)""",
                  (winner_id, "wl", LOTTO_WL_COUNT, json.dumps({"shop": SHOP_NAME, "week": wk}), "pending", iso(now_local()), iso(now_local())))
        prize_id = c.lastrowid
    member = interaction.guild.get_member(int(winner_id))
    mention = member.mention if member else f"<@{winner_id}>"
    embed = discord.Embed(
        title="🎉 Weekly Lotto Winner!",
        description=f"{mention} wins **{LOTTO_WL_COUNT}** wishlist gifts from **[{SHOP_NAME}]({SHOP_YAELI_URL})**.",
        color=discord.Color.gold()
    )
    # Post winner publicly with claim button, respond ephemeral to admin
    await interaction.channel.send(embed=embed, view=ClaimView(prize_id))
    await interaction.response.send_message("Winner posted.", ephemeral=True)


@bot.tree.command(name="eh_fulfil_done", description="(Admin) Mark a WL fulfilment done")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(queue_id="Queue ID from /eh_fulfil_next")
async def eh_fulfil_done(interaction: discord.Interaction, queue_id: int):
    if not user_is_admin(interaction.user):
        return await interaction.response.send_message("You don’t have permission.", ephemeral=True)
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT prize_id FROM prize_queue WHERE id=?", (queue_id,))
        row = c.fetchone()
        if not row:
            return await interaction.response.send_message("Queue ID not found.", ephemeral=True)
        prize_id = row[0]
        c.execute("UPDATE prize_queue SET status='fulfilled', updated_ts=? WHERE id=?", (iso(now_local()), queue_id))
        c.execute("UPDATE prizes SET status='fulfilled', updated_ts=? WHERE id=?", (iso(now_local()), prize_id))
    await interaction.response.send_message(f"Marked fulfilment queue **#{queue_id}** as fulfilled ✅", ephemeral=True)

# ---- Utilities ----
@bot.tree.command(name="eh_roundreset", description="(Admin) Force-unlock this channel if a round is stuck")
@app_commands.default_permissions(manage_guild=True)
async def eh_roundreset(interaction: discord.Interaction):
    if not user_is_admin(interaction.user):
        return await interaction.response.send_message("You don’t have permission.", ephemeral=True)
    rid = get_state(round_key(interaction.channel.id))
    if not rid:
        return await interaction.response.send_message("No open round to reset (state already clear).", ephemeral=True)
    with db() as conn:
        c = conn.cursor()
        c.execute("UPDATE rounds SET status='CANCELLED', resolved_at=? WHERE rid=?",
                  (iso(now_local()), rid))
    set_state(round_key(interaction.channel.id), None)
    await interaction.response.send_message(f"Force-reset round **{ClaimView.get_round_label(rid)}** — channel unlocked.", ephemeral=True)

# ---------------- Sync & Ready ----------------
@bot.event
async def on_ready():
    print(f"[EliHaus] Logged in as {bot.user} | TZ={TIMEZONE_NAME}")
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            await bot.tree.sync(guild=guild)
            print(f"[EliHaus] Slash commands synced to guild {GUILD_ID}")
        else:
            await bot.tree.sync()
            print("[EliHaus] Slash commands synced globally")
    except Exception as e:
        print(f"[EliHaus] Slash sync failed: {e}")

@bot.tree.command(name="eh_sync", description="(admin) Re-sync slash commands")
async def eh_sync(interaction: discord.Interaction):
    if not (interaction.user.guild_permissions.manage_guild or interaction.guild.owner_id == interaction.user.id):
        return await interaction.response.send_message("You don’t have permission.", ephemeral=True)

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        # Fast guild sync (instant)
        if interaction.guild:
            await bot.tree.sync(guild=interaction.guild)
        # Global sync (removes old globals)
        await bot.tree.sync()
        msg = "✅ Synced slash commands (guild + global)."
    except Exception as e:
        msg = f"❌ Sync error: {e!s}"
    await interaction.followup.send(msg, ephemeral=True)

@bot.event
async def on_message(message: discord.Message):
    # keep prefix commands working (even though we use slash now)
    await bot.process_commands(message)

    # ignore bots/DMs/system
    if message.author.bot or not message.guild:
        return
    if message.type != discord.MessageType.default:
        return

    # if there is an open round in this channel, count & bump
    o = get_open_round(message.channel.id)
    if not o:
        STICKY_COUNT.pop(message.channel.id, None)
        return

    rid, exp = o
    # don't bump if nearly done to avoid spammy last seconds
    if (exp - now_local()).total_seconds() <= 10:
        return

    STICKY_COUNT[message.channel.id] = STICKY_COUNT.get(message.channel.id, 0) + 1
    if STICKY_COUNT[message.channel.id] >= STICKY_AFTER_MSGS:
        STICKY_COUNT[message.channel.id] = 0
        try:
            await _bump_round_message(message.channel, rid)
        except Exception:
            pass
from datetime import timedelta

def _mention_or_id(guild: discord.Guild | None, uid: str) -> str:
    m = guild.get_member(int(uid)) if guild else None
    return m.mention if m else f"<@{uid}>"

@bot.tree.command(name="eh_leaderboard", description="Show top players by balance or roulette net")
@app_commands.describe(
    mode="balance (default), roulette_week, or roulette_all",
    public="Post in channel (True) or show only to you (False)"
)
async def eh_leaderboard(
    interaction: discord.Interaction,
    mode: str = "balance",
    public: bool = True
):
    # prevent 3s timeout
    await interaction.response.defer(ephemeral=not public, thinking=True)

    mode = (mode or "balance").lower().strip()
    guild = interaction.guild

    try:
        if mode == "balance":
            with db() as conn:
                c = conn.cursor()
                c.execute("SELECT discord_id, balance FROM users ORDER BY balance DESC LIMIT 10")
                rows = c.fetchall()
            title = "🏆 EliHaus Leaderboard — Balance"
            footer = "Top 10 richest players"
            items = [(_mention_or_id(guild, uid), bal) for uid, bal in rows]

        elif mode in ("roulette_week", "roulette_all"):
            # net = payouts − bets; bets are stored negative already
            q_time = ""
            params = ()
            if mode == "roulette_week":
                since = (now_local() - timedelta(days=7)).isoformat()
                q_time = "AND ts >= ?"
                params = (since,)

            with db() as conn:
                c = conn.cursor()
                c.execute(f"""
                    SELECT discord_id, COALESCE(SUM(amount),0) AS net
                    FROM tx
                    WHERE kind IN ('bet','payout') {q_time}
                    GROUP BY discord_id
                    HAVING net != 0
                    ORDER BY net DESC
                    LIMIT 10
                """, params)
                rows = c.fetchall()

            title = "🎰 Roulette Leaderboard — Weekly Net" if mode == "roulette_week" \
                    else "🎰 Roulette Leaderboard — All-Time Net"
            footer = "Net = payouts − bets"
            items = [(_mention_or_id(guild, uid), net) for uid, net in rows]

        else:
            await interaction.followup.send(
                "Unknown mode. Use `balance`, `roulette_week`, or `roulette_all`.",
                ephemeral=not public
            )
            return

        e = discord.Embed(title=title, color=discord.Color.gold(), timestamp=now_local())
        if not items:
            e.description = "_No data yet._"
        else:
            medals = ["🥇","🥈","🥉"]
            lines = []
            for i, (name, val) in enumerate(items, start=1):
                tag = medals[i-1] if i <= 3 else f"{i:>2}."
                lines.append(f"{tag} {name} — **{val:,}**")
            e.description = "\n".join(lines)
        e.set_footer(text=footer)

        await interaction.followup.send(embed=e, ephemeral=not public)

    except Exception as e:
        # surface the exact error to you ephemerally
        await interaction.followup.send(
            f"⚠️ Leaderboard error: `{type(e).__name__}: {e}`",
            ephemeral=True
        )

@bot.tree.command(name="eh_policy", description="Show the EliHaus prize/claim policy")
@app_commands.describe(public="Post in channel (True) or show only to you (False)")
async def eh_policy(interaction: discord.Interaction, public: bool = False):
    text = build_policy_text()
    e = discord.Embed(
        title="📜 EliHaus Policy",
        description=text,
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=e, ephemeral=not public)

# =========================
# 🎰 Emoji Slots (Shared Pot) — FULL ADD-ON
# =========================

# ---- Config ----
SLOTS_COST = int(os.getenv("SLOTS_COST", "500"))            # coins per spin
SLOTS_SEED = int(os.getenv("SLOTS_SEED", "1000"))           # minimum pot floor per channel
SLOTS_MAX_SPINS = int(os.getenv("SLOTS_MAX_SPINS", "5"))    # spins per modal submission
SLOTS_EMOJIS = ["🍒", "🍋", "🍇", "🍀", "⭐", "💎", "7️⃣"]

# payout rules (from the pot; pot never goes below seed)
SLOTS_PAYOUT_TRIPLE = float(os.getenv("SLOTS_PAYOUT_TRIPLE", "0.80"))  # 80% of (pot - seed)
SLOTS_PAYOUT_DOUBLE = int(os.getenv("SLOTS_PAYOUT_DOUBLE", "2000"))    # flat, capped by available

# ---- DB bootstrap (safe to call multiple times) ----
def _init_slots_tables():
    with db() as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS slots_spins(
            id INTEGER PRIMARY KEY,
            channel_id TEXT,
            discord_id TEXT,
            r1 TEXT, r2 TEXT, r3 TEXT,
            win INTEGER,            -- amount paid out
            pot_before INTEGER,     -- pot before paying win
            ts TEXT
        )""")

# call it once at import
_init_slots_tables()

# ---- State keys ----
def _slots_pot_key(channel_id: int) -> str:
    return f"slots:pot:{channel_id}"

def _slots_msg_key(channel_id: int) -> str:
    return f"slots:msg:{channel_id}"

# ---- Pot helpers ----
def _outcome_from_spin(n: int) -> str:
    red = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    if n == 0:
        return "green"
    return "red" if n in red else "black"

def _format_winners_lines(guild: discord.Guild, winners: list[tuple[str, int]], limit: int = 5) -> str:
    """
    winners: list of (discord_id, payout)
    Returns up to top N lines like '@User — 2500'.
    """
    if not winners:
        return "—"
    # sort by payout desc, then take top N
    winners = sorted(winners, key=lambda x: x[1], reverse=True)[:limit]
    lines = []
    for uid, amount in winners:
        m = guild.get_member(int(uid))
        who = m.mention if m else f"<@{uid}>"
        lines.append(f"{who} — **{amount}**")
    return "\n".join(lines)

     
def edit_round_message(bot, channel, rid: int, embed, view=None):
    """Safe from non-async code: schedules the edit on the loop."""
    return asyncio.create_task(_edit_round_message(bot, channel, rid, embed, view))

def set_slots_pot(channel_id: int, pot: int):
    # Pot can never fall below the configured seed
    set_state(_slots_pot_key(channel_id), str(max(pot, SLOTS_SEED)))

    

# ---- UI: Modal + View ----
class SlotsModal(discord.ui.Modal, title="Spin the Slots"):
    spins = discord.ui.TextInput(
        label=f"How many spins? (1–{SLOTS_MAX_SPINS})",
        placeholder="1",
        required=True,
        max_length=2
    )

    def __init__(self, channel_id: int):
        super().__init__(timeout=180)
        self.channel_id = channel_id

    
    # inside class SlotsModal(...)
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        sent = False
        try:
            # parse spins
            try:
                n = int(str(self.spins.value).strip())
            except Exception:
                await interaction.followup.send("Enter a valid number of spins (1–5).", ephemeral=True)
                return
            if n < 1 or n > SLOTS_MAX_SPINS:
                await interaction.followup.send(f"Spins must be between 1 and {SLOTS_MAX_SPINS}.", ephemeral=True)
                return
    
            # user + balance checks
            uid = str(interaction.user.id)
            ensure_user(uid)
    
            total_cost = SLOTS_COST * n
            bal = get_balance(uid)
            if bal < total_cost:
                await interaction.followup.send(
                    f"Insufficient coins. **{total_cost}** required for {n} spin(s). Balance **{bal}**.",
                    ephemeral=True
                )
                return
    
            # charge upfront
            with db() as conn:
                c = conn.cursor()
                c.execute("UPDATE users SET balance = balance - ? WHERE discord_id = ?", (total_cost, uid))
                c.execute(
                    "INSERT INTO tx(discord_id,kind,amount,meta,ts) VALUES(?,?,?,?,?)",
                    (uid, "bet", -total_cost, f"slots|entry x{n}", iso(now_local()))
                )
    
            # add to pot
            pot = get_slots_pot(self.channel_id) + total_cost
            set_slots_pot(self.channel_id, pot)
    
            # run spins
            total_win = 0
            lines = []
            last_roll = "-"
            last_win = 0
    
            for _ in range(n):
                r1 = random.choice(SLOTS_EMOJIS)
                r2 = random.choice(SLOTS_EMOJIS)
                r3 = random.choice(SLOTS_EMOJIS)
    
                available = max(0, pot - SLOTS_SEED)
                win = 0
                if r1 == r2 == r3:
                    win = int(available * SLOTS_PAYOUT_TRIPLE)
                elif (r1 == r2) or (r1 == r3) or (r2 == r3):
                    win = min(SLOTS_PAYOUT_DOUBLE, available)
    
                pot_before = pot
                if win > 0:
                    pot -= win
                    set_slots_pot(self.channel_id, pot)
                    total_win += win
    
                with db() as conn:
                    c = conn.cursor()
                    c.execute(
                        "INSERT INTO slots_spins(channel_id,discord_id,r1,r2,r3,win,pot_before,ts) VALUES(?,?,?,?,?,?,?,?)",
                        (str(self.channel_id), uid, r1, r2, r3, win, pot_before, iso(now_local()))
                    )
    
                sign = f"+{win}" if win else "–"
                lines.append(f"{r1} {r2} {r3}   {sign}")
                last_roll = f"{r1}{r2}{r3}"
                last_win = win
    
            # pay out bundle if any
            if total_win > 0:
                with db() as conn:
                    c = conn.cursor()
                    c.execute("UPDATE users SET balance = balance + ? WHERE discord_id = ?", (total_win, uid))
                    c.execute(
                        "INSERT INTO tx(discord_id,kind,amount,meta,ts) VALUES(?,?,?,?,?)",
                        (uid, "payout", total_win, f"slots|bundle x{n}", iso(now_local()))
                    )
    
            # refresh the panel (safe)
            try:
                mid = get_state(_slots_msg_key(self.channel_id))
                if mid:
                    panel = await interaction.channel.fetch_message(int(mid))
                    e = panel.embeds[0] if panel.embeds else discord.Embed(color=discord.Color.gold())
                    e.title = "🎰 Emoji Slots — Shared Pot"
                    e.clear_fields()
                    e.description = (
                        f"Entry: **{SLOTS_COST}** coins per spin.\n"
                        f"Triples pay **{int(SLOTS_PAYOUT_TRIPLE*100)}%** of available pot.\n"
                        f"Doubles pay **{SLOTS_PAYOUT_DOUBLE}**.\n"
                        f"Pot never drops below seed **{SLOTS_SEED}**."
                    )
                    e.add_field(name="Pot",  value=str(get_slots_pot(self.channel_id)), inline=True)
                    e.add_field(name="Seed", value=str(SLOTS_SEED), inline=True)
                    e.add_field(
                        name="Last roll",
                        value=f"{last_roll} {'+'+str(last_win) if last_win else '–'}",
                        inline=False
                    )
                    await panel.edit(embed=e, view=SlotsView(self.channel_id))
            except Exception as e:
                print("SLOTS panel refresh error:", e)
    
            # ephemeral summary (single send)
            show = 6
            body = "\n".join(lines[:show]) + (f"\n.. and {len(lines)-show} more." if len(lines) > show else "")
            await interaction.followup.send(
                f"**Spins:** {n}\n{body}\n\n**Total won:** {total_win}\n**Pot now:** {get_slots_pot(self.channel_id)}",
                ephemeral=True
            )
            sent = True
    
        except Exception:
            import traceback
            print("SLOTS on_submit fatal:\n", traceback.format_exc())
            if not sent:
                await interaction.followup.send("❌ Something went wrong running your spins.", ephemeral=True)

        # --- results & output ---
        try:
            # EPHEMERAL SUMMARY
            show = 6
            body = "\n".join(lines[:show]) + (f"\n.. and {len(lines)-show} more." if len(lines) > show else "")
            await interaction.followup.send(
                f"**Spins:** {n}\n{body}\n\n**Total won:** {total_win}\n**Pot now:** {get_slots_pot(self.channel_id)}",
                ephemeral=True
            )
            sent = True
        
            # PUBLIC attachment (optional)
            import io, time
            details = []
            details.append(f"User: {interaction.user} ({interaction.user.id})")
            details.append(f"Spins: {n}")
            details.extend(lines)
            details.append("")
            details.append(f"Total won: {total_win}")
            details.append(f"Pot now: {get_slots_pot(self.channel_id)}")
            txt = "\n".join(details)
        
            buf = io.BytesIO(txt.encode("utf-8"))
            filename = f"slots_{interaction.user.id}_{int(time.time())}.txt"
            file = discord.File(buf, filename=filename)
        
            public_summary = (
                f"{interaction.user.mention} spun **{n}x** • "
                f"{'+'+str(total_win) if total_win else 'no win'} • "
                f"Pot **{get_slots_pot(self.channel_id)}**"
            )
            await interaction.followup.send(public_summary, file=file)
        
        except Exception as e:
            import traceback
            print("SLOTS results send error:\n", traceback.format_exc())
            if not sent:
                await interaction.followup.send("❌ Something went wrong sending your result.", ephemeral=True)
                sent = True
        finally:
            if not sent:
                try:
                    await interaction.followup.send("✅ Done.", ephemeral=True)
                except Exception:
                    pass

        
class SlotsView(discord.ui.View):
    def __init__(self, channel_id: int, timeout: int | None = None):
        super().__init__(timeout=timeout or None)
        self.channel_id = channel_id

    @discord.ui.button(label="Spin 🎰", style=discord.ButtonStyle.primary)
    async def spin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SlotsModal(self.channel_id))

# ---- Slash Commands ----
# (admin) open a panel in the current channel
from discord import app_commands
import discord

# Optional: scope to your guild for instant updates (set GUILD_ID earlier)
# @app_commands.guilds(discord.Object(id=GUILD_ID))
@bot.tree.command(name="slots_open", description="(admin) Open a Shared-Pot Emoji Slots panel here")
async def slots_open(interaction: discord.Interaction):
    # basic admin gate
    if not (interaction.user.guild_permissions.manage_guild or interaction.guild.owner_id == interaction.user.id):
        return await interaction.response.send_message("You don’t have permission.", ephemeral=True)

    # 1) ACK within 3s so Discord doesn't expire the interaction
    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        pot = get_slots_pot(interaction.channel.id)

        e = discord.Embed(
            title="🎰 Emoji Slots — Shared Pot",
            description=(
                f"Entry: **{SLOTS_COST}** coins per spin.\n"
                f"Triples pay **{int(SLOTS_PAYOUT_TRIPLE*100)}%** of available pot.\n"
                f"Doubles pay **{SLOTS_PAYOUT_DOUBLE}**.\n"
                f"Pot never drops below seed **{SLOTS_SEED}**."
            ),
            color=discord.Color.gold()
        )
        e.add_field(name="Pot",  value=str(get_slots_pot(interaction.channel.id)), inline=True)
        e.add_field(name="Seed", value=str(SLOTS_SEED), inline=True)


        view = SlotsView(interaction.channel.id)
        msg = await interaction.channel.send(embed=e, view=view)

        set_state(_slots_msg_key(interaction.channel.id), str(msg.id))
        try:
            await msg.pin(reason="EliHaus Slots panel")
        except Exception:
            pass

        # 3) Final reply
        await interaction.followup.send("Slots panel posted.", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Failed to post panel: {e}", ephemeral=True)


# user: get a jump link to panel
@bot.tree.command(name="slots_panel", description="Get a jump link to the Slots panel")
async def slots_panel(interaction: discord.Interaction):
    mid = get_state(_slots_msg_key(interaction.channel.id))
    if not mid:
        return await interaction.response.send_message("No Slots panel in this channel.", ephemeral=True)
    url = f"https://discord.com/channels/{interaction.guild.id}/{interaction.channel.id}/{mid}"
    await interaction.response.send_message(f"⤵️ Jump to Slots panel:\n{url}", ephemeral=True)

# (admin) reset pot to seed & refresh the panel
@bot.tree.command(name="slots_reset", description="(admin) Reset the Slots pot to the seed amount")
async def slots_reset(interaction: discord.Interaction):
    if not (interaction.user.guild_permissions.manage_guild or interaction.guild.owner_id == interaction.user.id):
        return await interaction.response.send_message("You don’t have permission.", ephemeral=True)

    # set pot
    set_slots_pot(interaction.channel.id, SLOTS_SEED)

    # refresh the panel
    try:
        mid = get_state(_slots_msg_key(interaction.channel.id))
        if mid:
            panel = await interaction.channel.fetch_message(int(mid))
            # get (or build) embed
            if panel.embeds:
                e = panel.embeds[0]
            else:
                e = discord.Embed(color=discord.Color.gold())
                e.title = "🎰 Emoji Slots — Shared Pot"

            # (optional) pull last roll; safe fallback if none
            last_roll = "-"
            last_win = 0
            try:
                with db() as conn:
                    c = conn.cursor()
                    row = c.execute(
                        "SELECT r1,r2,r3,win FROM slots_spins WHERE channel_id=? ORDER BY ts DESC LIMIT 1",
                        (str(interaction.channel.id),)
                    ).fetchone()
                    if row:
                        r1, r2, r3, w = row
                        last_roll = f"{r1}{r2}{r3}"
                        last_win = int(w or 0)
            except Exception:
                pass  # ignore if table empty, etc.

            # rebuild embed
            e.clear_fields()
            e.description = (
                f"Entry: **{SLOTS_COST}** coins per spin.\n"
                f"Triples pay **{int(SLOTS_PAYOUT_TRIPLE*100)}%** of available pot.\n"
                f"Doubles pay **{SLOTS_PAYOUT_DOUBLE}**×.\n"
                f"Pot never drops below seed **{SLOTS_SEED}**."
            )
            e.add_field(name="Pot", value=str(get_slots_pot(interaction.channel.id)), inline=True)
            e.add_field(name="Seed", value=str(SLOTS_SEED), inline=True)
            e.add_field(
                name="Last roll",
                value=f"{last_roll} {'+'+str(last_win) if last_win else '–'}",
                inline=False
            )
            await panel.edit(embed=e, view=SlotsView(interaction.channel.id))
    except Exception as e:
        print("slots_reset panel refresh error:", e)

    # final ack
    await interaction.response.send_message("Slots pot reset to seed.", ephemeral=True)


# top winners (by total coins won) in this channel
@bot.tree.command(name="slots_top", description="Show top Slots winners (by total coins won) for this channel")
async def slots_top(interaction: discord.Interaction):
    with db() as conn:
        c = conn.cursor()
        c.execute("""SELECT discord_id, COALESCE(SUM(win),0) AS total
                     FROM slots_spins
                     WHERE channel_id=?
                     GROUP BY discord_id
                     HAVING total>0
                     ORDER BY total DESC
                     LIMIT 10""", (str(interaction.channel.id),))
        rows = c.fetchall()
    if not rows:
        return await interaction.response.send_message("No wins yet.", ephemeral=True)
    lines = []
    for i, (uid, total) in enumerate(rows, start=1):
        m = interaction.guild.get_member(int(uid))
        name = m.mention if m else f"<@{uid}>"
        lines.append(f"{i}. {name} — **{total}**")
    await interaction.response.send_message("**Slots Top Winners**\n" + "\n".join(lines), ephemeral=True)


# =========================
# 💸 Withdraw → WL Gifts (Modal + Ticket + Admin Approve)
# =========================

# --- Config (uses your existing env defaults) ---
# WL_COINS_PER_GIFT, MIN_WL_GIFTS, MAX_WL_GIFTS, SHOP_NAME, SHOP_YAELI_URL,
# TICKETS_STAFF_ROLE_ID must already exist in your file (they do).

# --- DB bootstrap ---
def _init_withdraw_tables():
    with db() as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS withdraw_requests(
            id INTEGER PRIMARY KEY,
            discord_id TEXT,
            coins INTEGER,
            gifts INTEGER,
            imvu_name TEXT,
            imvu_profile TEXT,  -- wishlist or profile URL
            note TEXT,
            status TEXT,        -- 'pending','approved','rejected'
            ticket_channel_id TEXT,
            message_id TEXT,    -- review message id inside ticket
            reviewer_id TEXT,   -- admin who approved/rejected
            review_note TEXT,
            created_ts TEXT,
            updated_ts TEXT
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_withdraw_user_status ON withdraw_requests(discord_id, status)")
_init_withdraw_tables()


# --- Slash: open withdraw modal ---
@bot.tree.command(name="eh_withdraw", description="Convert your coins to WL gifts (opens a ticket; admin approval)")
async def eh_withdraw(interaction: discord.Interaction):
    await interaction.response.send_modal(WithdrawWLModal())


# --- Modal: player fills details; creates ticket only if valid ---
class WithdrawWLModal(discord.ui.Modal, title="Withdraw → WL Gifts"):
    amount_coins = discord.ui.TextInput(
        label=f"Coins to convert (multiple of {WL_COINS_PER_GIFT})",
        placeholder=str(WL_COINS_PER_GIFT),
        required=True,
        max_length=12
    )
    imvu_handle_or_url = discord.ui.TextInput(
        label="IMVU Username or Profile URL",
        placeholder="e.g. YaEli   or   https://www.imvu.com/…",
        required=True,
        max_length=200
    )
    note = discord.ui.TextInput(
        label="Notes for staff (optional)",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=200
    )

    def _extract_username(self, text: str):
        raw = (text or "").strip()
        if not raw: return None, None, None
        if raw.startswith(("http://","https://")):
            import urllib.parse as _u
            try:
                p = _u.urlparse(raw)
                q = _u.parse_qs(p.query)
                if "av" in q and q["av"]:
                    uname = q["av"][0]
                else:
                    uname = p.path.strip("/").split("/")[-1] or None
            except Exception:
                uname = None
            profile_url = raw
        else:
            uname = raw
            profile_url = f"https://www.imvu.com/catalog/web_mypage.php?av={uname}"
        wishlist_url = f"https://www.imvu.com/catalog/web_wishlist.php?av={uname}" if uname else None
        return uname, profile_url, wishlist_url

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        ensure_user(uid)

        # Parse/validate amount
        try:
            coins = int(str(self.amount_coins).strip().replace("_",""))
        except Exception:
            return await interaction.response.send_message("Enter a valid number of coins.", ephemeral=True)
        if coins <= 0 or coins % WL_COINS_PER_GIFT != 0:
            return await interaction.response.send_message(
                f"Amount must be a positive multiple of **{WL_COINS_PER_GIFT}**.", ephemeral=True
            )
        gifts = coins // WL_COINS_PER_GIFT
        if gifts < MIN_WL_GIFTS or gifts > MAX_WL_GIFTS:
            return await interaction.response.send_message(
                f"Gift count must be between **{MIN_WL_GIFTS}** and **{MAX_WL_GIFTS}**.", ephemeral=True
            )

        # Balance check
        bal = get_balance(uid)
        if bal < coins:
            return await interaction.response.send_message(
                f"Insufficient coins. Need **{coins}**, you have **{bal}**.", ephemeral=True
            )

        # Block duplicate pending
        with db() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM withdraw_requests WHERE discord_id=? AND status='pending'", (uid,))
            if c.fetchone()[0] > 0:
                return await interaction.response.send_message(
                    "You already have a pending withdrawal ticket. Please wait for staff to process it.",
                    ephemeral=True
                )

        # Parse IMVU handle/link
        uname, profile_url, wishlist_url = self._extract_username(str(self.imvu_handle_or_url))
        if not uname:
            return await interaction.response.send_message("Please enter a valid IMVU username or profile link.", ephemeral=True)

        # Create DB request
        with db() as conn:
            c = conn.cursor()
            c.execute("""INSERT INTO withdraw_requests(discord_id,coins,gifts,imvu_name,imvu_profile,note,status,created_ts,updated_ts)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
                      (uid, coins, gifts, uname, wishlist_url or profile_url or "", str(self.note or ""),
                       "pending", iso(now_local()), iso(now_local())))
            req_id = c.lastrowid

        # Create private ticket (user + staff)
        cat = await _get_or_create_tickets_category(interaction.guild)
        if not cat:
            return await interaction.response.send_message("Could not create a ticket channel. Please ping an admin.", ephemeral=True)

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
        }
        # add staff role if you have one...
        if TICKETS_STAFF_ROLE_ID:
            role = interaction.guild.get_role(TICKETS_STAFF_ROLE_ID)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
        
        # ✅ add this line:
        overwrites[interaction.guild.me] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, embed_links=True, attach_files=True, read_message_history=True, use_application_commands=True
        )

        if TICKETS_STAFF_ROLE_ID:
            role = interaction.guild.get_role(TICKETS_STAFF_ROLE_ID)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

        ticket = await interaction.guild.create_text_channel(
            f"wl-withdraw-{interaction.user.name[:16].lower()}-{req_id}",
            category=cat, overwrites=overwrites, reason="WL withdraw request"
        )

        # Post admin review panel
        embed = discord.Embed(
            title=f"WL Withdraw Request #{req_id}",
            description=(f"User: <@{uid}>\n"
                         f"Coins → WL: **{coins} → {gifts}** (rate {WL_COINS_PER_GIFT}/WL)\n"
                         f"IMVU: **{uname}**\n"
                         f"[Profile/Wishlist]({wishlist_url or profile_url})"),
            color=discord.Color.gold(),
            timestamp=now_local()
        )
        if self.note:
            embed.add_field(name="User note", value=str(self.note)[:200], inline=False)
        embed.set_footer(text="Staff: review and approve or reject below.")

        view = AdminWithdrawReviewView(req_id)
        msg = await ticket.send(
            content=(f"<@&{TICKETS_STAFF_ROLE_ID}>" if TICKETS_STAFF_ROLE_ID else "@here"),
            embed=embed,
            view=view
        )

        # Save ticket/message refs
        with db() as conn:
            c = conn.cursor()
            c.execute("UPDATE withdraw_requests SET ticket_channel_id=?, message_id=?, updated_ts=? WHERE id=?",
                      (str(ticket.id), str(msg.id), iso(now_local()), req_id))

        # Final ack to user
        await interaction.response.send_message(
            f"✅ Request submitted. A private ticket was opened: {ticket.mention}",
            ephemeral=True
        )


# --- Admin buttons inside the ticket ---
class AdminWithdrawReviewView(discord.ui.View):
    def __init__(self, request_id: int):
        super().__init__(timeout=None)
        self.request_id = request_id

    @discord.ui.button(label="Approve & Deduct", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_admin_member(interaction.guild, interaction.user):
            return await interaction.response.send_message("You don’t have permission.", ephemeral=True)
        await interaction.response.send_modal(AdminApproveWithdrawModal(self.request_id))

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, emoji="🛑")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_admin_member(interaction.guild, interaction.user):
            return await interaction.response.send_message("You don’t have permission.", ephemeral=True)
        await interaction.response.send_modal(AdminRejectWithdrawModal(self.request_id))


class DisabledReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for label, style in [("Approved", discord.ButtonStyle.success),
                             ("Rejected", discord.ButtonStyle.danger)]:
            self.add_item(discord.ui.Button(label=label, style=style, disabled=True))


# --- Admin approve modal: deducts coins, creates prize & queue, marks approved ---
class AdminApproveWithdrawModal(discord.ui.Modal, title="Approve WL Withdraw"):
    coins = discord.ui.TextInput(
        label="Confirm coins to deduct",
        placeholder="e.g. 20000",
        required=True,
        max_length=12
    )
    note = discord.ui.TextInput(
        label="Internal note (optional)",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=200
    )

    def __init__(self, request_id: int):
        super().__init__(timeout=180)
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction):
        if not _is_admin_member(interaction.guild, interaction.user):
            return await interaction.response.send_message("You don’t have permission to approve.", ephemeral=True)

        # Load request
        with db() as conn:
            c = conn.cursor()
            c.execute("""SELECT discord_id, coins, gifts, status, ticket_channel_id, message_id, imvu_name, imvu_profile
                         FROM withdraw_requests WHERE id=?""", (self.request_id,))
            row = c.fetchone()
        if not row:
            return await interaction.response.send_message("Request not found.", ephemeral=True)

        uid, coins_req, gifts_req, status, tchid, mid, uname, prof = row
        if status != "pending":
            return await interaction.response.send_message(f"Request is already **{status}**.", ephemeral=True)

        # Parse confirmed coins
        try:
            coins_final = int(str(self.coins).strip().replace("_",""))
        except Exception:
            return await interaction.response.send_message("Enter a valid coin amount.", ephemeral=True)
        if coins_final <= 0 or coins_final % WL_COINS_PER_GIFT != 0:
            return await interaction.response.send_message(
                f"Amount must be a positive multiple of **{WL_COINS_PER_GIFT}**.", ephemeral=True
            )
        gifts_final = coins_final // WL_COINS_PER_GIFT
        if gifts_final < MIN_WL_GIFTS or gifts_final > MAX_WL_GIFTS:
            return await interaction.response.send_message(
                f"Gift count must be between **{MIN_WL_GIFTS}** and **{MAX_WL_GIFTS}**.", ephemeral=True
            )

        # Balance check at approval time
        bal = get_balance(uid)
        if bal < coins_final:
            return await interaction.response.send_message(
                f"User balance changed. Needs **{coins_final}**, has **{bal}**. Adjust and try again.", ephemeral=True
            )

        # Deduct & create prize + queue; mark approved
        with db() as conn:
            c = conn.cursor()
            # deduct
            c.execute("UPDATE users SET balance=balance-? WHERE discord_id=?", (coins_final, uid))
            c.execute("INSERT INTO tx(discord_id,kind,amount,meta,ts) VALUES(?,?,?,?,?)",
                      (uid, "adjust", -coins_final, f"withdraw_to_wl:{gifts_final} gifts", iso(now_local())))
            # prize + queue
            c.execute("""INSERT INTO prizes(winner_id,kind,amount,meta,status,created_ts,updated_ts)
                         VALUES(?,?,?,?,?,?,?)""",
                      (uid, "wl", gifts_final, json.dumps({"shop": SHOP_NAME, "source": "user_withdraw"}), "pending",
                       iso(now_local()), iso(now_local())))
            prize_id = c.lastrowid
            c.execute("""INSERT INTO prize_queue(prize_id,winner_id,imvu_name,imvu_profile,note,status,created_ts,updated_ts)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (prize_id, uid, uname, prof or "", str(self.note or ""), "ready", iso(now_local()), iso(now_local())))
            # mark request
            c.execute("""UPDATE withdraw_requests SET status='approved', reviewer_id=?, review_note=?, coins=?, gifts=?, updated_ts=?
                         WHERE id=?""",
                      (str(interaction.user.id), str(self.note or ""), coins_final, gifts_final, iso(now_local()), self.request_id))

        # Update ticket panel & freeze buttons
        try:
            channel = interaction.guild.get_channel(int(tchid)) if tchid else None
            if channel and mid:
                msg = await channel.fetch_message(int(mid))
                e = msg.embeds[0] if msg.embeds else discord.Embed(color=discord.Color.gold())
                e.add_field(name="Status", value=f"✅ **Approved** by {interaction.user.mention}\nCoins: {coins_final} → WL: {gifts_final}", inline=False)
                await msg.edit(embed=e, view=DisabledReviewView())
        except Exception:
            pass

        await interaction.response.send_message("Approved and deducted. Prize queued for fulfilment. ✅", ephemeral=True)

# --- Admin reject modal: marks rejected (no balance change) ---
class AdminRejectWithdrawModal(discord.ui.Modal, title="Reject WL Withdraw"):
    reason = discord.ui.TextInput(label="Reason (shown to user)", required=True, max_length=200)

    def __init__(self, request_id: int):
        super().__init__(timeout=180)
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction):
        if not _is_admin_member(interaction.guild, interaction.user):
            return await interaction.response.send_message("You don’t have permission to reject.", ephemeral=True)

        with db() as conn:
            c = conn.cursor()
            c.execute("""SELECT ticket_channel_id, message_id, status FROM withdraw_requests WHERE id=?""",
                      (self.request_id,))
            row = c.fetchone()
        if not row:
            return await interaction.response.send_message("Request not found.", ephemeral=True)

        tchid, mid, status = row
        if status != "pending":
            return await interaction.response.send_message(f"Request is already **{status}**.", ephemeral=True)

        with db() as conn:
            c = conn.cursor()
            c.execute("""UPDATE withdraw_requests SET status='rejected', reviewer_id=?, review_note=?, updated_ts=?
                         WHERE id=?""",
                      (str(interaction.user.id), str(self.reason), iso(now_local()), self.request_id))

        try:
            channel = interaction.guild.get_channel(int(tchid)) if tchid else None
            if channel and mid:
                msg = await channel.fetch_message(int(mid))
                e = msg.embeds[0] if msg.embeds else discord.Embed(color=discord.Color.gold())
                e.add_field(name="Status", value=f"❌ **Rejected** by {interaction.user.mention}\nReason: {str(self.reason)}", inline=False)
                await msg.edit(embed=e, view=DisabledReviewView())
        except Exception:
            pass

        await interaction.response.send_message("Rejected and left balance unchanged. ❌", ephemeral=True)
@bot.event
async def setup_hook():
    # Register handlers for the custom_id buttons so old messages keep working after restarts
    bot.add_view(RouletteBetView(0))   # rid here is a dummy; you’ll attach a fresh view to live rounds

from discord import app_commands

@bot.tree.command(name="eh_deposit", description="(owner-only) Deposit coins to a user")
@app_commands.describe(
    user="Member to receive coins",
    amount="How many coins to deposit (positive integer)",
    reason="Note (optional)"
)
async def eh_deposit(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int,
    reason: str | None = None
):
    # --- PERMISSION: strictly you only ---
    if interaction.user.id != DEPOSITOR_ID:
        return await interaction.response.send_message("You can’t use this command.", ephemeral=True)

    if amount <= 0:
        return await interaction.response.send_message("Amount must be a positive number.", ephemeral=True)

    await interaction.response.defer(ephemeral=True, thinking=True)

    # ensure recipient exists
    uid = str(user.id)
    ensure_user(uid)

    try:
        with db() as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET balance = balance + ? WHERE discord_id = ?", (amount, uid))
            meta = f"deposit|by:{interaction.user.id}" + (f"|reason:{reason}" if reason else "")
            c.execute(
                "INSERT INTO tx(discord_id,kind,amount,meta,ts) VALUES(?,?,?,?,?)",
                (uid, "deposit", amount, meta, iso(now_local()))
            )
        new_bal = get_balance(uid)
    except Exception as e:
        import traceback; print("DEPOSIT ERROR:\n", traceback.format_exc())
        return await interaction.followup.send(f"❌ Deposit failed: {e}", ephemeral=True)

    # confirm to you (ephemeral)
    await interaction.followup.send(
        f"✅ Deposited **{amount}** coins to {user.mention}. New balance: **{new_bal}**.",
        ephemeral=True
    )

    # (optional) public log — delete if you want it silent
    try:
        await interaction.channel.send(
            f"💰 Admin deposit: {user.mention} received **{amount}** coins."
            + (f" _({reason})_" if reason else "")
        )
    except Exception:
        pass

bot.run(TOKEN)
