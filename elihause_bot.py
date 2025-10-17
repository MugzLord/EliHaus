# elihause_bot.py — EliHaus (coins + admin roulette + weekly lotto + prize queue)
# Requires: pip install -U discord.py
import os, sqlite3, random, math, json
from datetime import datetime, timedelta, timezone
import asyncio
import discord
from discord.ext import commands
import json, os
from datetime import datetime
from zoneinfo import ZoneInfo  # proper DST for London


# ---- Config ----
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN")

TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/London")
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
    TZ = timezone.utc

PREFIX = "!"
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
bot = commands.Bot(command_prefix=PREFIX, intents=INTENTS)

# Economy knobs (Option B)
DAILY_AMOUNT = 1_800
WEEKLY_AMOUNT = 6_000
STARTER_AMOUNT = 5_000

# Lotto knobs
TICKET_COST = 10_000            # coins per ticket
LOTTO_WINNERS = 1               # fixed: 1 grand winner
LOTTO_WL_COUNT = 10             # 10 wishlist gifts to the winner
SHOP_NAME = "Shop YaEli"
SHOP_YAELI_URL = os.getenv("https://www.imvu.com/shop/web_search.php?manufacturers_id=360644281")  # put your real shop link


# Roulette knobs (admin-led, manual resolve)
ROUND_SECONDS_DEFAULT = 120
PAYOUT_RED_BLACK = 2.0
PAYOUT_GREEN = 14.0             # enable green flavour
MAX_STAKE = 50_000
ONE_BET_PER_ROUND = True

# --- Ticket system config ---
# Option A: set a fixed Category ID via env (recommended)
TICKETS_CATEGORY_ID = int(os.getenv("TICKETS_CATEGORY_ID", "0"))  # e.g. 123456789012345678
# Option B: if no ID, bot will look for (or create) a category with this name:
TICKETS_CATEGORY_NAME = os.getenv("TICKETS_CATEGORY_NAME", "🎟️ wl-claims")
# Optional staff role who can see all tickets (ID). If 0, only admins + winner can see.
TICKETS_STAFF_ROLE_ID = int(os.getenv("TICKETS_STAFF_ROLE_ID", "0"))

# --- Shop link used in the policy note (make this your real IMVU shop URL) ---
SHOP_YAELI_URL = os.getenv("https://www.imvu.com/shop/web_search.php?manufacturers_id=360644281")  # <- put your real shop link here


