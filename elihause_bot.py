# elihause_bot.py — EliHaus (coins + admin roulette + weekly lotto + prize queue) — SLASH ver (eh_*)
# Requires: pip install -U discord.py
import os, sqlite3, random, json, traceback
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands
from discord import app_commands
from zoneinfo import ZoneInfo  # proper DST (e.g., Europe/London)

# ---------------- Config ----------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN")

# Optional: fast guild sync during development
GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0"))

TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/London")
try:
    TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
    TZ = timezone.utc

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# Admin role (optional): users with Manage Server or this role ID are treated as admins
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))

# Economy
DAILY_AMOUNT = 1_800
WEEKLY_AMOUNT = 6_000
STARTER_AMOUNT = 5_000

# Lotto
TICKET_COST = 10_000
LOTTO_WINNERS = 1
LOTTO_WL_COUNT = 10
SHOP_NAME = "Shop YaEli"
SHOP_YAELI_URL = os.getenv(
    "SHOP_YAELI_URL",
    "https://www.imvu.com/shop/web_search.php?manufacturers_id=360644281"
)

# Roulette (admin-led)
ROUND_SECONDS_DEFAULT = 120
PAYOUT_RED_BLACK = 2.0
PAYOUT_GREEN = 14.0
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
init_db()

# ---------------- Time / State helpers ----------------
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

# ---------------- Admin check helpers ----------------
def user_is_admin(member: discord.Member) -> bool:
    if getattr(member.guild_permissions, "manage_guild", False) or member.id == getattr(member.guild, "owner_id", 0):
        return True
    if ADMIN_ROLE_ID and hasattr(member, "roles"):
        return any(getattr(r, "id", 0) == ADMIN_ROLE_ID for r in member.roles)
    return False

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

# ---------------- Views & Modals ----------------
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
            try:
                p = _u.urlparse(url)
                q = _u.parse_qs(p.query)
                if "av" in q and q["av"]:
                    uname = q["av"][0]
                else:
                    uname = p.path.strip("/").split("/")[-1] or None
            except Exception:
                uname = None
            profile_url = url
        else:
            uname = raw
            profile_url = f"https://www.imvu.com/catalog/web_mypage.php?av={uname}"
        wishlist_url = f"https://www.imvu.com/catalog/web_wishlist.php?av={uname}" if uname else None
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

        await ticket.send(
            f"{staff_tag} New WL claim for {interaction.user.mention}\n"
            f"IMVU: {profile_line}\n"
            f"Wishlist: {wishlist_line}\n"
            f"Notes: {str(self.note or '—')}\n\n"
            f"{policy}"
        )

        try:
            msg_id = get_state(_prize_msg_key(self.prize_id))
            if msg_id:
                msg = await interaction.channel.fetch_message(int(msg_id))
                await msg.edit(view=DisabledClaimView())
        except Exception:
            pass

        await interaction.response.send_message(f"✅ Ticket created: {ticket.mention}", ephemeral=True)

