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

# Roulette Config
ROUND_SECONDS_DEFAULT = 120
PAYOUT_RED_BLACK = 2.0
PAYOUT_GREEN = 14.0
PAYOUT_NUMBER = 36.0
ROUL_MIN_BET = 100
ROUL_MAX_BET = 999_999_999_999_999 # Effectively no limit

# Roles & Channels
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))

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
    """Helper to check for Administrator permission or specific Admin role."""
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
        c.execute("""INSERT INTO users(discord_id, balance, joined_at) VALUES(?,?,?)
                     ON CONFLICT(discord_id) DO UPDATE SET balance = balance + ?""", 
                  (str(uid), delta, iso(now_local()), delta))
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
    return {
        "day": "Saturday", 
        "time": "20:00", 
        "prize": 10, 
        "winners": 1, 
        "shop_name": "EliHaus Shop", 
        "shop_url": ""
    }

def set_lotto_config(cfg: dict):
    set_state("lotto_config", json.dumps(cfg))

# ---------------- Claim System ----------------

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

# ---------------- PVP Dice Duel View ----------------

class DiceDuelView(discord.ui.View):
    def __init__(self, challenger, opponent, stake):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.stake = stake

    @discord.ui.button(label="Accept Duel", style=discord.ButtonStyle.success, emoji="⚔️")
    async def accept(self, itx: discord.Interaction, button: discord.ui.Button):
        if itx.user.id != self.opponent.id:
            return await itx.response.send_message("You weren't challenged, stay back!", ephemeral=True)
        
        if get_balance(str(self.challenger.id)) < self.stake or get_balance(str(self.opponent.id)) < self.stake:
            return await itx.response.send_message("One of you is too broke to duel now.", ephemeral=True)

        self.stop()
        change_balance(str(self.challenger.id), -self.stake, "duel_stake", f"vs {self.opponent.id}")
        change_balance(str(self.opponent.id), -self.stake, "duel_stake", f"vs {self.challenger.id}")

        c_roll = random.randint(1, 100)
        o_roll = random.randint(1, 100)
        
        embed = discord.Embed(title="⚔️ Dice Duel Results", color=discord.Color.gold())
        embed.add_field(name=self.challenger.display_name, value=f"🎲 **{c_roll}**", inline=True)
        embed.add_field(name=self.opponent.display_name, value=f"🎲 **{o_roll}**", inline=True)

        if c_roll > o_roll:
            winner = self.challenger
            pot = int(self.stake * 2)
            change_balance(str(winner.id), pot, "duel_win")
            embed.description = f"🏆 **{winner.mention} WINS THE POT OF {pot:,}!**\nBetter luck next time, {self.opponent.mention}."
        elif o_roll > c_roll:
            winner = self.opponent
            pot = int(self.stake * 2)
            change_balance(str(winner.id), pot, "duel_win")
            embed.description = f"🏆 **{winner.mention} WINS THE POT OF {pot:,}!**\n{self.challenger.mention} just lost it all."
        else:
            change_balance(str(self.challenger.id), self.stake, "duel_refund")
            change_balance(str(self.opponent.id), self.stake, "duel_refund")
            embed.description = "🤝 **It's a tie! Stakes refunded.**"

        await itx.response.edit_message(content=None, embed=embed, view=None)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, itx: discord.Interaction, button: discord.ui.Button):
        if itx.user.id != self.opponent.id: return
        await itx.response.edit_message(content=f"❌ {self.opponent.mention} was too scared to duel.", embed=None, view=None)

# ---------------- Roulette Views ----------------

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
        
        if amt < ROUL_MIN_BET:
            return await itx.response.send_message(f"Minimum bet is {ROUL_MIN_BET:,} coins!", ephemeral=True)

        change_balance(uid, -amt, "bet", f"roulette:{self.rid}")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO bets(rid, channel_id, discord_id, choice, stake, ts) VALUES(?,?,?,?,?,?)",
                         (self.rid, str(itx.channel_id), uid, target, amt, iso(now_local())))
        await itx.response.send_message(f"✅ Bet of **{amt:,}** on **{target}** placed! Good luck, high roller.", ephemeral=True)