# ---- DB ----
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
            kind TEXT,        -- 'claim','bet','payout','adjust','redeem','lotto','starter'
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
            week_id TEXT,        -- 'YYYY-WW'
            discord_id TEXT,
            ts TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS lotto_draws(
            id INTEGER PRIMARY KEY,
            week_id TEXT,
            run_at TEXT,
            winner_id TEXT,
            seed TEXT,
            status TEXT          -- PENDING|DONE
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS prizes(
            id INTEGER PRIMARY KEY,
            winner_id TEXT,
            kind TEXT,           -- 'wl'
            amount INTEGER,      -- number of WL gifts
            meta TEXT,           -- JSON: {"shop":"Shop X"}
            status TEXT,         -- 'pending','claimed','ready','fulfilled','failed'
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
init_db()


# ---- Helpers ----
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

LONDON_TZ = ZoneInfo("Europe/London")  # uses DST automatically

def now_london() -> datetime:
    return datetime.now(LONDON_TZ)

def next_draw_dt(ref: datetime | None = None) -> datetime:
    """
    Next Saturday at 20:00 London time.
    """
    ref = ref or now_london()
    target_wd = 5  # Monday=0 ... Saturday=5, Sunday=6
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

# ---- State keys for prize announce message & ticket channel ----
def _prize_msg_key(prize_id: int) -> str:
    return f"prize_msg:{prize_id}"

def _prize_ticket_key(prize_id: int) -> str:
    return f"prize_ticket:{prize_id}"

async def _get_or_create_tickets_category(guild: discord.Guild) -> discord.CategoryChannel | None:
    if TICKETS_CATEGORY_ID:
        cat = guild.get_channel(TICKETS_CATEGORY_ID)
        if isinstance(cat, discord.CategoryChannel):
            return cat
    # fallback: by name
    for ch in guild.categories:
        if ch.name == TICKETS_CATEGORY_NAME:
            return ch
    try:
        return await guild.create_category(TICKETS_CATEGORY_NAME, reason="EliHaus WL claims")
    except Exception:
        return None

def now_local():
    return datetime.now(TZ)

def iso(dt: datetime) -> str:
    return dt.astimezone(TZ).isoformat()

def week_id(dt: datetime | None = None) -> str:
    dt = dt or now_local()
    y, w, _ = dt.isocalendar()
    return f"{y}-{w:02d}"

def ensure_user(uid: str):
    with db() as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users(discord_id,balance,last_daily,last_weekly,joined_at) VALUES(?,?,?,?,?)",
                  (uid, 0, None, None, iso(now_local())))

def get_balance(uid: str) -> int:
    ensure_user(uid)
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT balance FROM users WHERE discord_id=?", (uid,))
        row = c.fetchone()
        return row[0] if row else 0

def change_balance(uid: str, delta: int, kind: str, meta: str = "") -> int:
    ensure_user(uid)
    with db() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET balance = balance + ? WHERE discord_id=?", (delta, uid))
        c.execute("INSERT INTO tx(discord_id,kind,amount,meta,ts) VALUES(?,?,?,?,?)",
                  (uid, kind, delta, meta, iso(now_local())))
        c.execute("SELECT balance FROM users WHERE discord_id=?", (uid,))
        return c.fetchone()[0]

def set_state(key: str, val: str | None):
    with db() as conn:
        c = conn.cursor()
        if val is None:
            c.execute("DELETE FROM state WHERE key=?", (key,))
        else:
            c.execute("INSERT INTO state(key,val) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET val=excluded.val", (key, val))

def get_state(key: str) -> str | None:
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT val FROM state WHERE key=?", (key,))
        r = c.fetchone()
        return r[0] if r else None

def round_key(channel_id: int) -> str:
    return f"round:{channel_id}"

# ---- Views/Modals for prize claim ----
class DisabledClaimView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        btn = discord.ui.Button(label="Claim WL Gifts", style=discord.ButtonStyle.secondary, disabled=True)
        self.add_item(btn)

class ClaimView(discord.ui.View):
    def __init__(self, prize_id: int, timeout: int = 600):
        super().__init__(timeout=timeout)
        self.prize_id = prize_id

    @discord.ui.button(label="Claim WL Gifts", style=discord.ButtonStyle.primary)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        # remember the announce message ID so we can grey the button later
        set_state(_prize_msg_key(self.prize_id), str(interaction.message.id))

        if str(interaction.user.id) != self._winner_id_from_prize(self.prize_id):
            return await interaction.response.send_message("Only the winner can claim this prize.", ephemeral=True)

        # disable immediately to prevent double-click spam
        try:
            await interaction.message.edit(view=DisabledClaimView())
        except Exception:
            pass
        # then open the modal:
        await interaction.response.send_modal(ClaimModal(self.prize_id))

   

    def _winner_id_from_prize(self, pid: int) -> str:
        with db() as conn:
            c = conn.cursor()
            c.execute("SELECT winner_id FROM prizes WHERE id=?", (pid,))
            row = c.fetchone()
            return row[0] if row else ""


    # ---- Pretty round labels (per channel) ----
    def _round_counter_key(channel_id: int) -> str:
        return f"rcount:{channel_id}"
    
    def _round_label_key(rid: str) -> str:
        return f"rlabel:{rid}"
    
    def next_round_number(channel_id: int) -> int:
        cur = int(get_state(_round_counter_key(channel_id)) or 0)
        cur += 1
        set_state(_round_counter_key(channel_id), str(cur))
        return cur
    
    def set_round_label(rid: str, label: str):
        set_state(_round_label_key(rid), label)
    
    def get_round_label(rid: str) -> str:
        return get_state(_round_label_key(rid)) or rid  # fallback to full rid if missing

    # Shorten long strings like seeds
    def short_seed(s: str, n: int = 6) -> str:
        return f"{s[:n]}…{s[-n:]}" if s and len(s) > 2 * n else s

    ROUND_STATE_FILE = "roulette_rounds.json"

    def _load_round_state():
        try:
            with open(ROUND_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception:
            return {}
    
    def _save_round_state(state: dict):
        tmp = ROUND_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, ROUND_STATE_FILE)
    
    def next_round_label(guild_id: int):
        """Returns ('YYYY-MM-DD', round_number_int) and persists it per-guild/per-day."""
        state = _load_round_state()
        g = str(guild_id)
        today = datetime.utcnow().strftime("%Y-%m-%d")  # use UTC; swap to local if you prefer
        gstate = state.setdefault(g, {})
        n = int(gstate.get(today, 0)) + 1
        gstate[today] = n
        _save_round_state(state)
        return today, n

# ==== Role-aware Help (single embed, filtered) ====
# If you already have ADMIN_ROLE_ID in env/config, keep it. Otherwise set to 0.
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))  # optional: set to your @Admin role ID

def _user_is_admin(ctx: commands.Context) -> bool:
    if ctx.author.guild_permissions.manage_guild or ctx.guild.owner_id == ctx.author.id:
        return True
    if ADMIN_ROLE_ID:
        role = ctx.guild.get_role(ADMIN_ROLE_ID)
        if role and role in ctx.author.roles:
            return True
    return False

