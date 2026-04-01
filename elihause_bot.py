import os
import sqlite3
import random
import json
import traceback
import io
import time
import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict
from urllib.parse import urlparse, parse_qs

import discord
from discord.ext import commands
from discord import app_commands
from zoneinfo import ZoneInfo

# ---------------- Config ----------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN in environment variables.")

GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0"))
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/London")
try:
    TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
    TZ = timezone.utc

# Economy Constants
DAILY_AMOUNT = 1_800
WEEKLY_AMOUNT = 6_000
STARTER_AMOUNT = 5_000
TICKET_COST = 10_000
WL_COINS_PER_GIFT = 100000  # Used for prize conversion

# Roulette Config
ROUND_SECONDS_DEFAULT = 120
PAYOUT_RED_BLACK = 2.0
PAYOUT_GREEN = 14.0
PAYOUT_NUMBER = 36.0
ROUL_MIN_BET = 100
ROUL_MAX_BET = 999_999_999_999_999 # Effectively no limit

# Roles & Channels
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))
SHOP_YAELI_URL = os.getenv("SHOP_YAELI_URL", "")
SHOP_NAME = "EliHaus Shop"

# Paths
DB_PATH = os.getenv("ELI_DB_PATH") or "elihaus.db"

RED_NUMBERS = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}

# ---------------- Initialization ----------------
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
bot = commands.Bot(command_prefix="!", intents=INTENTS)

