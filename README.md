# 🕯 Crypto Candle Streak Telegram Alert Bot

A lightweight Python bot that watches Binance and sends you a **Telegram alert**
whenever a coin prints **5 or more consecutive green or red closed candles** on a
given timeframe. It keeps alerting as the streak grows (5th → 6th → 7th candle…)
and stops once the streak breaks.

- **Coins:** BTC, SOL, XRP, UNI, AVAX
- **Timeframes:** 30m, 1h, 4h
- **Data source:** Binance public REST API (no API key required)
- **Dependencies:** just `requests`

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

## 4. Deploy on Render.com

This bot runs as a **Background Worker** (no web server / no open port).

1. Push this folder to a GitHub repository.
2. Go to [Render.com](https://render.com) → **New** → **Background Worker**.
3. Connect your GitHub repo.
4. Render auto-detects `render.yaml`. If asked manually, set:
   - **Runtime:** Python
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python candle_alert_bot.py`
5. Create the service.

### Set environment variables on Render

In the service dashboard → **Environment** → **Add Environment Variable**:

| Key                  | Value                          |
| -------------------- | ------------------------------ |
| `TELEGRAM_BOT_TOKEN` | your BotFather token           |
| `TELEGRAM_CHAT_ID`   | your numeric chat id           |

Save — Render will redeploy automatically.

> ⚠️ **Free tier note:** Render's free plan does **not** support always-on
> background workers (they spin down). To keep the bot running 24/7 you need the
> **Starter plan (~$7/month)**.

---

## 5. Example alert

```
🟢 Candle Streak Alert!

📌 Coin      : BTC
⏱ Timeframe : 1h
🕯 Streak    : 5 consecutive green candles
💰 Last Close: $67,432.10
🕒 Time (UTC): 2024-01-15 14:00
```

---

## 6. Customize

All settings live at the top of [candle_alert_bot.py](candle_alert_bot.py):

```python
SYMBOLS = ["BTCUSDT", "SOLUSDT", "XRPUSDT", "UNIUSDT", "AVAXUSDT"]
TIMEFRAMES = ["30m", "1h", "4h"]
MIN_STREAK = 5            # alert threshold
CHECK_INTERVAL = 30 * 60  # scan frequency in seconds
```

- **Add/remove coins:** edit `SYMBOLS` (use Binance symbols, e.g. `"DOGEUSDT"`).
- **Change timeframes:** edit `TIMEFRAMES` (Binance intervals: `1m`, `5m`, `15m`,
  `30m`, `1h`, `2h`, `4h`, `1d`, …).
- **Change streak length:** set `MIN_STREAK` (e.g. `3` for shorter streaks).
- **Change scan frequency:** set `CHECK_INTERVAL` (in seconds).

---

## How streak detection works

- A candle is **green** if `close > open`, **red** if `close < open`.
- Only **closed** candles count — the currently forming candle is ignored.
- The bot counts how many of the most recent closed candles share the same color.
- When that count reaches `MIN_STREAK`, you get an alert; you get another alert
  each time the streak extends, and the state resets when the streak breaks.