class EliHausHelp(commands.MinimalHelpCommand):
    def get_command_signature(self, command):
        return f"!{command.qualified_name} {command.signature}".strip()

    async def send_bot_help(self, mapping):
        ctx = self.context
        is_admin = _user_is_admin(ctx)

        # Define buckets
        player_names = [
            "joinhaus","daily","weekly","balance","bet","round","buyticket","lotto"
        ]
        admin_names = [
            "openround","resolve","cancelround","deposit","withdraw",
            "drawlotto","fulfil_next","fulfil_done",
            "lottoboard","lottoexport","roundreset","settickets","diag"
        ]

        # Collect commands that actually exist (in case some were not added)
        all_cmds = {c.qualified_name: c for c in ctx.bot.commands}

        def fmt(names):
            lines = []
            for n in names:
                cmd = all_cmds.get(n)
                if not cmd:
                    continue
                sig = self.get_command_signature(cmd)
                brief = f" — {cmd.brief}" if getattr(cmd, "brief", None) else ""
                lines.append(f"• `{sig}`{brief}")
            return "\n".join(lines) if lines else "_None_"

        e = discord.Embed(
            title="📖 EliHaus Commands",
            description="**Tip:** Use `!help <command>` for details.",
            color=discord.Color.gold()
        )
        e.add_field(name="🎮 Player Commands", value=fmt(player_names), inline=False)

        if is_admin:
            e.add_field(name="🛠️ Admin Commands", value=fmt(admin_names), inline=False)
            e.set_footer(text="You are an admin: admin commands shown.")
        else:
            e.set_footer(text="Admin commands are hidden. Ask a mod if you need help.")

        await self.get_destination().send(embed=e)

    async def send_command_help(self, command):
        e = discord.Embed(
            title=f"❓ Help: !{command.qualified_name}",
            color=discord.Color.gold()
        )
        e.add_field(name="Usage", value=f"`{self.get_command_signature(command) or '!'+command.qualified_name}`", inline=False)
        if command.help:
            e.add_field(name="Description", value=command.help, inline=False)
        await self.get_destination().send(embed=e)

# Activate it (once, after you create the bot)
bot.help_command = EliHausHelp()

    