def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            discord_id TEXT PRIMARY KEY, balance INTEGER DEFAULT 0,
            last_daily TEXT, last_weekly TEXT, joined_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS tx(
            id INTEGER PRIMARY KEY, discord_id TEXT, kind TEXT, amount INTEGER, meta TEXT, ts TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, val TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS rounds(
            rid TEXT PRIMARY KEY, channel_id TEXT, status TEXT, opened_by TEXT, 
            opened_at TEXT, expires_at TEXT, outcome TEXT, seed TEXT, resolved_at TEXT, message_id TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS bets(
            id INTEGER PRIMARY KEY, rid TEXT, channel_id TEXT, discord_id TEXT, choice TEXT, stake INTEGER, ts TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY, week_id TEXT, discord_id TEXT, ts TEXT)""")
init_db()

# ---------------- Helpers ----------------

def user_is_admin(itx: discord.Interaction) -> bool:
    if itx.user.guild_permissions.administrator:
        return True
    if itx.user.guild_permissions.manage_guild:
        return True
    if ADMIN_ROLE_ID and any(r.id == ADMIN_ROLE_ID for r in itx.user.roles):
        return True
    return False

def get_balance(uid: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT balance FROM users WHERE discord_id=?", (str(uid),))
        row = c.fetchone()
        return row[0] if row else 0

def change_balance(uid: str, delta: int, kind: str, meta: str = "") -> int:
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        # Ensure user exists and update balance
        c.execute("""INSERT INTO users(discord_id, balance, joined_at) VALUES(?,?,?)
                     ON CONFLICT(discord_id) DO UPDATE SET balance = balance + ?""", 
                  (str(uid), delta, iso(now_local()), delta))
        # Record transaction
        c.execute("INSERT INTO tx(discord_id, kind, amount, meta, ts) VALUES(?,?,?,?,?)",
                  (str(uid), kind, delta, meta, iso(now_local())))
        c.execute("SELECT balance FROM users WHERE discord_id=?", (str(uid),))
        return c.fetchone()[0]

def ensure_user(uid: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO users(discord_id, balance, joined_at) VALUES(?,0,?)", 
                     (str(uid), iso(now_local())))

def now_local(): return datetime.now(TZ)
def iso(dt: datetime): return dt.astimezone(TZ).isoformat()

def set_state(key: str, val: str | None):
    with sqlite3.connect(DB_PATH) as conn:
        if val is None: conn.execute("DELETE FROM state WHERE key=?", (key,))
        else: conn.execute("INSERT INTO state(key,val) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET val=excluded.val", (key, val))

def get_state(key: str) -> str | None:
    with sqlite3.connect(DB_PATH) as conn:
        r = conn.execute("SELECT val FROM state WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

def get_lotto_config() -> dict:
    raw = get_state("lotto_config")
    if raw:
        return json.loads(raw)
    return {"day": "Saturday", "time": "20:00", "prize": 10, "winners": 1, "shop_name": "EliHaus Shop", "shop_url": ""}

def set_lotto_config(cfg: dict):
    set_state("lotto_config", json.dumps(cfg))

# ---------------- UI Components ----------------

class LottoClaimModal(discord.ui.Modal, title="Claim Prize as WL Gifts"):
    imvu = discord.ui.TextInput(label="IMVU Username", placeholder="e.g. YaEli", required=True)
    note = discord.ui.TextInput(label="Extra Notes", style=discord.TextStyle.paragraph, required=False, max_length=100)

    def __init__(self, prize_count):
        super().__init__()
        self.prize_count = prize_count

    async def on_submit(self, itx: discord.Interaction):
        embed = discord.Embed(title="🎁 Lotto Prize Claim (WL)", color=discord.Color.blue())
        embed.description = f"**Winner:** {itx.user.mention}\n**IMVU:** {self.imvu.value}\n**Jackpot:** {self.prize_count} gifts"
        await itx.channel.send(content=f"🔔 **Staff Alert:** {itx.user.mention} requested **Gifts**!", embed=embed)
        await itx.response.send_message("✅ Your details have been sent to staff for fulfillment!", ephemeral=True)

class LottoClaimView(discord.ui.View):
    def __init__(self, winner_id, prize_count):
        super().__init__(timeout=None)
        self.winner_id = int(winner_id); self.prize_count = prize_count

    async def interaction_check(self, itx: discord.Interaction):
        if itx.user.id != self.winner_id:
            await itx.response.send_message("Not your prize, babe.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Claim WL Gifts", style=discord.ButtonStyle.primary, emoji="🎁")
    async def claim_wl(self, itx: discord.Interaction, button: discord.ui.Button):
        await itx.response.send_modal(LottoClaimModal(self.prize_count))

class RouletteBetView(discord.ui.View):
    def __init__(self, rid: str): super().__init__(timeout=None); self.rid = rid
    @discord.ui.button(label="RED", style=discord.ButtonStyle.danger, emoji="🎯")
    async def bet_red(self, itx, _): await itx.response.send_modal(BetModal(self.rid, "RED"))
    @discord.ui.button(label="BLACK", style=discord.ButtonStyle.primary, emoji="🎯")
    async def bet_black(self, itx, _): await itx.response.send_modal(BetModal(self.rid, "BLACK"))
    @discord.ui.button(label="GREEN", style=discord.ButtonStyle.success, emoji="🎯")
    async def bet_green(self, itx, _): await itx.response.send_modal(BetModal(self.rid, "GREEN"))
    @discord.ui.button(label="NUMBER", style=discord.ButtonStyle.secondary, emoji="🔢")
    async def bet_num(self, itx, _): await itx.response.send_modal(BetModal(self.rid, "NUMBER"))

class BetModal(discord.ui.Modal):
    def __init__(self, rid, choice):
        super().__init__(title=f"Bet: {choice}")
        self.rid = rid; self.choice = choice
        self.stake_input = discord.ui.TextInput(label="Stake Amount", placeholder=f"Min {ROUL_MIN_BET}", required=True)
        self.add_item(self.stake_input)
        if choice == "NUMBER":
            self.num_input = discord.ui.TextInput(label="Number (0-36)", placeholder="17", max_length=2)
            self.add_item(self.num_input)

    async def on_submit(self, itx: discord.Interaction):
        try:
            amt = int(self.stake_input.value)
            target = self.choice
            if self.choice == "NUMBER":
                n = int(self.num_input.value)
                if not (0 <= n <= 36): raise ValueError
                target = f"NUM:{n}"
        except: return await itx.response.send_message("Invalid inputs.", ephemeral=True)

        uid = str(itx.user.id)
        if get_balance(uid) < amt: return await itx.response.send_message("You're too broke for that bet!", ephemeral=True)
        if amt < ROUL_MIN_BET: return await itx.response.send_message(f"Minimum bet is {ROUL_MIN_BET:,}!", ephemeral=True)

        change_balance(uid, -amt, "bet", f"roulette:{self.rid}")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO bets(rid, channel_id, discord_id, choice, stake, ts) VALUES(?,?,?,?,?,?)",
                         (self.rid, str(itx.channel_id), uid, target, amt, iso(now_local())))
        await itx.response.send_message(f"✅ Bet of **{amt:,}** on **{target}** placed!", ephemeral=True)

# ---------------- Commands ----------------

@bot.tree.command(name="eh_join", description="Join EliHaus and get starter coins")
async def eh_join(itx: discord.Interaction):
    uid = str(itx.user.id)
    with sqlite3.connect(DB_PATH) as conn:
        res = conn.execute("SELECT 1 FROM tx WHERE discord_id=? AND kind='starter' LIMIT 1", (uid,)).fetchone()
    
    if res:
        return await itx.response.send_message(f"You've already joined! Balance: **{get_balance(uid):,}**", ephemeral=True)
    
    new_bal = change_balance(uid, STARTER_AMOUNT, "starter", "Initial join bonus")
    await itx.response.send_message(
        f"Welcome to EliHaus! 🥂 You've received **{STARTER_AMOUNT:,}** starter coins.\nTotal balance: **{new_bal:,}**", 
        ephemeral=True
    )

@bot.tree.command(name="eh_balance", description="Check balance")
async def eh_balance(itx: discord.Interaction, user: discord.Member = None):
    target = user or itx.user
    bal = get_balance(str(target.id))
    await itx.response.send_message(f"💰 {target.display_name}: **{bal:,}** coins.", ephemeral=(user is None))

@bot.tree.command(name="eh_daily", description="Claim your daily coins")
async def eh_daily(itx: discord.Interaction):
    uid = str(itx.user.id)
    ensure_user(uid)
    with sqlite3.connect(DB_PATH) as conn:
        r = conn.execute("SELECT last_daily FROM users WHERE discord_id=?", (uid,)).fetchone()
        last = datetime.fromisoformat(r[0]) if r and r[0] else None
        if last and (now_local() - last) < timedelta(hours=24):
            rem = timedelta(hours=24) - (now_local() - last)
            h, m = divmod(int(rem.total_seconds()), 3600); m //= 60
            return await itx.response.send_message(f"Try again in **{h}h {m}m**.", ephemeral=True)
        
        new_bal = change_balance(uid, DAILY_AMOUNT, "claim", "daily")
        conn.execute("UPDATE users SET last_daily=? WHERE discord_id=?", (iso(now_local()), uid))
    await itx.response.send_message(f"💰 Daily coins claimed! **+{DAILY_AMOUNT:,}** balance.", ephemeral=True)

@bot.tree.command(name="eh_leaderboard", description="Show top players")
async def eh_leaderboard(itx: discord.Interaction, mode: str = "balance", public: bool = True):
    await itx.response.defer(ephemeral=not public)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            if mode == "balance":
                rows = conn.execute("SELECT discord_id, balance FROM users ORDER BY balance DESC LIMIT 10").fetchall()
                title = "🏆 EliHaus Leaderboard — Balance"
            else:
                rows = conn.execute("SELECT discord_id, SUM(amount) FROM tx WHERE kind IN ('bet','payout') GROUP BY discord_id ORDER BY SUM(amount) DESC LIMIT 10").fetchall()
                title = "🎰 Leaderboard — Roulette Net"

        embed = discord.Embed(title=title, color=discord.Color.gold())
        lines = [f"{i+1}. <@{r[0]}> — **{r[1]:,}**" for i, r in enumerate(rows)]
        embed.description = "\n".join(lines) if lines else "No data yet."
        await itx.followup.send(embed=embed)
    except Exception as e:
        await itx.followup.send(f"⚠️ Leaderboard error: `{e}`")

@bot.tree.command(name="eh_openround", description="(Admin) Open roulette betting")
async def eh_openround(itx: discord.Interaction, seconds: int = 120):
    if not user_is_admin(itx): return await itx.response.send_message("No permission.", ephemeral=True)
    rid = f"R-{itx.channel_id}-{int(time.time())}"
    expires = now_local() + timedelta(seconds=seconds)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO rounds(rid, channel_id, status, opened_by, expires_at) VALUES(?,?,?,?,?)", (rid, str(itx.channel_id), "OPEN", str(itx.user.id), iso(expires)))
    set_state(f"active_round:{itx.channel_id}", rid)
    embed = discord.Embed(title="🎰 Roulette Open", description=f"Place your bets! Ending in <t:{int(expires.timestamp())}:R>", color=discord.Color.gold())
    await itx.channel.send(embed=embed, view=RouletteBetView(rid))
    await itx.response.send_message("Round opened.", ephemeral=True)

@bot.tree.command(name="eh_resolve", description="(Admin) Spin the wheel!")
async def eh_resolve(itx: discord.Interaction):
    if not user_is_admin(itx): return await itx.response.send_message("No permission.", ephemeral=True)
    rid = get_state(f"active_round:{itx.channel_id}")
    if not rid: return await itx.response.send_message("No active round.", ephemeral=True)
    
    await itx.response.defer(ephemeral=True)
    roll = random.randint(0, 36)
    color = "GREEN" if roll == 0 else ("RED" if roll in RED_NUMBERS else "BLACK")
    color_emoji = "🟩" if color == "GREEN" else ("🟥" if color == "RED" else "⬛")
    
    winners = []
    with sqlite3.connect(DB_PATH) as conn:
        bets = conn.execute("SELECT discord_id, choice, stake FROM bets WHERE rid=?", (rid,)).fetchall()
        for uid, ch, st in bets:
            payout = 0
            if ch == color: payout = int(st * (PAYOUT_GREEN if color == "GREEN" else PAYOUT_RED_BLACK))
            elif ch == f"NUM:{roll}": payout = int(st * PAYOUT_NUMBER)
            
            if payout > 0:
                change_balance(uid, payout, "payout", f"roulette win:{rid}")
                winners.append(f"<@{uid}>: +{payout:,}")

    set_state(f"active_round:{itx.channel_id}", None)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE rounds SET status='RESOLVED', outcome=? WHERE rid=?", (f"{color}:{roll}", rid))
    
    embed = discord.Embed(title=f"🎰 Result: {color_emoji} {color} #{roll}", color=discord.Color.gold())
    embed.description = "**Winners:**\n" + ("\n".join(winners) if winners else "Everyone lost! 💀")
    await itx.channel.send(embed=embed)
    await itx.followup.send("Resolved.")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}"); await bot.tree.sync()

bot.run(DISCORD_TOKEN)