# --- Bet Modal for the buttons ---
class BetModal(discord.ui.Modal, title="Place your bet"):
    amount = discord.ui.TextInput(label="Amount (coins)", placeholder="e.g. 2500", required=True, max_length=12)

    def __init__(self, rid: str, color: str):
        super().__init__()
        self.rid = rid
        self.color = color

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amt = int(str(self.amount).strip().replace("_",""))
        except Exception:
            return await interaction.response.send_message("Enter a valid number.", ephemeral=True)

        if amt <= 0 or amt > MAX_STAKE:
            return await interaction.response.send_message(f"Stake must be between 1 and {MAX_STAKE}.", ephemeral=True)

        # Validate round still open
        with db() as conn:
            c = conn.cursor()
            c.execute("SELECT status, expires_at FROM rounds WHERE rid=?", (self.rid,))
            row = c.fetchone()
        if not row or row[0] != "OPEN":
            return await interaction.response.send_message("Betting window is closed.", ephemeral=True)
        try:
            exp_dt = datetime.fromisoformat(row[1])
        except Exception:
            exp_dt = now_local()
        if now_local() > exp_dt:
            return await interaction.response.send_message("Betting window is closed.", ephemeral=True)

        # --- refresh the round embed (pool, bets, time) ---
        try:
            with db() as conn:
                c = conn.cursor()
                c.execute("SELECT message_id, expires_at FROM rounds WHERE rid=?", (self.rid,))
                r = c.fetchone()
                if r and r[0]:
                    msg_id, exp_iso = r[0], r[1]
                    c.execute("SELECT COUNT(*), COALESCE(SUM(stake),0) FROM bets WHERE rid=?", (self.rid,))
                    cnt, pool = c.fetchone()

            # compute remaining seconds
            try:
                exp_dt = datetime.fromisoformat(exp_iso)
            except Exception:
                exp_dt = now_local()
            left = max(0, int((exp_dt - now_local()).total_seconds()))

            # fetch and update the embed
            msg = await interaction.channel.fetch_message(int(msg_id))
            if msg.embeds:
                e = msg.embeds[0]
                e.clear_fields()
                e.add_field(name="Pool", value=str(pool), inline=True)
                e.add_field(name="Time", value=f"{left}s left", inline=True)
                e.add_field(name="Bets", value=str(cnt), inline=True)
                await msg.edit(embed=e)
        except Exception:
            pass

        uid = str(interaction.user.id)
        # ensure user + balance checks
        with db() as conn:
            c = conn.cursor()
            c.execute("""INSERT OR IGNORE INTO users(discord_id,balance,last_daily,last_weekly,joined_at)
                         VALUES(?,?,?,?,?)""", (uid, 0, None, None, iso(now_local())))
            c.execute("SELECT balance FROM users WHERE discord_id=?", (uid,))
            row = c.fetchone()
            bal = row[0] if row else 0
        if bal < amt:
            return await interaction.response.send_message(f"Insufficient coins. Balance **{bal}**.", ephemeral=True)

        with db() as conn:
            c = conn.cursor()
            if ONE_BET_PER_ROUND:
                c.execute("SELECT 1 FROM bets WHERE rid=? AND discord_id=? LIMIT 1", (self.rid, uid))
                if c.fetchone():
                    return await interaction.response.send_message("You’ve already placed a bet this round.", ephemeral=True)
            c.execute("UPDATE users SET balance=balance-? WHERE discord_id=?", (amt, uid))
            c.execute("INSERT INTO tx(discord_id,kind,amount,meta,ts) VALUES(?,?,?,?,?)",
                      (uid, "bet", -amt, f"roulette:{self.rid}|{self.color}", iso(now_local())))
            c.execute("INSERT INTO bets(rid,channel_id,discord_id,choice,stake,ts) VALUES(?,?,?,?,?,?)",
                      (self.rid, str(interaction.channel.id), uid, self.color, amt, iso(now_local())))

        await interaction.response.send_message("✅ Bet placed!", ephemeral=True)

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

def change_balance(uid: str, delta: int, kind: str, meta: str = "") -> int:
    ensure_user(uid)
    with db() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET balance=balance+? WHERE discord_id=?", (delta, uid))
        c.execute("INSERT INTO tx(discord_id,kind,amount,meta,ts) VALUES(?,?,?,?,?)",
                  (uid, kind, delta, meta, iso(now_local())))
        c.execute("SELECT balance FROM users WHERE discord_id=?", (uid,))
        return c.fetchone()[0]

# ---- Help (slash) ----
@bot.tree.command(name="eh_help", description="Show EliHaus commands")
async def eh_help(interaction: discord.Interaction):
    is_admin = user_is_admin(interaction.user)
    public = [
        "`/eh_join` – join EliHaus (starter coins)",
        "`/eh_daily` – claim daily coins",
        "`/eh_weeklyw` – claim weekly coins",
        "`/eh_balance` – check balance",
        "`/eh_buyticket` – buy lotto tickets",
        "`/eh_lotto` – see lotto status",
        "`/eh_table` – active roulette round status",
    ]
    admin = [
        "`/eh_openround` – open roulette round",
        "`/eh_resolve` – resolve round",
        "`/eh_cancelround` – cancel round",
        "`/eh_deposit` / `/eh_withdraw` – adjust balance",
        "`/eh_drawlotto` – draw weekly winner",
        "`/eh_fulfil_next` / `/eh_fulfil_done` – fulfil WL claims",
        "`/eh_roundreset` – unlock stuck round",
    ]
    lines = public + (["\n**Admin**"] + admin if is_admin else [])
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

