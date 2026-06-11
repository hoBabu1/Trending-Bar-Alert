# 🕯 Crypto Candle Streak Telegram Alert Bot

A lightweight Python bot that watches the market and sends you a **Telegram alert**
whenever a coin prints **5 or more consecutive green or red closed candles** on a
given timeframe. It keeps alerting as the streak grows (5th → 6th → 7th candle…)
and stops once the streak breaks. After every scan it also sends a compact
**heartbeat summary** so you always know it's alive.

- **Coins:** BTC, SOL, XRP, UNI, AVAX
- **Timeframes:** 30m, 1h, 4h
- **Data source:** Kraken public REST API (no API key required)
- **Dependencies:** just `requests`

> ℹ️ **Why Kraken?** Binance (HTTP 451) and Bybit (HTTP 403) geo-block requests
> from US cloud / data-center IPs, which is where free hosts like GitHub Actions
> run. Kraken is reachable from those hosts and supports the 30m / 1h / 4h
> timeframes this bot uses.

---

## 1. Get your Telegram Bot Token

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the prompts (choose a name and a username ending in `bot`).
3. BotFather replies with a **token** that looks like:
   ```
   123456789:AAH...your-token...xyz
   ```
   This is your `TELEGRAM_BOT_TOKEN`.

## 2. Get your Telegram Chat ID

1. Search for **@userinfobot** in Telegram.
2. Start it / send any message.
3. It replies with your numeric **Id** — that is your `TELEGRAM_CHAT_ID`.

> 💡 Make sure you send `/start` to **your own bot** at least once, otherwise
> Telegram won't let the bot message you.

---

## 3. Run locally (optional)

```bash
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="123456789:AAH...xyz"
export TELEGRAM_CHAT_ID="987654321"

python candle_alert_bot.py
```

On startup you'll get:

> 🤖 Candle Alert Bot is live! Monitoring BTC, SOL, XRP, UNI, AVAX on 30m / 1h / 4h.

---

## 4. Deploy on GitHub Actions (free, recommended)

The bot ships with a scheduled workflow at
[.github/workflows/candle-alert.yml](.github/workflows/candle-alert.yml) that
runs a single scan every 30 minutes — no server and no monthly cost. It runs the
script in `--once` mode and persists `alert_state.json` back to the repo so you
only get alerted when a streak is **new or grows** (no duplicate spam across runs).

1. Push this folder to a GitHub repository.
2. In your repo, go to **Settings → Secrets and variables → Actions**.
3. Under **Repository secrets** (not *Environment* secrets), click
   **New repository secret** and add both:

   | Name                 | Value                |
   | -------------------- | -------------------- |
   | `TELEGRAM_BOT_TOKEN` | your BotFather token |
   | `TELEGRAM_CHAT_ID`   | your numeric chat id |

4. Go to the **Actions** tab → **Candle Streak Alert** → **Run workflow** to test
   it immediately (otherwise it runs automatically on the 30-minute schedule).

> ⏱ **Schedule note:** On GitHub Actions the scan frequency is controlled by the
> `cron` line in the workflow file (`*/30 * * * *`), **not** the `CHECK_INTERVAL`
> variable. GitHub may delay scheduled runs by a few minutes under load, and
> auto-pauses schedules after 60 days of repo inactivity (a single run re-enables
> them).

---

## 4b. Alternative: deploy on Render.com (always-on, paid)

Prefer a continuously running process instead of a cron? Deploy the **Background
Worker** defined in `render.yaml`. In this mode the script runs forever
(`python candle_alert_bot.py`, no `--once`) and uses `CHECK_INTERVAL`.

1. [Render.com](https://render.com) → **New** → **Background Worker** → connect your repo.
2. Render auto-detects `render.yaml` (runtime, build/start commands, Starter plan).
3. In the service dashboard → **Environment**, add `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_CHAT_ID`. Save — Render redeploys automatically.

> ⚠️ Render's free plan does **not** support always-on workers (they spin down).
> Keeping it running 24/7 needs the **Starter plan (~$7/month)**.

---

## 5. What you'll receive in Telegram

**A) Streak alert** — only when a coin hits a 5+ streak or it grows:

```
🟢 Candle Streak Alert!

📌 Coin      : BTC
⏱ Timeframe : 1h
🕯 Streak    : 5 consecutive green candles
💰 Last Close: $67,432.10
🕒 Time (UTC): 2024-01-15 14:00
```

**B) Heartbeat summary** — after every scan, so you know the bot is alive:

```
🔍 Scan complete — 2024-01-15 14:00 UTC

BTC: 30m 1🔴 | 1h 1🟢 | 4h 1🟢
SOL: 30m 1🔴 | 1h 1🟢 | 4h 1🟢
XRP: 30m 1🔴 | 1h 1🟢 | 4h 1🟢
UNI: 30m 1🔴 | 1h 5🟢⭐ | 4h 1🟢
AVAX: 30m 1🔴 | 1h 1🟢 | 4h 1🟢
```

Each entry is `timeframe streak-length color`. ⭐ marks a streak that has hit the
alert threshold; ⚪ means a doji (no clear color). Turn the heartbeat off by
setting `SEND_SCAN_SUMMARY = False` at the top of the script.

---

## 6. Customize

All settings live at the top of [candle_alert_bot.py](candle_alert_bot.py):

```python
SYMBOLS = ["BTCUSDT", "SOLUSDT", "XRPUSDT", "UNIUSDT", "AVAXUSDT"]
TIMEFRAMES = ["30m", "1h", "4h"]
MIN_STREAK = 5             # alert threshold
CHECK_INTERVAL = 30 * 60   # scan frequency in seconds (Render/local loop only)
SEND_SCAN_SUMMARY = True   # send the heartbeat summary after every scan
```

- **Add/remove coins:** edit `SYMBOLS`, then add the Kraken pair mapping in
  `KRAKEN_PAIRS` (e.g. `"DOGEUSDT": "DOGEUSD"`). Kraken uses `XBT` for BTC.
- **Change timeframes:** edit `TIMEFRAMES` using values present in
  `KRAKEN_INTERVALS` (`1m`, `5m`, `15m`, `30m`, `1h`, `4h`, `1d`).
- **Change streak length:** set `MIN_STREAK` (e.g. `3` for shorter streaks).
- **Change scan frequency:**
  - On **GitHub Actions**, edit the `cron` in the workflow file (e.g.
    `0 * * * *` for hourly).
  - On **Render / local loop**, set `CHECK_INTERVAL` (in seconds).
- **Mute the heartbeat:** set `SEND_SCAN_SUMMARY = False` (keeps streak alerts).

---

## How streak detection works

- A candle is **green** if `close > open`, **red** if `close < open`.
- Only **closed** candles count — the currently forming candle is ignored.
- The bot counts how many of the most recent closed candles share the same color.
- When that count reaches `MIN_STREAK`, you get an alert; you get another alert
  each time the streak extends, and the state resets when the streak breaks.
