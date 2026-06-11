#!/usr/bin/env python3
"""
Crypto Candle Streak Telegram Alert Bot

Monitors Binance for consecutive same-color closed candles and sends a Telegram
alert whenever a coin/timeframe shows MIN_STREAK or more candles in a row of the
same color. Re-alerts on every new candle that extends an active streak.
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone

import requests


# --------------------------------------------------------------------------- #
# Load .env (local dev convenience; no external deps).
# On Render you set real env vars, so this is a no-op there.
# --------------------------------------------------------------------------- #
def load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Don't override variables already set in the real environment.
            if key and key not in os.environ:
                os.environ[key] = value


load_dotenv()

# --------------------------------------------------------------------------- #
# Config (edit these to customize)
# --------------------------------------------------------------------------- #
SYMBOLS = ["BTCUSDT", "SOLUSDT", "XRPUSDT", "UNIUSDT", "AVAXUSDT"]
TIMEFRAMES = ["30m", "1h", "4h"]
MIN_STREAK = 5                  # alert when this many same-color candles in a row
CHECK_INTERVAL = 30 * 60        # seconds between full scans (30 minutes)
CANDLE_LIMIT = 50               # how many candles to fetch per request
TELEGRAM_MSG_DELAY = 0.5        # seconds between Telegram messages (rate limit)
STATE_FILE = "alert_state.json" # persists which streaks we already alerted on
SEND_SCAN_SUMMARY = True        # send a Telegram summary after every scan (heartbeat)

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("candle_alert_bot")

# Tracks the last streak length we already alerted on, so we only send a new
# message when the streak grows (5 -> 6 -> 7 ...) and reset when it breaks.
# Key: (symbol, timeframe) -> (color, last_alerted_length)
# Persisted to STATE_FILE so it survives across separate cron runs.
_alert_state = {}


def _state_key_str(symbol, timeframe):
    """JSON keys must be strings, so flatten the (symbol, timeframe) tuple."""
    return f"{symbol}|{timeframe}"


def load_state():
    """Load alert state from STATE_FILE into _alert_state."""
    _alert_state.clear()
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
        for key_str, value in data.items():
            symbol, _, timeframe = key_str.partition("|")
            _alert_state[(symbol, timeframe)] = (value["color"], value["length"])
    except Exception as exc:  # noqa: BLE001 - corrupt state shouldn't crash the bot
        log.error("Could not read %s (%s); starting fresh.", STATE_FILE, exc)


def save_state():
    """Write _alert_state back to STATE_FILE."""
    try:
        data = {
            _state_key_str(sym, tf): {"color": color, "length": length}
            for (sym, tf), (color, length) in _alert_state.items()
        }
        with open(STATE_FILE, "w") as fh:
            json.dump(data, fh, indent=2)
    except Exception as exc:  # noqa: BLE001
        log.error("Could not write %s: %s", STATE_FILE, exc)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def send_telegram(text):
    """Send an HTML message to Telegram. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram credentials missing; cannot send message.")
        return False
    try:
        url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN)
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        resp.raise_for_status()
        time.sleep(TELEGRAM_MSG_DELAY)  # gentle rate limiting
        return True
    except Exception as exc:  # noqa: BLE001 - never crash the loop on send failure
        log.error("Failed to send Telegram message: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# Binance data
# --------------------------------------------------------------------------- #
def fetch_candles(symbol, timeframe, limit=CANDLE_LIMIT):
    """
    Fetch klines from Binance. Returns a list of dicts with open/close/close_time
    for CLOSED candles only (the last, still-forming candle is dropped).
    Returns None on failure.
    """
    try:
        resp = requests.get(
            BINANCE_KLINES_URL,
            params={"symbol": symbol, "interval": timeframe, "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to fetch candles for %s %s: %s", symbol, timeframe, exc)
        return None

    # Binance kline format: [open_time, open, high, low, close, volume,
    #                        close_time, ...]
    candles = []
    for k in raw:
        candles.append(
            {
                "open": float(k[1]),
                "close": float(k[4]),
                "close_time": int(k[6]),  # ms
            }
        )

    # Drop the currently forming candle (last one is not yet closed).
    if candles:
        candles = candles[:-1]
    return candles


def candle_color(candle):
    """Return 'green', 'red', or None (doji / unchanged)."""
    if candle["close"] > candle["open"]:
        return "green"
    if candle["close"] < candle["open"]:
        return "red"
    return None


def compute_streak(candles):
    """
    Compute the trailing streak from the most recent closed candle backwards.
    Returns (color, length). color is None if no streak (e.g. doji at the end).
    """
    if not candles:
        return None, 0

    last_color = candle_color(candles[-1])
    if last_color is None:
        return None, 0

    length = 0
    for candle in reversed(candles):
        if candle_color(candle) == last_color:
            length += 1
        else:
            break
    return last_color, length


# --------------------------------------------------------------------------- #
# Alert formatting
# --------------------------------------------------------------------------- #
def coin_name(symbol):
    """BTCUSDT -> BTC (strip the USDT quote)."""
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def format_price(price):
    """Format a price with thousands separators and 2 decimals."""
    return f"{price:,.2f}"


def build_alert_message(symbol, timeframe, color, length, last_close, close_time_ms):
    emoji = "🟢" if color == "green" else "🔴"
    coin = coin_name(symbol)
    dt = datetime.fromtimestamp(close_time_ms / 1000, tz=timezone.utc)
    time_str = dt.strftime("%Y-%m-%d %H:%M")

    return (
        f"{emoji} <b>Candle Streak Alert!</b>\n\n"
        f"📌 Coin      : <b>{coin}</b>\n"
        f"⏱ Timeframe : <b>{timeframe}</b>\n"
        f"🕯 Streak    : <b>{length} consecutive {color} candles</b>\n"
        f"💰 Last Close: <b>${format_price(last_close)}</b>\n"
        f"🕒 Time (UTC): <b>{time_str}</b>"
    )


# --------------------------------------------------------------------------- #
# Core check
# --------------------------------------------------------------------------- #
def check_symbol_timeframe(symbol, timeframe, dry_run=False):
    """
    Check one symbol/timeframe. Sends an alert if a new/extended streak of
    MIN_STREAK+ is detected. Returns (color, length) for inspection.
    """
    key = (symbol, timeframe)
    candles = fetch_candles(symbol, timeframe)
    if candles is None:
        return None, 0

    color, length = compute_streak(candles)
    log.info(
        "Checked %s %s -> streak: %s %s candle(s)",
        symbol,
        timeframe,
        length,
        color or "none",
    )

    if dry_run:
        return color, length

    # No qualifying streak: reset state so the next qualifying streak alerts.
    if color is None or length < MIN_STREAK:
        if key in _alert_state:
            log.info("Streak broken for %s %s; resetting alert state.", symbol, timeframe)
            _alert_state.pop(key, None)
        return color, length

    prev_color, prev_len = _alert_state.get(key, (None, 0))

    # Alert if this is a new streak (different color) or it has grown longer.
    if color != prev_color or length > prev_len:
        last = candles[-1]
        msg = build_alert_message(
            symbol, timeframe, color, length, last["close"], last["close_time"]
        )
        if send_telegram(msg):
            log.info(
                "ALERT sent: %s %s %d %s candles", symbol, timeframe, length, color
            )
            _alert_state[key] = (color, length)
    else:
        log.info(
            "Streak %s %s already alerted at length %d; skipping.",
            symbol,
            timeframe,
            prev_len,
        )

    return color, length


def build_summary_message(results):
    """
    Build a compact heartbeat message of every coin/timeframe result.
    `results` is a dict: (symbol, timeframe) -> (color, length).
    """
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    lines = [f"🔍 <b>Scan complete</b> — {now} UTC\n"]
    for symbol in SYMBOLS:
        parts = []
        for timeframe in TIMEFRAMES:
            color, length = results.get((symbol, timeframe), (None, 0))
            if color == "green":
                emoji = "🟢"
            elif color == "red":
                emoji = "🔴"
            else:
                emoji = "⚪"
            # ⭐ marks a streak that hit the alert threshold
            star = "⭐" if (color and length >= MIN_STREAK) else ""
            parts.append(f"{timeframe} {length}{emoji}{star}")
        lines.append(f"<b>{coin_name(symbol)}</b>: " + " | ".join(parts))
    return "\n".join(lines)


def run_scan():
    """Run one full scan over all symbols and timeframes (state is persisted)."""
    log.info("=== Starting scan ===")
    load_state()
    results = {}
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            try:
                color, length = check_symbol_timeframe(symbol, timeframe)
                results[(symbol, timeframe)] = (color, length)
            except Exception as exc:  # noqa: BLE001 - isolate per-check failures
                log.error("Error checking %s %s: %s", symbol, timeframe, exc)
    save_state()

    if SEND_SCAN_SUMMARY and results:
        send_telegram(build_summary_message(results))
        log.info("Scan summary sent to Telegram.")
    log.info("=== Scan complete ===")


def _require_credentials():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set as environment "
            "variables. Exiting."
        )
        raise SystemExit(1)


def run_once():
    """Single scan then exit — used by GitHub Actions / cron schedulers."""
    _require_credentials()
    log.info("Running single scan (--once mode).")
    run_scan()
    log.info("Single scan finished. Exiting.")


def run_forever():
    """Infinite loop with built-in sleep — used for always-on hosts / local."""
    _require_credentials()
    startup = (
        "🤖 Candle Alert Bot is live! Monitoring BTC, SOL, XRP, UNI, AVAX "
        "on 30m / 1h / 4h."
    )
    send_telegram(startup)
    log.info("Startup message sent. Beginning monitoring loop.")

    while True:
        try:
            run_scan()
        except Exception as exc:  # noqa: BLE001 - never let the loop die
            log.error("Unexpected error during scan: %s", exc)
        log.info("Sleeping %d seconds until next scan.", CHECK_INTERVAL)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    # `--once` = run one scan and exit (GitHub Actions / cron).
    # no args  = run forever (Render worker / local machine).
    if "--once" in sys.argv:
        run_once()
    else:
        run_forever()
