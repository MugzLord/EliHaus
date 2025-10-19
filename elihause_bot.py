# elihause_bot_full.py — EliHaus casino/bank bot (coins + admin roulette + weekly lotto + slots + WL withdraw tickets)
# Python 3.10+  |  discord.py 2.4+
# ------------------------------------------------------------
# Features
# - Coins economy (users, tx) with /eh_daily, /eh_weekly, /eh_balance, /eh_leaderboard
# - Admin-led Roulette with sticky table, bet modals, resolve/cancel/reset
# - Weekly Lotto (Sat 20:00 Europe/London), tickets, draw, prize queue
# - Emoji Slots (shared pot)
# - WL Gift Withdraw via ticket thread + admin Approve/Reject; prize queue fulfilment helpers
# - SQLite persistence; environment-configurable
# ------------------------------------------------------------

import os, sqlite3, random, math, json, asyncio, traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from discord import app_commands

# ---------------- Config ----------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN")

GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0"))  # optional fast sync
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/London")
try:
    TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
    TZ = timezone.utc

ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))  # optional extra admin role
DB_PATH = os.getenv("ELIHAUS_DB", "elihause.db")

# Roulette tunables
ROULETTE_BET_TIMEOUT_S = int(os.getenv("ROULETTE_BET_TIMEOUT_S", "90"))
ROULETTE_MAX_STAKE = int(os.getenv("ROULETTE_MAX_STAKE", "100000"))
ROULETTE_ONE_BET_PER_ROUND = True

# WL Conversion
WL_COINS_PER_GIFT = int(os.getenv("WL_COINS_PER_GIFT", "5000"))
MIN_WL_GIFTS = int(os.getenv("MIN_WL_GIFTS", "1"))
MAX_WL_GIFTS = int(os.getenv("MAX_WL_GIFTS", "10"))

# Tickets / staff
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0"))  # category where WL tickets will be created
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0"))            # pinged on new ticket

# Slots (shared pot)
SLOTS_SPIN_COST = int(os.getenv("SLOTS_SPIN_COST", "250"))
SLOTS_MIN_POT = int(os.getenv("SLOTS_MIN_POT", "5000"))
SLOTS_TRIPLE_PCT = float(os.getenv("SLOTS_TRIPLE_PCT", "0.6"))  # 60% of pot
SLOTS_DOUBLE_PAY = int(os.getenv("SLOTS_DOUBLE_PAY", "750"))

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
bot = commands.Bot(command_prefix="!", intents=INTENTS)

def is_admin(member: discord.Member) -> bool:
    if member.guild_permissions.manage_guild:
        return True
    if ADMIN_ROLE_ID and any(r.id == ADMIN_ROLE_ID for r in member.roles):
        return True
    return False

# ---------------- DB helpers ----------------
SCHEMA = r"""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  coins   INTEGER NOT NULL DEFAULT 0,
  last_daily TEXT,
  last_weekly TEXT
);
CREATE TABLE IF NOT EXISTS tx (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  amount INTEGER NOT NULL,
  note TEXT
);
-- Roulette
CREATE TABLE IF NOT EXISTS rounds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  table_message_id INTEGER,
  status TEXT NOT NULL, -- open/resolved/cancelled
  opened_ts TEXT NOT NULL,
  closes_ts TEXT NOT NULL,
  seed TEXT,
  result INTEGER,
  round_no INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS bets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL, -- red/black/green/number
  selection TEXT NOT NULL,
  stake INTEGER NOT NULL
);
-- Lotto
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lotto_draws (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL,
  draw_ts TEXT NOT NULL,
  winner_id INTEGER,
  seed TEXT
);
-- Prizes & queue (for WL gifts, lotto, etc)
CREATE TABLE IF NOT EXISTS prizes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL,
  created_ts TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL, -- wl_gift, lotto
  amount INTEGER NOT NULL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS prize_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prize_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', -- pending/done
  taken_by INTEGER,
  taken_ts TEXT,
  done_ts TEXT
);
-- Slots state
CREATE TABLE IF NOT EXISTS state (
  k TEXT PRIMARY KEY,
  v TEXT
);
"""