class ClaimModal(discord.ui.Modal, title="Claim WL Gifts"):
    # SINGLE field that accepts either a username or a full IMVU profile link
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
        placeholder="Anything staff should know (order, size, color, etc.)"
    )

    def __init__(self, prize_id: int):
        super().__init__()
        self.prize_id = prize_id

    # --- helpers to normalize what the user entered ---
    def _extract_username(self, text: str) -> tuple[str|None, str|None, str|None]:
        """
        Returns (username, profile_url, wishlist_url)
        Accepts either a bare username or a profile URL.
        """
        raw = (text or "").strip()
        if not raw:
            return None, None, None

        # If it's a URL, try to extract the username from common IMVU patterns
        if raw.startswith("http://") or raw.startswith("https://"):
            url = raw
            # Try to parse `av=<username>` query param first
            import urllib.parse as _u
            try:
                p = _u.urlparse(url)
                q = _u.parse_qs(p.query)
                if "av" in q and q["av"]:
                    uname = q["av"][0]
                else:
                    # fallback: sometimes profile URLs end with the name (best effort)
                    uname = p.path.strip("/").split("/")[-1] or None
            except Exception:
                uname = None
            profile_url = url
        else:
            # Treat as plain username
            uname = raw
            # Old profile style works reliably with ?av=
            profile_url = f"https://www.imvu.com/catalog/web_mypage.php?av={uname}"

        # Build a wishlist URL (two common patterns; choose the first)
        wishlist_url = f"https://www.imvu.com/catalog/web_wishlist.php?av={uname}" if uname else None
        return uname, profile_url, wishlist_url

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)

        # idempotence: if a ticket already exists for this prize, just send link
        existing_ticket_id = get_state(_prize_ticket_key(self.prize_id))
        if existing_ticket_id:
            ch = interaction.guild.get_channel(int(existing_ticket_id))
            if ch:
                return await interaction.response.send_message(f"You already opened a ticket: {ch.mention}", ephemeral=True)

        # Parse username / URLs
        uname, profile_url, wishlist_url = self._extract_username(str(self.handle_or_url))
        if not uname:
            return await interaction.response.send_message("Please enter a valid IMVU username or profile link.", ephemeral=True)

        # queue entry + mark prize claimed (store wishlist/profile in imvu_profile field)
        with db() as conn:
            c = conn.cursor()
            c.execute("""INSERT INTO prize_queue(prize_id,winner_id,imvu_name,imvu_profile,note,status,created_ts,updated_ts)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (self.prize_id, uid, uname, wishlist_url or profile_url or "", str(self.note or ""),
                       "ready", iso(now_local()), iso(now_local())))
            c.execute("UPDATE prizes SET status='claimed', updated_ts=? WHERE id=?", (iso(now_local()), self.prize_id))

        # create a private ticket channel
        cat = await _get_or_create_tickets_category(interaction.guild)
        if not cat:
            return await interaction.response.send_message("Could not create a ticket channel (no category). Please ping an admin.", ephemeral=True)

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

        # remember ticket channel
        set_state(_prize_ticket_key(self.prize_id), str(ticket.id))

        # Post ticket intro with clickable links + policy note (with clickable Shop link)
        staff_tag = f"<@&{TICKETS_STAFF_ROLE_ID}>" if TICKETS_STAFF_ROLE_ID else "@here"
        profile_line = f"[{uname}]({profile_url})" if profile_url else uname
        wishlist_line = f"[Open Wishlist]({wishlist_url})" if wishlist_url else "—"

        policy = (
            f"**Policy:** To claim your winnings, you must have **10 items** added from **[Shop YaEli]({SHOP_YAELI_URL})**. "
            f"Failure to comply is subject to **disqualification**."
        )

        await ticket.send(
            f"{staff_tag} New WL claim for {interaction.user.mention}\n"
            f"IMVU: {profile_line}\n"
            f"Wishlist: {wishlist_line}\n"
            f"Notes: {str(self.note or '—')}\n\n"
            f"{policy}"
        )

        # Grey-out / disable the original button on the announce message
        try:
            msg_id = get_state(_prize_msg_key(self.prize_id))
            if msg_id:
                msg = await interaction.channel.fetch_message(int(msg_id))
                await msg.edit(view=DisabledClaimView())
        except Exception:
            pass

        await interaction.response.send_message(f"✅ Ticket created: {ticket.mention}", ephemeral=True)



        # optional: refresh the round embed stats
        try:
            with db() as conn:
                c = conn.cursor()
                c.execute("SELECT message_id FROM rounds WHERE rid=?", (self.rid,))
                r = c.fetchone()
            if r and r[0]:
                msg = await interaction.channel.fetch_message(int(r[0]))
                with db() as conn:
                    c = conn.cursor()
                    c.execute("SELECT COUNT(*), COALESCE(SUM(stake),0) FROM bets WHERE rid=?", (self.rid,))
                    cnt, pool = c.fetchone()
                if msg.embeds:
                    e = msg.embeds[0]
                    e.clear_fields()
                    _, exp2 = get_open_round(interaction.channel.id)
                    left = max(0, int((exp2 - now_local()).total_seconds()))
                    e.add_field(name="Pool", value=str(pool), inline=True)
                    e.add_field(name="Time", value=f"{left}s left", inline=True)
                    e.add_field(name="Bets", value=str(cnt), inline=True)
                    await msg.edit(embed=e)
        except Exception:
            pass

    from discord.ext import commands
    import traceback
    
    @bot.event
    async def on_command_error(ctx, error):
        # unwrap the underlying exception so we see the real reason
        if isinstance(error, commands.CommandInvokeError):
            orig = error.original
            tb = "".join(traceback.format_exception(type(orig), orig, orig.__traceback__))[:1800]
            return await ctx.reply(f"Crash: **{type(orig).__name__}** — {orig}\n```py\n{tb}\n```")
        elif isinstance(error, (commands.MissingPermissions, commands.CheckFailure)):
            return await ctx.reply("You don’t have permission to use that command.")
        elif isinstance(error, commands.CommandNotFound):
            return  # ignore typos quietly
        else:
            return await ctx.reply(f"Error: **{type(error).__name__}** — {error}")


class BetView(discord.ui.View):
    def __init__(self, rid: str, timeout: int | None = None):
        super().__init__(timeout=timeout or 120)
        self.rid = rid

    @discord.ui.button(label="Bet RED", style=discord.ButtonStyle.danger, emoji="🟥")
    async def bet_red(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BetModal(self.rid, color="red"))

    @discord.ui.button(label="Bet BLACK", style=discord.ButtonStyle.primary, emoji="⬛")
    async def bet_black(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BetModal(self.rid, color="black"))

    @discord.ui.button(label="Bet GREEN", style=discord.ButtonStyle.success, emoji="🟩")
    async def bet_green(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BetModal(self.rid, color="green"))

# ---- Commands: Coins ----
@bot.command(name="joinhaus")
async def joinhaus(ctx: commands.Context):
    uid = str(ctx.author.id)
    ensure_user(uid)
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT joined_at FROM users WHERE discord_id=?", (uid,))
        row = c.fetchone()
        # Starter only if balance is zero and no prior tx of kind 'starter'
        c.execute("SELECT 1 FROM tx WHERE discord_id=? AND kind='starter' LIMIT 1", (uid,))
        has_starter = c.fetchone() is not None
    if has_starter:
        return await ctx.reply("You’ve already joined EliHaus. Use `!daily` and `!weekly` to build coins.")
    new_bal = change_balance(uid, STARTER_AMOUNT, "starter", "joinhaus starter")
    await ctx.reply(f"Welcome to **EliHaus**. Starter pack: **{STARTER_AMOUNT}** coins. Balance: **{new_bal}**")

@bot.command(name="daily")
async def daily(ctx: commands.Context):
    uid = str(ctx.author.id)
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
            return await ctx.reply(f"You’ve already claimed. Try again in **{hrs}h {mins}m**.")
        new_bal = change_balance(uid, DAILY_AMOUNT, "claim", "daily")
        c.execute("UPDATE users SET last_daily=? WHERE discord_id=?", (iso(now), uid))
    await ctx.reply(f"Daily claimed: **{DAILY_AMOUNT}** coins. New balance: **{new_bal}**")

@bot.command(name="weekly")
async def weekly(ctx: commands.Context):
    uid = str(ctx.author.id)
    ensure_user(uid)
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT last_weekly FROM users WHERE discord_id=?", (uid,))
        row = c.fetchone()
        now = now_local()
        last = datetime.fromisoformat(row[0]).astimezone(TZ) if row and row[0] else None
        if last and (last.isocalendar()[:2] == now.isocalendar()[:2]):
            return await ctx.reply("You’ve already claimed your weekly this week.")
        new_bal = change_balance(uid, WEEKLY_AMOUNT, "claim", "weekly")
        c.execute("UPDATE users SET last_weekly=? WHERE discord_id=?", (iso(now), uid))
    await ctx.reply(f"Weekly claimed: **{WEEKLY_AMOUNT}** coins. New balance: **{new_bal}**")

@bot.command(name="balance")
async def balance(ctx: commands.Context, member: discord.Member | None = None):
    m = member or ctx.author
    bal = get_balance(str(m.id))
    await ctx.reply(f"{m.mention} has **{bal}** coins.")

# Admin coin adjust (optional)
@commands.has_permissions(manage_guild=True)
@bot.command(name="deposit")
async def deposit(ctx: commands.Context, member: discord.Member, amount: int):
    if amount <= 0:
        return await ctx.reply("Amount must be positive.")
    new_bal = change_balance(str(member.id), amount, "adjust", f"deposit by {ctx.author.id}")
    await ctx.reply(f"Deposited **{amount}** to {member.mention}. Balance: **{new_bal}**")

@commands.has_permissions(manage_guild=True)
@bot.command(name="withdraw")
async def withdraw(ctx: commands.Context, member: discord.Member, amount: int):
    if amount <= 0:
        return await ctx.reply("Amount must be positive.")
    uid = str(member.id)
    bal = get_balance(uid)
    if bal < amount:
        return await ctx.reply("Insufficient user balance.")
    new_bal = change_balance(uid, -amount, "adjust", f"withdraw by {ctx.author.id}")
    await ctx.reply(f"Withdrew **{amount}** from {member.mention}. Balance: **{new_bal}**")

# ---- Roulette (Admin open + Admin resolve) ----
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
    """Return (rid, expires_at_dt) if truly open; otherwise auto-clear stale state and return None."""
    rk = round_key(channel_id)
    rid = get_state(rk)
    if not rid:
        return None
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT status, expires_at FROM rounds WHERE rid=? LIMIT 1", (rid,))
        row = c.fetchone()
    if not row:
        # state points to a non-existent round
        set_state(rk, None)
        return None
    status, exp = row
    try:
        exp_dt = datetime.fromisoformat(exp)
    except Exception:
        exp_dt = now_local()  # treat bad value as expired

    # if not OPEN or expired, clear lock and report none
    if status != "OPEN" or now_local() > exp_dt:
        set_state(rk, None)
        return None

    return rid, exp_dt



# --- Roulette round id (date + per-day counter, per guild) ---
ROUND_STATE_FILE = "roulette_rounds.json"
LONDON_TZ = ZoneInfo("Europe/London")  # respects BST/GMT

def _load_round_state():
    try:
        with open(ROUND_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}

def _save_round_state(state: dict):
    tmp = ROUND_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, ROUND_STATE_FILE)

def next_round_label(guild_id: int):
    """
    Returns ('YYYY-MM-DD', round_number_int) and persists it per-guild/per-day
    using London local date.
    """
    state = _load_round_state()
    g = str(guild_id)
    today = datetime.now(LONDON_TZ).strftime("%Y-%m-%d")
    gstate = state.setdefault(g, {})
    n = int(gstate.get(today, 0)) + 1
    gstate[today] = n
    _save_round_state(state)
    return today, n


@is_admin()
@bot.command(name="openround")
async def openround(ctx, seconds: int = ROUND_SECONDS_DEFAULT):
    seconds = max(10, min(seconds, 600))
    # self-healing get_open_round already clears stale; just check
    if get_open_round(ctx.channel.id):
        return await ctx.reply("There’s already an open round in this channel.")

    # safety clamp (avoid silly values)
    seconds = max(10, min(seconds, 600))

    rid, exp = open_round(ctx.channel.id, seconds, str(ctx.author.id))

    rnum = next_round_number(ctx.channel.id)
    rlabel = f"#{rnum}"
    set_round_label(rid, rlabel)

    embed = discord.Embed(
        title=f"🎯 Roulette — Round {rlabel}",
        description="Click a button to bet. A modal will ask your amount.",
        color=discord.Color.gold()
    )
    embed.add_field(name="Pool", value="0", inline=True)
    embed.add_field(name="Time", value=f"{seconds}s left", inline=True)
    embed.add_field(name="Bets", value="0", inline=True)

    # keep buttons alive slightly longer than the window
    view = BetView(rid, timeout=seconds + 30)
    msg = await ctx.reply(embed=embed, view=view)

    with db() as conn:
        conn.execute("UPDATE rounds SET message_id=? WHERE rid=?", (str(msg.id), rid))

    stale = get_open_round(ctx.channel.id)
    if stale:
        rid_stale, exp = stale
        if now_local() > exp:
            with db() as conn:
                conn.execute("UPDATE rounds SET status='CANCELLED', resolved_at=? WHERE rid=?",
                             (iso(now_local()), rid_stale))
            set_state(round_key(ctx.channel.id), None)
        else:
            return await ctx.reply("There’s already an open round in this channel.")


@bot.command(name="round")
async def round_status(ctx: commands.Context):
    o = get_open_round(ctx.channel.id)
    if not o:
        return await ctx.reply("No open round in this channel.")
    rid, exp = o
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*), COALESCE(SUM(stake),0) FROM bets WHERE rid=?", (rid,))
        cnt, pool = c.fetchone()
    remain = max(0, int((exp - now_local()).total_seconds()))
    await ctx.reply(f"Round `{rid}` — Bets: **{cnt}** | Pool: **{pool}** | Time left: **{remain}s**")

@bot.command(name="bet")
async def bet(ctx: commands.Context, amount: int, choice: str):
    choice = choice.lower().strip()
    if choice not in ("red","black","green"):
        return await ctx.reply("Pick `red`, `black`, or `green`.")
    o = get_open_round(ctx.channel.id)
    if not o:
        return await ctx.reply("No open round. Ask an admin to `!openround`.")
    rid, exp = o
    if now_local() > exp:
        return await ctx.reply("Betting window is closed for this round.")
    if amount <= 0 or amount > MAX_STAKE:
        return await ctx.reply(f"Stake must be between 1 and {MAX_STAKE}.")
    uid = str(ctx.author.id)
    ensure_user(uid)
    with db() as conn:
        c = conn.cursor()
        if ONE_BET_PER_ROUND:
            c.execute("SELECT 1 FROM bets WHERE rid=? AND discord_id=? LIMIT 1", (rid, uid))
            if c.fetchone():
                return await ctx.reply("You’ve already placed a bet this round.")
        bal = get_balance(uid)
        if bal < amount:
            return await ctx.reply(f"Insufficient coins. Balance **{bal}**.")
        # escrow
        c.execute("UPDATE users SET balance=balance-? WHERE discord_id=?", (amount, uid))
        c.execute("INSERT INTO tx(discord_id,kind,amount,meta,ts) VALUES(?,?,?,?,?)",
                  (uid, "bet", -amount, f"roulette:{rid}|{choice}", iso(now_local())))
        c.execute("INSERT INTO bets(rid,channel_id,discord_id,choice,stake,ts) VALUES(?,?,?,?,?,?)",
                  (rid, str(ctx.channel.id), uid, choice, amount, iso(now_local())))
    await ctx.message.add_reaction("✅")

@commands.has_permissions(manage_guild=True)
@bot.command(name="resolve")
async def resolve_round(ctx: commands.Context):
    o = get_open_round(ctx.channel.id)
    if not o:
        return await ctx.reply("No open round to resolve.")
    rid, _exp = o
    set_state(round_key(ctx.channel.id), None)   # after updating rounds.status

   
    # Build display label (NEW)
    rlabel = get_round_label(rid)
    
    # Optional: short seed in messages
    seed_display = short_seed(seed, 8)
    
    text = (f"🎯 **Round {rlabel} → {outcome.upper()}**\n"
            f"Total bets: **{len(rows)}** • Pool: **{total_pool}**\n"
            f"Winners (top): {', '.join(top_mentions) if top_mentions else 'None'}\n"
            f"Seed: `{seed_display}`")
    
    if msg_id:
        try:
            msg = await ctx.channel.fetch_message(msg_id)
            embed = msg.embeds[0] if msg.embeds else discord.Embed(color=discord.Color.gold())
            embed.title = f"🎯 Roulette — Round {rlabel}"
            embed.description = f"**RESULT:** {outcome.upper()}"
            embed.set_footer(text=f"Seed: {seed_display}")
            await msg.edit(content=None, embed=embed, view=None)  # remove buttons after resolve
            await ctx.send(text)  # <- if you want ONLY the embed, delete this line
        except Exception:
            await ctx.reply(text)



@commands.has_permissions(manage_guild=True)
@bot.command(name="cancelround")
async def cancelround(ctx: commands.Context):
    o = get_open_round(ctx.channel.id)
    if not o:
        return await ctx.reply("No open round to cancel.")
    rid, _ = o
    set_state(round_key(ctx.channel.id), None)
    # refund all
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT discord_id, stake FROM bets WHERE rid=?", (rid,))
        rows = c.fetchall()
        for uid, stake in rows:
            c.execute("UPDATE users SET balance=balance+? WHERE discord_id=?", (stake, uid))
            c.execute("INSERT INTO tx(discord_id,kind,amount,meta,ts) VALUES(?,?,?,?,?)",
                      (uid, "payout", stake, f"roulette:{rid}|refund", iso(now_local())))
        c.execute("UPDATE rounds SET status='CANCELLED', resolved_at=? WHERE rid=?", (iso(now_local()), rid))
    await ctx.reply(f"Round `{rid}` cancelled and bets refunded.")

# ---- Lotto (1 winner, 10 WL from Shop X) ----
@bot.command(name="buyticket")
async def buyticket(ctx: commands.Context, count: int = 1):
    if count <= 0 or count > 100:
        return await ctx.reply("You can buy between 1 and 100 tickets at once.")
    uid = str(ctx.author.id)
    cost = TICKET_COST * count
    bal = get_balance(uid)
    if bal < cost:
        return await ctx.reply(f"Not enough coins. Need **{cost}**, you have **{bal}**.")
    # burn coins and issue tickets
    with db() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET balance=balance-? WHERE discord_id=?", (cost, uid))
        c.execute("INSERT INTO tx(discord_id,kind,amount,meta,ts) VALUES(?,?,?,?,?)",
                  (uid, "redeem", -cost, f"tickets {count}", iso(now_local())))
        wk = week_id()
        for _ in range(count):
            c.execute("INSERT INTO tickets(week_id,discord_id,ts) VALUES(?,?,?)", (wk, uid, iso(now_local())))
    await ctx.reply(f"🎟️ Bought **{count}** ticket(s) for this week’s Lotto. Good luck!")

@bot.command(name="lotto")
async def lotto(ctx: commands.Context):
    wk = week_id()
    uid = str(ctx.author.id)
    
    draw_dt = next_draw_dt()
    draw_str = draw_dt.strftime("%a %d %b %Y • %I:%M %p %Z")  # e.g., Sat 18 Oct 2025 • 08:00 PM BST
    left = human_left(draw_dt)

    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM tickets WHERE week_id=?", (wk,))
        total = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM tickets WHERE week_id=? AND discord_id=?", (wk, uid))
        mine = c.fetchone()[0]
        embed.add_field(
        name="Draw time",
        value=f"{draw_str}  _(in {left})_",
        inline=False
    )

    await ctx.reply(f"🎟️ **Weekly Lotto** — Week {wk}\nTotal tickets: **{total}** • Your tickets: **{mine}**\nPrize: **{LOTTO_WL_COUNT} WL gifts** from **{SHOP_NAME}** to **1 winner**.")


@commands.has_permissions(manage_guild=True)
@bot.command(name="drawlotto")
async def drawlotto(ctx: commands.Context):
    wk = week_id()
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, discord_id FROM tickets WHERE week_id=?", (wk,))
        all_tix = c.fetchall()
    if not all_tix:
        return await ctx.reply(f"No tickets for Week {wk}.")

    # keep seed internal (not displayed)
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

    member = ctx.guild.get_member(int(winner_id))
    mention = member.mention if member else f"<@{winner_id}>"

    embed = discord.Embed(
    title="🎉 Weekly Lotto Winner!",
    description=(
        f"{mention} wins **{LOTTO_WL_COUNT}** wishlist gifts from "
        f"**[{SHOP_NAME}]({SHOP_YAELI_URL})**."
    ),
    color=discord.Color.gold()
)

    # Optional: include claim instructions
    # embed.add_field(name="How to claim", value=f"Add **10 items** from **[Shop YaEli]({SHOP_YAELI_URL})** to your wishlist, then press the button below.", inline=False)

    await ctx.reply(embed=embed, view=ClaimView(prize_id))


# ---- Prize fulfilment (admins) ----
@commands.has_permissions(manage_guild=True)
@bot.command(name="fulfil_next")
async def fulfil_next(ctx: commands.Context):
    with db() as conn:
        c = conn.cursor()
        c.execute("""SELECT pq.id, pq.prize_id, pq.winner_id, pq.imvu_name, pq.imvu_profile, p.amount, p.meta
                     FROM prize_queue pq
                     JOIN prizes p ON p.id = pq.prize_id
                     WHERE pq.status='ready'
                     ORDER BY pq.created_ts ASC
                     LIMIT 1""")
        row = c.fetchone()
    if not row:
        return await ctx.reply("No pending WL claims to fulfil.")
    pq_id, prize_id, winner_id, imvu_name, imvu_profile, amount, meta = row
    imvu_link = imvu_profile or f"https://www.imvu.com/catalog/web_mypage.php?av={imvu_name}"
    meta_obj = {}
    try:
        meta_obj = json.loads(meta or "{}")
    except Exception:
        pass
    await ctx.reply(
        f"Fulfil queue **#{pq_id}** → Prize **#{prize_id}** for <@{winner_id}>\n"
        f"IMVU: **{imvu_name}** • {imvu_link}\n"
        f"Gifts to send: **{amount}** from **{meta_obj.get('shop', SHOP_NAME)}**\n"
        f"After gifting, run `!fulfil_done {pq_id}`."
    )

@commands.has_permissions(manage_guild=True)
@bot.command(name="fulfil_done")
async def fulfil_done(ctx: commands.Context, queue_id: int):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT prize_id FROM prize_queue WHERE id=?", (queue_id,))
        row = c.fetchone()
        if not row:
            return await ctx.reply("Queue ID not found.")
        prize_id = row[0]
        c.execute("UPDATE prize_queue SET status='fulfilled', updated_ts=? WHERE id=?", (iso(now_local()), queue_id))
        c.execute("UPDATE prizes SET status='fulfilled', updated_ts=? WHERE id=?", (iso(now_local()), prize_id))
    await ctx.reply(f"Marked fulfilment queue **#{queue_id}** as fulfilled ✅")

# ==== Categorized Help ====
class EliHausHelp(commands.MinimalHelpCommand):
    def get_command_signature(self, command):
        return f"!{command.qualified_name} {command.signature}".strip()

    async def send_bot_help(self, mapping):
        # Define categories
        player_cmds = [
            "joinhaus", "daily", "weekly", "balance",
            "openround",  # players see it, but we’ll mark admin-only below
            "bet", "round",
            "buyticket", "lotto"
        ]
        admin_cmds = [
            "openround", "resolve", "cancelround",
            "deposit", "withdraw",
            "drawlotto",
            "fulfil_next", "fulfil_done",
            "lottoboard", "lottoexport", "roundreset"  # if you added these
        ]

        # Collect available commands
        all_cmds = {c.qualified_name: c for c in self.context.bot.commands}

        def fmt_list(names, admin=False):
            lines = []
            for n in names:
                c = all_cmds.get(n)
                if not c:
                    continue
                sig = self.get_command_signature(c)
                # mark admin-only commands
                tag = " *(admin)*" if admin else ""
                brief = f" — {c.brief}" if getattr(c, "brief", None) else ""
                lines.append(f"• `{sig}`{tag}{brief}")
            return "\n".join(lines) if lines else "_None_"

        # Build embed
        e = discord.Embed(
            title="📖 EliHaus Commands",
            color=discord.Color.gold(),
            description="**Tip:** Use `!help <command>` for details."
        )
        e.add_field(
            name="🎮 Player Commands",
            value=fmt_list(
                ["joinhaus","daily","weekly","balance","bet","round","buyticket","lotto"]
            ),
            inline=False
        )
        e.add_field(
            name="🛠️ Admin Commands",
            value=fmt_list(
                ["openround","resolve","cancelround","deposit","withdraw","drawlotto","fulfil_next","fulfil_done","lottoboard","lottoexport","roundreset"],
                admin=True
            ),
            inline=False
        )
        await self.get_destination().send(embed=e)

    async def send_command_help(self, command):
        e = discord.Embed(title=f"❓ Help: !{command.qualified_name}", color=discord.Color.gold())
        e.add_field(name="Usage", value=f"`{self.get_command_signature(command) or '!'+command.qualified_name}`", inline=False)
        if command.help:
            e.add_field(name="Description", value=command.help, inline=False)
        await self.get_destination().send(embed=e)
        
@is_admin()
@bot.command(name="roundreset", brief="(admin) Force-unlock this channel if a round is stuck")
async def roundreset(ctx):
    rid = get_state(round_key(ctx.channel.id))
    if not rid:
        return await ctx.reply("No open round to reset (state already clear).")
    with db() as conn:
        c = conn.cursor()
        c.execute("UPDATE rounds SET status='CANCELLED', resolved_at=? WHERE rid=?",
                  (iso(now_local()), rid))
    set_state(round_key(ctx.channel.id), None)
    await ctx.reply(f"Force-reset round `{rid}` — channel unlocked.")



# ---- Run ----
@bot.event
async def on_ready():
    print(f"[EliHaus] Logged in as {bot.user} | TZ={TIMEZONE_NAME}")

bot.run(TOKEN)