class LottoConfigModal(discord.ui.Modal, title="Configure Weekly Lotto"):
    day = discord.ui.TextInput(label="Draw Day (Mon-Sun)", placeholder="Saturday", max_length=10)
    time = discord.ui.TextInput(label="Draw Time (HH:MM)", placeholder="20:00", max_length=5)
    prize = discord.ui.TextInput(label="Jackpot Amount (WL Gifts)", placeholder="10", max_length=5)
    winners = discord.ui.TextInput(label="Number of Winners", placeholder="1", max_length=2)
    shop = discord.ui.TextInput(label="Shop Name", placeholder="EliHaus Shop", max_length=50)

    def __init__(self, current_cfg):
        super().__init__()
        self.day.default = current_cfg.get("day", "Saturday")
        self.time.default = current_cfg.get("time", "20:00")
        self.prize.default = str(current_cfg.get("prize", 10))
        self.winners.default = str(current_cfg.get("winners", 1))
        self.shop.default = current_cfg.get("shop_name", "EliHaus Shop")

    async def on_submit(self, itx: discord.Interaction):
        try:
            p = int(self.prize.value)
            w = int(self.winners.value)
            cfg = {
                "day": self.day.value, 
                "time": self.time.value, 
                "prize": p, 
                "winners": max(1, w), 
                "shop_name": self.shop.value, 
                "shop_url": ""
            }
            set_lotto_config(cfg)
            await itx.response.send_message("✅ Lotto updated!", ephemeral=True)
        except:
            await itx.response.send_message("❌ Invalid numbers.", ephemeral=True)

# ---------------- Commands (Public) ----------------

@bot.tree.command(name="eh_join", description="Join EliHaus and get starter coins")
async def eh_join(itx: discord.Interaction):
    uid = str(itx.user.id)
    ensure_user(uid)
    await itx.response.send_message(f"Welcome! Total: **{get_balance(uid):,}**", ephemeral=True)

@bot.tree.command(name="eh_balance", description="Check balance")
async def eh_balance(itx: discord.Interaction, user: discord.Member = None):
    target = user or itx.user
    bal = get_balance(str(target.id))
    await itx.response.send_message(f"💰 {target.display_name}: **{bal:,}** coins.", ephemeral=(user is None))

@bot.tree.command(name="eh_send", description="Send coins to another player")
async def eh_send(itx: discord.Interaction, user: discord.Member, amount: int):
    if amount <= 0: return await itx.response.send_message("Stop being stingy.", ephemeral=True)
    if user.id == itx.user.id: return await itx.response.send_message("You can't send money to yourself, silly.", ephemeral=True)
    uid = str(itx.user.id)
    if get_balance(uid) < amount: return await itx.response.send_message("You don't have enough coins!", ephemeral=True)
    change_balance(uid, -amount, "transfer", f"to {user.id}")
    change_balance(str(user.id), amount, "transfer", f"from {itx.user.id}")
    await itx.response.send_message(f"💸 {itx.user.mention} sent **{amount:,}** coins to {user.mention}!")

@bot.tree.command(name="eh_coinflip", description="Double or Nothing (High Risk!)")
async def eh_coinflip(itx: discord.Interaction, amount: int, choice: str):
    if choice.lower() not in ["heads", "tails"]: return await itx.response.send_message("Choose heads or tails.", ephemeral=True)
    uid = str(itx.user.id)
    if get_balance(uid) < amount: return await itx.response.send_message("You're too broke for this bet.", ephemeral=True)
    await itx.response.send_message(f"🪙 Flipping for **{amount:,}**...")
    await asyncio.sleep(2)
    outcome = random.choice(["heads", "tails"])
    if choice.lower() == outcome:
        new_bal = change_balance(uid, amount, "coinflip_win")
        await itx.edit_original_response(content=f"🎉 **It's {outcome.upper()}!** You doubled your money. New balance: **{new_bal:,}**")
    else:
        new_bal = change_balance(uid, -amount, "coinflip_lose")
        await itx.edit_original_response(content=f"💀 **It's {outcome.upper()}!** You lost it all. New balance: **{new_bal:,}**")

@bot.tree.command(name="eh_dice_duel", description="Challenge someone for their coins! (Winner takes all)")
async def eh_dice_duel(itx: discord.Interaction, opponent: discord.Member, stake: int):
    if stake <= 0 or opponent.id == itx.user.id: return
    if get_balance(str(itx.user.id)) < stake: return await itx.response.send_message("You don't have enough for this duel.", ephemeral=True)
    view = DiceDuelView(itx.user, opponent, stake)
    await itx.response.send_message(f"⚔️ {itx.user.mention} challenged {opponent.mention} to a **{stake:,} coin duel**! Higher roll wins the pot.", view=view)

@bot.tree.command(name="eh_buyticket", description="Buy Lotto tickets")
async def eh_buyticket(itx: discord.Interaction, count: int = 1):
    uid = str(itx.user.id); cost = TICKET_COST * count
    if get_balance(uid) < cost: return await itx.response.send_message("Insufficient coins.", ephemeral=True)
    change_balance(uid, -cost, "buy_ticket", f"lotto x{count}")
    wk = now_local().strftime("%Y-%W")
    with sqlite3.connect(DB_PATH) as conn:
        for _ in range(count): conn.execute("INSERT INTO tickets(week_id, discord_id, ts) VALUES(?,?,?)", (wk, uid, iso(now_local())))
    await itx.response.send_message(f"🎟️ Bought {count} tickets!", ephemeral=True)