RED_SET = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
BLACK_SET = set(range(1,37)) - RED_SET

async def adb():
    return sqlite3.connect(DB_PATH)

def ensure_schema():
    con = sqlite3.connect(DB_PATH)
    try:
        con.executescript(SCHEMA)
        # Ensure slots pot
        cur = con.execute("SELECT v FROM state WHERE k='slots_pot'")
        row = cur.fetchone()
        if not row:
            con.execute("INSERT OR REPLACE INTO state(k,v) VALUES('slots_pot', ?)", (str(SLOTS_MIN_POT),))
        con.commit()
    finally:
        con.close()

@bot.event
async def on_ready():
    ensure_schema()
    try:
        if GUILD_ID:
            guild = bot.get_guild(GUILD_ID)
            if guild:
                await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
    except Exception:
        traceback.print_exc()
    print(f"Logged in as {bot.user} | Slash commands synced")

# ---------------- Economy ----------------
async def change_balance(user_id: int, delta: int, kind: str, note: str = ""):
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("INSERT OR IGNORE INTO users(user_id, coins) VALUES(?,0)", (user_id,))
        cur = con.execute("SELECT coins FROM users WHERE user_id=?", (user_id,))
        bal = cur.fetchone()[0]
        new_bal = max(0, bal + delta)
        con.execute("UPDATE users SET coins=? WHERE user_id=?", (new_bal, user_id))
        con.execute("INSERT INTO tx(ts,user_id,kind,amount,note) VALUES(?,?,?,?,?)",
                    (datetime.now(TZ).isoformat(), user_id, kind, delta, note))
        con.commit()
        return new_bal
    finally:
        con.close()