# ---- Player: join/daily/weekly/balance ----
@bot.tree.command(name="eh_join", description="Join EliHaus and get starter coins")
async def eh_join(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    ensure_user(uid)
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM tx WHERE discord_id=? AND kind='starter' LIMIT 1", (uid,))
        has_starter = c.fetchone() is not None
    if has_starter:
        return await interaction.response.send_message("You’ve already joined EliHaus. Use `/eh_daily` and `/eh_weeklyw` to build coins.", ephemeral=True)
    new_bal = change_balance(uid, STARTER_AMOUNT, "starter", "joinhaus starter")
    await interaction.response.send_message(f"Welcome to **EliHaus**. Starter pack: **{STARTER_AMOUNT}** coins. Balance: **{new_bal}**", ephemeral=True)

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

@bot.tree.command(name="eh_weeklyw", description="Claim your weekly coins")
async def eh_weeklyw(interaction: discord.Interaction):
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

@bot.tree.command(name="eh_balance", description="Check a balance")
@app_commands.describe(member="Member to check (optional)")
async def eh_balance(interaction: discord.Interaction, member: discord.Member | None = None):
    m = member or interaction.user
    bal = get_balance(str(m.id))
    await interaction.response.send_message(f"{m.mention} has **{bal}** coins.", ephemeral=True)

# ---- Admin: deposit/withdraw ----
@bot.tree.command(name="eh_deposit", description="(Admin) Deposit coins into a user's balance")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(member="Member to deposit to", amount="Positive amount")
async def eh_deposit(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not user_is_admin(interaction.user):
        return await interaction.response.send_message("You don’t have permission.", ephemeral=True)
    if amount <= 0:
        return await interaction.response.send_message("Amount must be positive.", ephemeral=True)
    new_bal = change_balance(str(member.id), amount, "adjust", f"deposit by {interaction.user.id}")
    await interaction.response.send_message(f"Deposited **{amount}** to {member.mention}. Balance: **{new_bal}**", ephemeral=True)

@bot.tree.command(name="eh_withdraw", description="(Admin) Withdraw coins from a user's balance")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(member="Member to withdraw from", amount="Positive amount")
async def eh_withdraw(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not user_is_admin(interaction.user):
        return await interaction.response.send_message("You don’t have permission.", ephemeral=True)
    if amount <= 0:
        return await interaction.response.send_message("Amount must be positive.", ephemeral=True)
    uid = str(member.id)
    bal = get_balance(uid)
    if bal < amount:
        return await interaction.response.send_message("Insufficient user balance.", ephemeral=True)
    new_bal = change_balance(uid, -amount, "adjust", f"withdraw by {interaction.user.id}")
    await interaction.response.send_message(f"Withdrew **{amount}** from {member.mention}. Balance: **{new_bal}**", ephemeral=True)

# ---- Roulette: open/status/resolve/cancel ----
@bot.tree.command(name="eh_openround", description="(Admin) Open a roulette round")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(seconds="Betting window (10-600s)")
async def eh_openround(interaction: discord.Interaction, seconds: int = ROUND_SECONDS_DEFAULT):
    if not user_is_admin(interaction.user):
        return await interaction.response.send_message("You don’t have permission.", ephemeral=True)
    seconds = max(10, min(seconds, 600))
    if get_open_round(interaction.channel.id):
        return await interaction.response.send_message("There’s already an open round in this channel.", ephemeral=True)
    rid, exp = open_round(interaction.channel.id, seconds, str(interaction.user.id))

    # user-friendly label like #1, #2 per channel
    rnum = ClaimView.next_round_number(interaction.channel.id)
    rlabel = f"#{rnum}"
    ClaimView.set_round_label(rid, rlabel)

    embed = discord.Embed(
        title=f"🎯 Roulette — Round {rlabel}",
        description="Click a button to bet. A modal will ask your amount.",
        color=discord.Color.gold()
    )
    embed.add_field(name="Pool", value="0", inline=True)
    embed.add_field(name="Time", value=f"{seconds}s left", inline=True)
    embed.add_field(name="Bets", value="0", inline=True)

    view = BetView(rid, timeout=seconds + 30)
    msg = await interaction.channel.send(embed=embed, view=view)
    with db() as conn:
        conn.execute("UPDATE rounds SET message_id=? WHERE rid=?", (str(msg.id), rid))
    await interaction.response.send_message(f"Opened roulette round {rlabel}.", ephemeral=True)

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

@bot.tree.command(name="eh_resolve", description="(Admin) Resolve the current roulette round")
@app_commands.default_permissions(manage_guild=True)
async def eh_resolve(interaction: discord.Interaction):
    if not user_is_admin(interaction.user):
        return await interaction.response.send_message("You don’t have permission.", ephemeral=True)
    o = get_open_round(interaction.channel.id)
    if not o:
        return await interaction.response.send_message("No open round to resolve.", ephemeral=True)
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
                  (outcome, seed, iso(now_local()), rid))
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

    summary_text = (f"🎯 **Round {rlabel} → {outcome.upper()}**\n"
                    f"Total bets: **{len(rows)}** • Pool: **{total_pool}**\n"
                    f"Winners (top): {', '.join(top_mentions) if top_mentions else 'None'}\n"
                    f"Seed: `{seed_display}`")

    if msg_id:
        try:
            msg = await interaction.channel.fetch_message(msg_id)
            e = msg.embeds[0] if msg.embeds else discord.Embed(color=discord.Color.gold())
            e.title = f"🎯 Roulette — Round {rlabel}"
            e.description = f"**RESULT:** {outcome.upper()}"
            e.set_footer(text=f"Seed: {seed_display}")
            await msg.edit(embed=e, view=None)
            await interaction.channel.send(summary_text)
        except Exception:
            await interaction.channel.send(summary_text)
    else:
        await interaction.channel.send(summary_text)

    await interaction.response.send_message("Round resolved.", ephemeral=True)

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
        return await interaction.response.send_message("You can buy between 1 and 100 tickets at once.", ephemeral=True)
    uid = str(interaction.user.id)
    cost = TICKET_COST * count
    bal = get_balance(uid)
    if bal < cost:
        return await interaction.response.send_message(f"Not enough coins. Need **{cost}**, you have **{bal}**.", ephemeral=True)
    with db() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET balance=balance-? WHERE discord_id=?", (cost, uid))
        c.execute("INSERT INTO tx(discord_id,kind,amount,meta,ts) VALUES(?,?,?,?,?)",
                  (uid, "redeem", -cost, f"tickets {count}", iso(now_local())))
        wk = week_id()
        for _ in range(count):
            c.execute("INSERT INTO tickets(week_id,discord_id,ts) VALUES(?,?,?)", (wk, uid, iso(now_local())))
    await interaction.response.send_message(f"🎟️ Bought **{count}** ticket(s) for this week’s Lotto. Good luck!", ephemeral=True)

@bot.tree.command(name="eh_lotto", description="Show weekly lotto status")
async def eh_lotto(interaction: discord.Interaction):
    wk = week_id()
    uid = str(interaction.user.id)
    draw_dt = next_draw_dt()
    draw_str = draw_dt.strftime("%a %d %b %Y • %I:%M %p %Z")
    left = human_left(draw_dt)
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM tickets WHERE week_id=?", (wk,))
        total = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM tickets WHERE week_id=? AND discord_id=?", (wk, uid))
        mine = c.fetchone()[0]
    await interaction.response.send_message(
        f"🎟️ **Weekly Lotto** — Week {wk}\n"
        f"Draw: **{draw_str}** _(in {left})_\n"
        f"Total tickets: **{total}** • Your tickets: **{mine}**\n"
        f"Prize: **{LOTTO_WL_COUNT} WL gifts** from **{SHOP_NAME}** to **{LOTTO_WINNERS}** winner.",
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

# ---- Prize fulfilment ----
@bot.tree.command(name="eh_fulfil_next", description="(Admin) Show next WL claim to fulfil")
@app_commands.default_permissions(manage_guild=True)
async def eh_fulfil_next(interaction: discord.Interaction):
    if not user_is_admin(interaction.user):
        return await interaction.response.send_message("You don’t have permission.", ephemeral=True)
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
        return await interaction.response.send_message("No pending WL claims to fulfil.", ephemeral=True)
    pq_id, prize_id, winner_id, imvu_name, imvu_profile, amount, meta = row
    imvu_link = imvu_profile or f"https://www.imvu.com/catalog/web_mypage.php?av={imvu_name}"
    meta_obj = {}
    try:
        meta_obj = json.loads(meta or "{}")
    except Exception:
        pass
    await interaction.response.send_message(
        f"Fulfil queue **#{pq_id}** → Prize **#{prize_id}** for <@{winner_id}>\n"
        f"IMVU: **{imvu_name}** • {imvu_link}\n"
        f"Gifts to send: **{amount}** from **{meta_obj.get('shop', SHOP_NAME)}**\n"
        f"After gifting, run `/eh_fulfil_done {pq_id}`.",
        ephemeral=True
    )

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

@bot.tree.command(name="eh_sync", description="(Admin) Force refresh EliHaus slash commands")
@app_commands.default_permissions(manage_guild=True)
async def eh_sync(interaction: discord.Interaction):
    try:
        # Prefer fast per-guild sync while testing
        gid = int(os.getenv("TEST_GUILD_ID", "0"))
        if gid:
            guild = discord.Object(id=gid)
            # Clear guild cmds then mirror current globals, then sync
            bot.tree.clear_commands(guild=guild)
            bot.tree.copy_global_to(guild=guild)
            cmds = await bot.tree.sync(guild=guild)
            return await interaction.response.send_message(f"✅ Synced {len(cmds)} commands to guild {gid}.", ephemeral=True)
        # Otherwise do global sync
        cmds = await bot.tree.sync()
        await interaction.response.send_message(f"✅ Globally synced {len(cmds)} commands.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Sync error: {e}", ephemeral=True)


bot.run(TOKEN)
