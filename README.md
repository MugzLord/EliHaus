# EliHaus – Coins, Admin Roulette, Weekly Lotto & Prize Queue

EliHaus is a Discord bot for IMVU-style giveaways and light casino play.  
It powers:
- 🪙 Coins (`!joinhaus`, `!daily`, `!weekly`, `!balance`)
- 🎯 Admin-led Roulette (`!openround`, `!bet`, `!resolve`, `!cancelround`)
- 🎟️ Weekly Lotto (1 winner gets **10 WL gifts** from Shop X)
- 🎁 Prize claims via button + modal, with an admin fulfilment queue

> **No real money.** Coins are virtual and only used for in-server fun.

---

## ✨ Features

- **Starter pack**: `!joinhaus` → 5,000 coins (once)
- **Daily/Weekly**: `!daily` = 1,800, `!weekly` = 6,000
- **Roulette (admin-only)**: open → players bet → admin resolves
- **Weekly Lotto**: 10,000 coins per ticket; single grand winner gets 10 WL gifts (Shop X)
- **Prize Queue**: Winner presses “Claim WL Gifts”, submits IMVU; admins fulfil with `!fulfil_next` / `!fulfil_done`

---

## 🚀 Quick Start

### 1) Requirements
- Python **3.10+**
- `discord.py` **2.x**

```bash
python -m venv .venv
. .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -U discord.py