async def get_balance(user_id: int) -> int:
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("SELECT coins FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return row[0] if row else 0
    finally:
        con.close()

# Daily & Weekly
@bot.tree.command(name="eh_daily", description="Claim your daily coins")
async def eh_daily(inter: discord.Interaction):
    uid = inter.user.id
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("INSERT OR IGNORE INTO users(user_id, coins) VALUES(?,0)", (uid,))
        cur = con.execute("SELECT last_daily FROM users WHERE user_id=?", (uid,))
        last = cur.fetchone()[0]
        now = datetime.now(TZ)
        grant = 1000
        if last:
            last_dt = datetime.fromisoformat(last)
            if (now.date() == last_dt.date()):
                await inter.response.send_message("You already claimed daily today.", ephemeral=True)
                return
        bal = await change_balance(uid, grant, "daily", "daily grant")
        con.execute("UPDATE users SET last_daily=? WHERE user_id=?", (now.isoformat(), uid))
        con.commit()
    finally:
        con.close()
    await inter.response.send_message(f"Daily +{grant} coins. Balance: {bal}", ephemeral=True)

@bot.tree.command(name="eh_weekly", description="Claim your weekly coins")
async def eh_weekly(inter: discord.Interaction):
    uid = inter.user.id
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("INSERT OR IGNORE INTO users(user_id, coins) VALUES(?,0)", (uid,))
        cur = con.execute("SELECT last_weekly FROM users WHERE user_id=?", (uid,))
        last = cur.fetchone()[0]
        now = datetime.now(TZ)
        grant = 5000
        if last:
            last_dt = datetime.fromisoformat(last)
            if (now - last_dt) < timedelta(days=7):
                await inter.response.send_message("You already claimed weekly.", ephemeral=True)
                return
        bal = await change_balance(uid, grant, "weekly", "weekly grant")
        con.execute("UPDATE users SET last_weekly=? WHERE user_id=?", (now.isoformat(), uid))
        con.commit()
    finally:
        con.close()
    await inter.response.send_message(f"Weekly +{grant} coins. Balance: {bal}", ephemeral=True)

@bot.tree.command(name="eh_balance", description="Check your or someone’s coin balance")
@app_commands.describe(member="Optional member to check")
async def eh_balance(inter: discord.Interaction, member: discord.Member | None = None):
    member = member or inter.user
    bal = await get_balance(member.id)
    await inter.response.send_message(f"{member.mention} has **{bal}** coins.", ephemeral=True)

# ---------------- Roulette ----------------
class BetModal(discord.ui.Modal, title="Place Bet"):
    stake = discord.ui.TextInput(label="Stake (coins)", max_length=10)
    selection = discord.ui.TextInput(label="Selection (number or leave blank)", required=False)

    def __init__(self, kind: str, round_id: int):
        super().__init__()
        self.kind = kind  # red/black/green/number
        self.round_id = round_id

    async def on_submit(self, inter: discord.Interaction):
        try:
            stake = int(str(self.stake).strip())
        except Exception:
            await inter.response.send_message("Invalid stake.", ephemeral=True)
            return
        if stake <= 0 or stake > ROULETTE_MAX_STAKE:
            await inter.response.send_message("Stake out of range.", ephemeral=True)
            return
        sel = str(self.selection).strip()
        if self.kind == "number":
            if not sel.isdigit():
                await inter.response.send_message("Enter a number 0-36.", ephemeral=True)
                return
            num = int(sel)
            if num < 0 or num > 36:
                await inter.response.send_message("Enter a number 0-36.", ephemeral=True)
                return
            selection = sel
        elif self.kind in ("red","black"):
            selection = self.kind
        else:
            selection = "0"

        # Validate round open and one-bet rule
        con = sqlite3.connect(DB_PATH)
        try:
            cur = con.execute("SELECT status, closes_ts FROM rounds WHERE id=?", (self.round_id,))
            row = cur.fetchone()
            if not row or row[0] != "open":
                await inter.response.send_message("Round is not open.", ephemeral=True)
                return
            closes_ts = datetime.fromisoformat(row[1])
            if datetime.now(TZ) >= closes_ts:
                await inter.response.send_message("Betting closed.", ephemeral=True)
                return
            if ROULETTE_ONE_BET_PER_ROUND:
                cur = con.execute("SELECT 1 FROM bets WHERE round_id=? AND user_id=?", (self.round_id, inter.user.id))
                if cur.fetchone():
                    await inter.response.send_message("You already placed a bet.", ephemeral=True)
                    return
        finally:
            con.close()

        bal = await get_balance(inter.user.id)
        if bal < stake:
            await inter.response.send_message("Insufficient balance.", ephemeral=True)
            return
        await change_balance(inter.user.id, -stake, "roulette_bet", f"round:{self.round_id}")
        con = sqlite3.connect(DB_PATH)
        try:
            con.execute("INSERT INTO bets(round_id,user_id,kind,selection,stake) VALUES(?,?,?,?,?)",
                        (self.round_id, inter.user.id, self.kind, selection, stake))
            con.commit()
        finally:
            con.close()
        await inter.response.send_message(f"Bet placed: {self.kind} {selection} for {stake} coins.", ephemeral=True)

class TableView(discord.ui.View):
    def __init__(self, round_id: int):
        super().__init__(timeout=None)
        self.round_id = round_id

    @discord.ui.button(label="Bet RED", style=discord.ButtonStyle.danger)
    async def bet_red(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_modal(BetModal("red", self.round_id))

    @discord.ui.button(label="Bet BLACK", style=discord.ButtonStyle.secondary)
    async def bet_black(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_modal(BetModal("black", self.round_id))

    @discord.ui.button(label="Bet 0 (GREEN)", style=discord.ButtonStyle.success)
    async def bet_green(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_modal(BetModal("green", self.round_id))

    @discord.ui.button(label="Bet NUMBER", style=discord.ButtonStyle.primary)
    async def bet_number(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_modal(BetModal("number", self.round_id))

    @discord.ui.button(label="My Bet", style=discord.ButtonStyle.secondary)
    async def my_bet(self, inter: discord.Interaction, button: discord.ui.Button):
        con = sqlite3.connect(DB_PATH)
        try:
            cur = con.execute("SELECT kind, selection, stake FROM bets WHERE round_id=? AND user_id=?",
                              (self.round_id, inter.user.id))
            row = cur.fetchone()
        finally:
            con.close()
        if not row:
            await inter.response.send_message("You have no bet yet.", ephemeral=True)
        else:
            await inter.response.send_message(f"Your bet: {row[0]} {row[1]} for {row[2]} coins.", ephemeral=True)

async def render_table_embed(round_id: int) -> discord.Embed:
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("SELECT status, opened_ts, closes_ts, result, round_no FROM rounds WHERE id=?", (round_id,))
        r = cur.fetchone()
        status, opened_ts, closes_ts, result, round_no = r
        cur = con.execute("SELECT COUNT(*), COALESCE(SUM(stake),0) FROM bets WHERE round_id=?", (round_id,))
        c, s = cur.fetchone()
    finally:
        con.close()
    opened = datetime.fromisoformat(opened_ts).astimezone(TZ)
    closes = datetime.fromisoformat(closes_ts).astimezone(TZ)
    emb = discord.Embed(title=f"Roulette — Round #{round_no}", colour=discord.Colour.gold())
    emb.add_field(name="Status", value=status)
    emb.add_field(name="Bets", value=str(c))
    emb.add_field(name="Pool", value=str(s))
    emb.add_field(name="Opens", value=discord.utils.format_dt(opened, style='t'))
    emb.add_field(name="Closes", value=discord.utils.format_dt(closes, style='t'))
    if result is not None:
        emb.add_field(name="Result", value=str(result), inline=False)
    emb.set_footer(text="Bet responsibly.")
    return emb

@bot.tree.command(name="eh_openround", description="(Admin) Open a roulette round")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(duration_s="Betting window in seconds")
async def eh_openround(inter: discord.Interaction, duration_s: int = ROULETTE_BET_TIMEOUT_S):
    now = datetime.now(TZ)
    closes = now + timedelta(seconds=duration_s)
    con = sqlite3.connect(DB_PATH)
    try:
        # find last round no in this channel
        cur = con.execute("SELECT COALESCE(MAX(round_no),0) FROM rounds WHERE guild_id=? AND channel_id=?",
                          (inter.guild_id, inter.channel_id))
        next_no = (cur.fetchone()[0] or 0) + 1
        cur = con.execute(
            "INSERT INTO rounds(guild_id,channel_id,status,opened_ts,closes_ts,round_no) VALUES(?,?,?,?,?,?)",
            (inter.guild_id, inter.channel_id, "open", now.isoformat(), closes.isoformat(), next_no))
        rid = cur.lastrowid
        con.commit()
    finally:
        con.close()
    view = TableView(rid)
    emb = await render_table_embed(rid)
    await inter.response.send_message(embed=emb, view=view)
    msg = await inter.original_response()
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("UPDATE rounds SET table_message_id=? WHERE id=?", (msg.id, rid))
        con.commit()
    finally:
        con.close()

@bot.tree.command(name="eh_table", description="Show current roulette table (sticky)")
async def eh_table(inter: discord.Interaction):
    # find latest open or last round in this channel
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("SELECT id FROM rounds WHERE guild_id=? AND channel_id=? ORDER BY id DESC LIMIT 1",
                          (inter.guild_id, inter.channel_id))
        row = cur.fetchone()
    finally:
        con.close()
    if not row:
        await inter.response.send_message("No rounds yet.", ephemeral=True)
        return
    rid = row[0]
    emb = await render_table_embed(rid)
    await inter.response.send_message(embed=emb, view=TableView(rid))

@bot.tree.command(name="eh_resolve", description="(Admin) Resolve the current roulette round")
@app_commands.checks.has_permissions(manage_guild=True)
async def eh_resolve(inter: discord.Interaction):
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("SELECT id, status FROM rounds WHERE guild_id=? AND channel_id=? ORDER BY id DESC LIMIT 1",
                          (inter.guild_id, inter.channel_id))
        row = cur.fetchone()
        if not row:
            await inter.response.send_message("No round.", ephemeral=True)
            return
        rid, status = row
        if status != "open":
            await inter.response.send_message("Round is not open.", ephemeral=True)
            return
        # close bets
        con.execute("UPDATE rounds SET status='resolving' WHERE id=?", (rid,))
        con.commit()
        # spin
        result = random.randint(0,36)
        seed = os.urandom(8).hex()
        # compute payouts
        cur = con.execute("SELECT user_id, kind, selection, stake FROM bets WHERE round_id=?", (rid,))
        rows = cur.fetchall()
        winners = []
        for uid, kind, selection, stake in rows:
            pay = 0
            if result == 0:
                if kind == 'green':
                    pay = stake * 35
            else:
                if kind == 'red' and result in RED_SET:
                    pay = stake * 2
                elif kind == 'black' and result in BLACK_SET:
                    pay = stake * 2
                elif kind == 'number' and selection.isdigit() and int(selection) == result:
                    pay = stake * 36
            if pay:
                winners.append((uid, pay))
        # update balances
        for uid, pay in winners:
            await change_balance(uid, pay, "roulette_payout", f"round:{rid}")
        con.execute("UPDATE rounds SET status='resolved', result=?, seed=? WHERE id=?", (result, seed, rid))
        con.commit()
    finally:
        con.close()
    emb = await render_table_embed(rid)
    await inter.response.send_message(f"Result: **{result}**", embed=emb)

@bot.tree.command(name="eh_cancelround", description="(Admin) Cancel the current roulette round and refund")
@app_commands.checks.has_permissions(manage_guild=True)
async def eh_cancelround(inter: discord.Interaction):
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("SELECT id, status FROM rounds WHERE guild_id=? AND channel_id=? ORDER BY id DESC LIMIT 1",
                          (inter.guild_id, inter.channel_id))
        row = cur.fetchone()
        if not row:
            await inter.response.send_message("No round.", ephemeral=True)
            return
        rid, status = row
        if status != "open":
            await inter.response.send_message("Round is not open.", ephemeral=True)
            return
        # refund
        cur = con.execute("SELECT user_id, stake FROM bets WHERE round_id=?", (rid,))
        for uid, stake in cur.fetchall():
            await change_balance(uid, stake, "roulette_refund", f"round:{rid}")
        con.execute("UPDATE rounds SET status='cancelled' WHERE id=?", (rid,))
        con.commit()
    finally:
        con.close()
    await inter.response.send_message("Round cancelled and bets refunded.")

@bot.tree.command(name="eh_roundreset", description="(Admin) Unlock/Reset stuck round state")
@app_commands.checks.has_permissions(manage_guild=True)
async def eh_roundreset(inter: discord.Interaction):
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("UPDATE rounds SET status='cancelled' WHERE status NOT IN ('resolved','cancelled') AND guild_id=? AND channel_id=?",
                    (inter.guild_id, inter.channel_id))
        con.commit()
    finally:
        con.close()
    await inter.response.send_message("Any stuck rounds marked as cancelled.", ephemeral=True)

# ---------------- Leaderboard ----------------
@bot.tree.command(name="eh_leaderboard", description="Top coin balances")
async def eh_leaderboard(inter: discord.Interaction):
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("SELECT user_id, coins FROM users ORDER BY coins DESC LIMIT 10")
        rows = cur.fetchall()
    finally:
        con.close()
    lines = []
    for i,(uid,coins) in enumerate(rows,1):
        user = inter.guild.get_member(uid) or await inter.client.fetch_user(uid)
        lines.append(f"**{i}.** {user.mention if user else uid}: {coins}")
    emb = discord.Embed(title="Top Rinsers", description="\n".join(lines) if lines else "No data yet.")
    await inter.response.send_message(embed=emb)

# ---------------- Lotto ----------------
@bot.tree.command(name="eh_lotto", description="Show weekly lotto status (draw Sat 20:00 London)")
async def eh_lotto(inter: discord.Interaction):
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("SELECT COUNT(*) FROM tickets WHERE guild_id=?", (inter.guild_id,))
        count = cur.fetchone()[0]
    finally:
        con.close()
    emb = discord.Embed(title="Weekly Lotto", description=f"Tickets sold: **{count}**\nDraw: Saturday 20:00 Europe/London")
    await inter.response.send_message(embed=emb)

@bot.tree.command(name="eh_buyticket", description="Buy a lotto ticket")
async def eh_buyticket(inter: discord.Interaction):
    cost = 500
    bal = await get_balance(inter.user.id)
    if bal < cost:
        await inter.response.send_message("Not enough coins.", ephemeral=True)
        return
    await change_balance(inter.user.id, -cost, "lotto_buy", "ticket")
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("INSERT INTO tickets(guild_id,user_id,ts) VALUES(?,?,?)",
                    (inter.guild_id, inter.user.id, datetime.now(TZ).isoformat()))
        con.commit()
    finally:
        con.close()
    await inter.response.send_message("Ticket purchased!", ephemeral=True)

@bot.tree.command(name="eh_drawlotto", description="(Admin) Draw weekly lotto winner")
@app_commands.checks.has_permissions(manage_guild=True)
async def eh_drawlotto(inter: discord.Interaction):
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("SELECT user_id FROM tickets WHERE guild_id=?", (inter.guild_id,))
        rows = cur.fetchall()
        if not rows:
            await inter.response.send_message("No tickets.", ephemeral=True)
            return
        seed = os.urandom(8).hex()
        winner_uid = random.choice(rows)[0]
        # clear tickets (one weekly pool)
        con.execute("DELETE FROM tickets WHERE guild_id=?", (inter.guild_id,))
        con.execute("INSERT INTO lotto_draws(guild_id, draw_ts, winner_id, seed) VALUES(?,?,?,?)",
                    (inter.guild_id, datetime.now(TZ).isoformat(), winner_uid, seed))
        # create prize (credits or WL gift as you prefer); here: 10000 coins prize
        con.execute("INSERT INTO prizes(guild_id,created_ts,user_id,kind,amount,note) VALUES(?,?,?,?,?,?)",
                    (inter.guild_id, datetime.now(TZ).isoformat(), winner_uid, 'lotto', 10000, 'Weekly lotto'))
        pid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute("INSERT INTO prize_queue(prize_id,status) VALUES(?, 'pending')", (pid,))
        con.commit()
    finally:
        con.close()
    winner = inter.guild.get_member(winner_uid) or await inter.client.fetch_user(winner_uid)
    await inter.response.send_message(f"🎉 Lotto winner: {winner.mention if winner else winner_uid} (seed {seed})")

# ---------------- Slots (shared pot) ----------------
SLOT_SYMBOLS = ["🍒","🍋","🔔","⭐","🍀","7️⃣"]

async def get_slots_pot(con) -> int:
    cur = con.execute("SELECT v FROM state WHERE k='slots_pot'")
    return int(cur.fetchone()[0])

async def set_slots_pot(con, val: int):
    con.execute("UPDATE state SET v=? WHERE k='slots_pot'", (str(max(val, SLOTS_MIN_POT)),))

class SlotsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Spin", style=discord.ButtonStyle.primary)
    async def spin(self, inter: discord.Interaction, button: discord.ui.Button):
        uid = inter.user.id
        bal = await get_balance(uid)
        if bal < SLOTS_SPIN_COST:
            await inter.response.send_message("Not enough coins for a spin.", ephemeral=True)
            return
        await change_balance(uid, -SLOTS_SPIN_COST, "slots_spin", "spin")
        reels = [random.choice(SLOT_SYMBOLS) for _ in range(3)]
        con = sqlite3.connect(DB_PATH)
        try:
            pot = await get_slots_pot(con)
            # evaluate
            payout = 0
            if reels[0] == reels[1] == reels[2]:
                payout = math.floor(pot * SLOTS_TRIPLE_PCT)
            elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
                payout = SLOTS_DOUBLE_PAY
            # update pot
            pot += SLOTS_SPIN_COST
            pot -= payout
            await set_slots_pot(con, pot)
            con.commit()
        finally:
            con.close()
        if payout:
            await change_balance(uid, payout, "slots_payout", "slots")
        desc = f"Result: {' '.join(reels)}\nPayout: {payout} coins\nSpin cost: {SLOTS_SPIN_COST}"
        await inter.response.send_message(desc, ephemeral=True)

@bot.tree.command(name="eh_slots", description="Emoji slots (shared pot)")
async def eh_slots(inter: discord.Interaction):
    con = sqlite3.connect(DB_PATH)
    try:
        pot = await get_slots_pot(con)
    finally:
        con.close()
    emb = discord.Embed(title="Emoji Slots", description=f"Shared Pot: **{pot}**\nSpin cost: {SLOTS_SPIN_COST}")
    await inter.response.send_message(embed=emb, view=SlotsView())

# ---------------- WL Withdraw (ticket) ----------------
class WithdrawModal(discord.ui.Modal, title="Withdraw to WL Gifts"):
    qty = discord.ui.TextInput(label=f"Number of gifts ({MIN_WL_GIFTS}-{MAX_WL_GIFTS})", max_length=3)
    imvu_profile = discord.ui.TextInput(label="IMVU profile link or username", style=discord.TextStyle.short, max_length=100)

    async def on_submit(self, inter: discord.Interaction):
        try:
            q = int(str(self.qty).strip())
        except Exception:
            await inter.response.send_message("Invalid number.", ephemeral=True)
            return
        if q < MIN_WL_GIFTS or q > MAX_WL_GIFTS:
            await inter.response.send_message("Quantity out of range.", ephemeral=True)
            return
        cost = q * WL_COINS_PER_GIFT
        bal = await get_balance(inter.user.id)
        if bal < cost:
            await inter.response.send_message(f"You need {cost} coins but have {bal}.", ephemeral=True)
            return
        # Create ticket thread in category
        category = inter.guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
        if not category or not isinstance(category, discord.CategoryChannel):
            await inter.response.send_message("Ticket category not set.", ephemeral=True)
            return
        chan = await inter.guild.create_text_channel(
            name=f"wl-{inter.user.name}-{q}", category=category, topic=f"WL withdraw request by {inter.user.id}")
        staff_mention = f"<@&{STAFF_ROLE_ID}>" if STAFF_ROLE_ID else "Staff"
        view = TicketReviewView(requester_id=inter.user.id, qty=q, imvu=str(self.imvu_profile))
        await chan.send(f"{staff_mention} New WL withdraw request.")
        await chan.send(
            embed=discord.Embed(title="WL Withdraw Request",
                                 description=f"User: {inter.user.mention}\nGifts: **{q}** (cost {cost})\nIMVU: {self.imvu_profile}"),
            view=view
        )
        await inter.response.send_message(f"Ticket opened: {chan.mention}. Staff will review shortly.", ephemeral=True)

class TicketReviewView(discord.ui.View):
    def __init__(self, requester_id: int, qty: int, imvu: str):
        super().__init__(timeout=None)
        self.requester_id = requester_id
        self.qty = qty
        self.imvu = imvu

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, inter: discord.Interaction, button: discord.ui.Button):
        if not is_admin(inter.user):
            await inter.response.send_message("Admin only.", ephemeral=True)
            return
        cost = self.qty * WL_COINS_PER_GIFT
        bal = await get_balance(self.requester_id)
        if bal < cost:
            await inter.response.send_message("Requester has insufficient coins.", ephemeral=True)
            return
        await change_balance(self.requester_id, -cost, "wl_withdraw", f"{self.qty}x gifts")
        # enqueue prize for fulfilment (amount = qty gifts)
        con = sqlite3.connect(DB_PATH)
        try:
            con.execute("INSERT INTO prizes(guild_id,created_ts,user_id,kind,amount,note) VALUES(?,?,?,?,?,?)",
                        (inter.guild_id, datetime.now(TZ).isoformat(), self.requester_id, 'wl_gift', self.qty, self.imvu))
            pid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            con.execute("INSERT INTO prize_queue(prize_id,status) VALUES(?, 'pending')", (pid,))
            con.commit()
        finally:
            con.close()
        await inter.message.edit(view=None)
        await inter.response.send_message("Approved and queued for fulfilment.")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def reject(self, inter: discord.Interaction, button: discord.ui.Button):
        if not is_admin(inter.user):
            await inter.response.send_message("Admin only.", ephemeral=True)
            return
        await inter.message.edit(view=None)
        await inter.response.send_message("Request rejected.")

@bot.tree.command(name="eh_withdraw", description="Open a WL withdraw ticket")
async def eh_withdraw(inter: discord.Interaction):
    await inter.response.send_modal(WithdrawModal())

# ---------------- Prize fulfilment helpers ----------------
@bot.tree.command(name="eh_fulfil_next", description="(Admin) Take the next pending prize from queue")
@app_commands.checks.has_permissions(manage_guild=True)
async def eh_fulfil_next(inter: discord.Interaction):
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute(
            "SELECT pq.id, p.id, p.user_id, p.kind, p.amount, p.note FROM prize_queue pq JOIN prizes p ON p.id=pq.prize_id WHERE pq.status='pending' ORDER BY pq.id ASC LIMIT 1")
        row = cur.fetchone()
        if not row:
            await inter.response.send_message("No pending prizes.", ephemeral=True)
            return
        qid, pid, uid, kind, amount, note = row
        con.execute("UPDATE prize_queue SET status='taken', taken_by=?, taken_ts=? WHERE id=?",
                    (inter.user.id, datetime.now(TZ).isoformat(), qid))
        con.commit()
    finally:
        con.close()
    user = inter.guild.get_member(uid) or await inter.client.fetch_user(uid)
    await inter.response.send_message(f"Taken prize #{pid} for {user.mention if user else uid}: {kind} x{amount} ({note}).", ephemeral=True)

@bot.tree.command(name="eh_fulfil_done", description="(Admin) Mark a taken prize as done by prize ID")
@app_commands.describe(prize_id="Prize ID")
@app_commands.checks.has_permissions(manage_guild=True)
async def eh_fulfil_done(inter: discord.Interaction, prize_id: int):
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("SELECT id FROM prize_queue WHERE prize_id=? AND status='taken'", (prize_id,))
        row = cur.fetchone()
        if not row:
            await inter.response.send_message("Prize not taken or not found.", ephemeral=True)
            return
        qid = row[0]
        con.execute("UPDATE prize_queue SET status='done', done_ts=? WHERE id=?",
                    (datetime.now(TZ).isoformat(), qid))
        con.commit()
    finally:
        con.close()
    await inter.response.send_message(f"Prize #{prize_id} marked done.", ephemeral=True)

# ---------------- Utility ----------------
@bot.tree.command(name="eh_sync", description="(Admin) Resync slash commands")
@app_commands.checks.has_permissions(manage_guild=True)
async def eh_sync(inter: discord.Interaction):
    if GUILD_ID:
        guild = bot.get_guild(GUILD_ID)
        if guild:
            await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()
    await inter.response.send_message("Synced.", ephemeral=True)

# ------------------------------------------------------------
if __name__ == "__main__":
    ensure_schema()
    bot.run(TOKEN)
