# elihause_bot.py — EliHaus (coins + admin roulette + weekly lotto + prize queue)
# Requires: pip install -U discord.py
import os, sqlite3, random, math, json
from datetime import datetime, timedelta, timezone
import asyncio
import discord
from discord.ext import commands
# ---- Config ----
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN")

TIMEZONE_NAME = os.getenv("TIMEZONE", "Asia/Qatar")
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
SHOP_NAME = "Shop X"            # display name in winner message

# Roulette knobs (admin-led, manual resolve)
ROUND_SECONDS_DEFAULT = 30
PAYOUT_RED_BLACK = 2.0
PAYOUT_GREEN = 14.0             # enable green flavour
MAX_STAKE = 50_000
ONE_BET_PER_ROUND = True

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
class ClaimView(discord.ui.View):
    def __init__(self, prize_id: int, timeout: int = 600):
        super().__init__(timeout=timeout)
        self.prize_id = prize_id

    @discord.ui.button(label="Claim WL Gifts", style=discord.ButtonStyle.primary)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self._winner_id_from_prize(self.prize_id):
            return await interaction.response.send_message("Only the winner can claim this prize.", ephemeral=True)
        await interaction.response.send_modal(ClaimModal(self.prize_id))

    def _winner_id_from_prize(self, pid: int) -> str:
        with db() as conn:
            c = conn.cursor()
            c.execute("SELECT winner_id FROM prizes WHERE id=?", (pid,))
            row = c.fetchone()
            return row[0] if row else ""

class ClaimModal(discord.ui.Modal, title="Claim WL Gifts"):
    imvu_name = discord.ui.TextInput(label="IMVU Username", required=True, max_length=60)
    imvu_profile = discord.ui.TextInput(label="IMVU Profile Link (optional)", required=False, max_length=200, placeholder="https://www.imvu.com/…")
    note = discord.ui.TextInput(label="Notes (optional)", required=False, style=discord.TextStyle.paragraph, max_length=200)

    def __init__(self, prize_id: int):
        super().__init__()
        self.prize_id = prize_id

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        with db() as conn:
            c = conn.cursor()
            c.execute("""INSERT INTO prize_queue(prize_id,winner_id,imvu_name,imvu_profile,note,status,created_ts,updated_ts)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (self.prize_id, uid, str(self.imvu_name), str(self.imvu_profile), str(self.note or ""),
                       "ready", iso(now_local()), iso(now_local())))
            c.execute("UPDATE prizes SET status='claimed', updated_ts=? WHERE id=?", (iso(now_local()), self.prize_id))
        await interaction.response.send_message("Thanks! Your WL gift claim is queued. An admin will fulfil it shortly. ✅", ephemeral=True)

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

def get_open_round(channel_id: int) -> tuple[str, datetime] | None:
    rid = get_state(round_key(channel_id))
    if not rid:
        return None
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT expires_at FROM rounds WHERE rid=? AND status='OPEN'", (rid,))
        r = c.fetchone()
        if not r: 
            return None
        exp = datetime.fromisoformat(r[0]).astimezone(TZ)
        return rid, exp

@commands.has_permissions(manage_guild=True)
@bot.command(name="openround")
async def openround(ctx: commands.Context, seconds: int = ROUND_SECONDS_DEFAULT):
    if get_open_round(ctx.channel.id):
        return await ctx.reply("There’s already an open round in this channel.")
    rid, exp = open_round(ctx.channel.id, seconds, str(ctx.author.id))
    embed = discord.Embed(title=f"🎯 Roulette — Round {rid}", description=f"Bet window: **{seconds}s**\nUse `!bet <amount> <red|black|green>`", color=discord.Color.gold())
    embed.add_field(name="Pool", value="0", inline=True)
    embed.add_field(name="Time", value=f"closes ~ {exp.strftime('%H:%M:%S')}", inline=True)
    msg = await ctx.reply(embed=embed)
    with db() as conn:
        conn.execute("UPDATE rounds SET message_id=? WHERE rid=?", (str(msg.id), rid))

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
    # close key
    set_state(round_key(ctx.channel.id), None)

    # outcome - emulate 18 red / 18 black / 1 green probabilities
    wheel = ["red"]*18 + ["black"]*18 + ["green"]
    seed = f"{rid}-{int(now_local().timestamp())}-{random.randint(1, 1_000_000)}"
    random.seed(seed)
    outcome = random.choice(wheel)

    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT discord_id, choice, stake FROM bets WHERE rid=?", (rid,))
        rows = c.fetchall()
        total_pool = sum(stake for _,_,stake in rows)
        winners = []
        for uid, choice, stake in rows:
            win = 0
            if choice == outcome:
                if outcome in ("red","black"):
                    win = int(stake * PAYOUT_RED_BLACK)
                elif outcome == "green":
                    win = int(stake * PAYOUT_GREEN)
            if win > 0:
                c.execute("UPDATE users SET balance=balance+? WHERE discord_id=?", (win, uid))
                c.execute("INSERT INTO tx(discord_id,kind,amount,meta,ts) VALUES(?,?,?,?,?)",
                          (uid, "payout", win, f"roulette:{rid}|{outcome}", iso(now_local())))
                winners.append((uid, win))
        c.execute("UPDATE rounds SET status='RESOLVED', outcome=?, seed=?, resolved_at=? WHERE rid=?",
                  (outcome, seed, iso(now_local()), rid))
        # fetch message id for edit
        c.execute("SELECT message_id FROM rounds WHERE rid=?", (rid,))
        r = c.fetchone()
        msg_id = int(r[0]) if (r and r[0]) else None

    top_mentions = []
    for uid, _ in winners[:5]:
        m = ctx.guild.get_member(int(uid))
        top_mentions.append(m.mention if m else f"<@{uid}>")
    text = (f"🎯 **Round {rid} → {outcome.upper()}**\n"
            f"Total bets: **{len(rows)}** • Pool: **{total_pool}**\n"
            f"Winners (top): {', '.join(top_mentions) if top_mentions else 'None'}\n"
            f"Seed: `{seed}`")
    if msg_id:
        try:
            msg = await ctx.channel.fetch_message(msg_id)
            embed = msg.embeds[0] if msg.embeds else discord.Embed(color=discord.Color.gold())
            embed.title = f"🎯 Roulette — Round {rid}"
            embed.description = f"**RESULT:** {outcome.upper()}"
            embed.set_footer(text=f"Seed: {seed}")
            await msg.edit(content=None, embed=embed)
            await ctx.send(text)
        except Exception:
            await ctx.reply(text)
    else:
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
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM tickets WHERE week_id=?", (wk,))
        total = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM tickets WHERE week_id=? AND discord_id=?", (wk, uid))
        mine = c.fetchone()[0]
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
    seed = f"LOTTO-{wk}-{int(now_local().timestamp())}-{random.randint(1, 1_000_000)}"
    random.seed(seed)
    winner_ticket = random.choice(all_tix)
    winner_id = winner_ticket[1]
    # record draw + create prize
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
        description=f"{mention} wins **{LOTTO_WL_COUNT}** wishlist gifts from **{SHOP_NAME}**.\nSeed: `{seed}`",
        color=discord.Color.gold()
    )
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

# ---- Run ----
@bot.event
async def on_ready():
    print(f"[EliHaus] Logged in as {bot.user} | TZ={TIMEZONE_NAME}")

bot.run(TOKEN)