# ---------------- Commands (Admin Only) ----------------

@bot.tree.command(name="eh_lotto_config", description="(Admin) Configure lotto draw")
async def eh_lotto_config(itx: discord.Interaction):
    if not user_is_admin(itx): return await itx.response.send_message("No permission.", ephemeral=True)
    await itx.response.send_modal(LottoConfigModal(get_lotto_config()))

@bot.tree.command(name="eh_drawlotto", description="(Admin) Draw winners with suspense!")
async def eh_drawlotto(itx: discord.Interaction):
    if not user_is_admin(itx): return await itx.response.send_message("No permission.", ephemeral=True)
    wk = now_local().strftime("%Y-%W")
    with sqlite3.connect(DB_PATH) as conn: rows = conn.execute("SELECT discord_id FROM tickets WHERE week_id=?", (wk,)).fetchall()
    if not rows: return await itx.response.send_message("No entries.", ephemeral=True)
    await itx.response.send_message("🔥 **STARTING DRAW...** 🥁"); msg = await itx.original_response()
    for f in ["🔎 Searching database...", "🌪️ Shuffling tickets...", "✨ WINNERS FOUND!"]: 
        await asyncio.sleep(1.5); await msg.edit(content=f)
    cfg = get_lotto_config(); unique = list(set([r[0] for r in rows])); random.shuffle(unique)
    winners = unique[:cfg["winners"]]
    embed = discord.Embed(title="🎉 LOTTO WINNERS!", color=discord.Color.gold())
    embed.description = "\n".join([f"🏆 <@{w}>" for w in winners]) + f"\n\nEach wins **{cfg['prize']} Gifts**!"
    await itx.channel.send(embed=embed)
    for w in winners: await itx.channel.send(f"Hey <@{w}>! Click below to claim your prize:", view=LottoClaimView(w, cfg["prize"]))

@bot.tree.command(name="eh_openround", description="(Admin) Open roulette betting")
async def eh_openround(itx: discord.Interaction, seconds: int = 120):
    if not user_is_admin(itx): return await itx.response.send_message("Only admins can start Roulette, babes.", ephemeral=True)
    rid = f"R-{itx.channel_id}-{int(time.time())}"
    expires = now_local() + timedelta(seconds=seconds)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO rounds(rid, channel_id, status, opened_by, expires_at) VALUES(?,?,?,?,?)", (rid, str(itx.channel_id), "OPEN", str(itx.user.id), iso(expires)))
    set_state(f"active_round:{itx.channel_id}", rid)
    embed = discord.Embed(title="🎰 Roulette Open", description=f"Ending in <t:{int(expires.timestamp())}:R>", color=discord.Color.gold())
    await itx.channel.send(embed=embed, view=RouletteBetView(rid))
    await itx.response.send_message("Round opened.", ephemeral=True)

@bot.tree.command(name="eh_resolve", description="(Admin) Spin the wheel!")
async def eh_resolve(itx: discord.Interaction):
    if not user_is_admin(itx): return await itx.response.send_message("Only admins can spin the wheel!", ephemeral=True)
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
    
    embed = discord.Embed(
        title=f"🎰 Roulette Result: {color_emoji} {color} #{roll}", 
        color=discord.Color.green() if color=="GREEN" else (discord.Color.red() if color=="RED" else discord.Color.dark_grey())
    )
    embed.description = "**Winners:**\n" + ("\n".join(winners) if winners else "Everyone lost! 💀")
    await itx.channel.send(embed=embed)
    await itx.followup.send("Resolved.")

@bot.tree.command(name="eh_leaderboard", description="Show top players")
async def eh_leaderboard(itx: discord.Interaction, mode: str = "balance", public: bool = True):
    await itx.response.defer(ephemeral=not public)
    with sqlite3.connect(DB_PATH) as conn:
        if mode == "balance": rows = conn.execute("SELECT discord_id, balance FROM users ORDER BY balance DESC LIMIT 10").fetchall()
        else: rows = conn.execute("SELECT discord_id, SUM(amount) FROM tx WHERE kind IN ('bet','payout') GROUP BY discord_id ORDER BY SUM(amount) DESC LIMIT 10").fetchall()
    embed = discord.Embed(title=f"🏆 Leaderboard - {mode.title()}", color=discord.Color.gold())
    embed.description = "\n".join([f"{i+1}. <@{r[0]}> — **{r[1]:,}**" for i, r in enumerate(rows)]) or "No data."
    await itx.followup.send(embed=embed)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}"); await bot.tree.sync()

bot.run(DISCORD_TOKEN)
