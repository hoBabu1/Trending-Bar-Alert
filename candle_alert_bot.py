#!/usr/bin/env python3
"""
T-Bar Alert Bot

Reports consecutive same-color closed candles ("T-bar" = 5 or more in a row)
from Kraken OHLC data to three independent Telegram bots:

  1. bot4h        — fixed 10-coin report on the 4h timeframe, every 4h.
  2. dayMomentum  — user-configurable watchlist (config.json), per-timeframe.
  3. custom       — a second, independent watchlist on its own bot.

Dueness is derived from the candle data itself (Kraken's `last` field), not
from the wall clock, so a late or missed run catches up instead of skipping.
Kraken 30m/1h/4h boundaries align natively with the 05:30 IST anchor
(00:00 UTC == 05:30 IST, and IST has no DST).
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta, timezone

import requests


# --------------------------------------------------------------------------- #
# Load .env (local dev convenience; no external deps).
# In GitHub Actions the real env vars come from repository secrets.
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
# Config
# --------------------------------------------------------------------------- #
MIN_STREAK = 5                   # ⭐ marks a streak of this length or more
CANDLE_LIMIT = 120               # closed candles to consider per request
TELEGRAM_MSG_DELAY = 0.5         # seconds between Telegram messages
KRAKEN_FETCH_DELAY = 0.25        # seconds between Kraken OHLC requests
TELEGRAM_MAX_CHARS = 3500        # Telegram hard-limits at 4096; chunk below it
STATE_FILE = "alert_state.json"  # last reported candle close per stream
CONFIG_FILE = "config.json"      # watchlists, written by the web frontend
STATE_TTL_DAYS = 7               # prune state entries older than this
IST = timezone(timedelta(hours=5, minutes=30))

# Kraken is the data source because Binance (HTTP 451) and Bybit (HTTP 403)
# both geo-block US cloud/data-center IPs such as GitHub Actions runners.
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
KRAKEN_INTERVALS = {"30m": 30, "1h": 60, "4h": 240}

# The single supported coin list, shared with the frontend's picker. USDT pairs
# are used where Kraken has one; HYPE, UNI, XLM and NEAR are USD-only there.
COINS_FILE = os.path.join("docs", "coins.json")
FALLBACK_COINS = [
    ("BTC", "XBTUSDT"),
    ("BNB", "BNBUSDT"),
    ("ETH", "ETHUSDT"),
    ("XRP", "XRPUSDT"),
    ("HYPE", "HYPEUSD"),
    ("UNI", "UNIUSD"),
    ("XLM", "XXLMZUSD"),
    ("ADA", "ADAUSDT"),
    ("DOGE", "XDGUSDT"),   # Kraken calls DOGE "XDG"
    ("NEAR", "NEARUSD"),
]
BOT4H_TIMEFRAME = "4h"


def load_coins():
    """
    Read docs/coins.json — the one list both this scanner and the web picker
    use, so they can never disagree about which pair backs a coin. Falls back
    to the built-in list if the file is missing or unreadable.
    """
    try:
        with open(COINS_FILE) as fh:
            data = json.load(fh)
        coins = [(str(c["name"]), str(c["pair"])) for c in data]
        if not coins:
            raise ValueError("empty list")
        return coins
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read %s (%s); using the built-in list.", COINS_FILE, exc)
        return list(FALLBACK_COINS)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"

# Each stream has its own bot token, and falls back to a shared chat id when it
# doesn't define its own. A chat id env var may hold several comma-separated
# ids; every id receives the same message.
SHARED_CHAT_ENV = "TELEGRAM_CHAT_ID"
SECTION_LABELS = {"dayMomentum": "DayMomentum", "custom": "Custom"}
STREAMS = {
    "bot4h": {
        "label": "BOT4H",
        "token_env": "TELEGRAM_BOT_TOKEN_4H",
        "chat_env": "TELEGRAM_CHAT_ID_4H",
    },
    "dayMomentum": {
        "label": "MomentumBOT",
        "token_env": "TELEGRAM_BOT_TOKEN_MOMENTUM",
        "chat_env": "TELEGRAM_CHAT_ID_MOMENTUM",
    },
    "custom": {
        "label": "customTbarBOT",
        "token_env": "TELEGRAM_BOT_TOKEN_CUSTOM",
        "chat_env": "TELEGRAM_CHAT_ID_CUSTOM",
    },
}

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("candle_alert_bot")


# --------------------------------------------------------------------------- #
# State
#
# Maps a stream key -> the close time (ms) of the last candle already reported.
# Key format: "bot4h|4h" or "<section>|<pair>|<timeframe>".
#
# Rule 1 deliberately uses a SINGLE key for all ten coins: with per-coin keys a
# single failed fetch would leave that coin "due" on the next 30-minute tick and
# fire a bogus one-coin report off-boundary.
# --------------------------------------------------------------------------- #
def load_state():
    """Load alert state. A malformed entry is dropped without losing the rest."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001 - corrupt state shouldn't crash the bot
        log.error("Could not read %s (%s); starting fresh.", STATE_FILE, exc)
        return {}

    state = {}
    if not isinstance(data, dict):
        log.error("%s is not a JSON object; starting fresh.", STATE_FILE)
        return {}
    for key, value in data.items():
        try:
            state[key] = {
                "last_close_ms": int(value["last_close_ms"]),
                "updated": str(value["updated"]),
            }
        except Exception as exc:  # noqa: BLE001 - isolate per-entry corruption
            log.warning("Dropping malformed state entry %r (%s).", key, exc)
    return state


def prune_state(state, days=STATE_TTL_DAYS):
    """Drop entries not touched in `days` days, so the file stays bounded."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    kept = {}
    for key, value in state.items():
        try:
            updated = datetime.fromisoformat(value["updated"].replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001 - unparseable timestamp: treat as stale
            log.warning("Pruning state entry %r with unreadable timestamp.", key)
            continue
        if updated >= cutoff:
            kept[key] = value
        else:
            log.info("Pruning state entry %r (last updated %s).", key, value["updated"])
    return kept


def save_state(state):
    """Write state back to STATE_FILE."""
    try:
        with open(STATE_FILE, "w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except Exception as exc:  # noqa: BLE001
        log.error("Could not write %s: %s", STATE_FILE, exc)


def mark_reported(state, key, close_ms):
    state[key] = {
        "last_close_ms": int(close_ms),
        "updated": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def is_due(state, key, close_ms):
    """True when this candle close has not been reported yet."""
    if close_ms is None:
        return False
    entry = state.get(key)
    return entry is None or close_ms > entry["last_close_ms"]


# --------------------------------------------------------------------------- #
# Watchlist config (written by the web frontend)
# --------------------------------------------------------------------------- #
def load_config():
    """
    Read config.json. Shape:
        {"dayMomentum": [{"pair": "SOLUSD", "name": "SOL",
                          "timeframes": ["30m", "1h"]}], "custom": [...]}
    A missing or malformed file yields empty watchlists rather than an error.
    """
    empty = {"dayMomentum": [], "custom": []}
    if not os.path.exists(CONFIG_FILE):
        log.warning("%s not found; both watchlists are empty.", CONFIG_FILE)
        return empty
    try:
        with open(CONFIG_FILE) as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log.error("Could not read %s (%s); both watchlists are empty.", CONFIG_FILE, exc)
        return empty

    config = {}
    for section in ("dayMomentum", "custom"):
        entries = []
        for raw in data.get(section) or []:
            try:
                pair = str(raw["pair"]).strip()
                name = str(raw.get("name") or pair).strip()
                timeframes = [
                    tf for tf in raw.get("timeframes") or [] if tf in KRAKEN_INTERVALS
                ]
                if pair and timeframes:
                    entries.append(
                        {"pair": pair, "name": name, "timeframes": timeframes}
                    )
                else:
                    log.warning("Skipping %s entry %r (no pair or no valid timeframe).",
                                section, raw)
            except Exception as exc:  # noqa: BLE001 - isolate per-entry corruption
                log.warning("Skipping malformed %s entry %r (%s).", section, raw, exc)
        config[section] = entries
    return config


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def chunk_message(text, limit=TELEGRAM_MAX_CHARS):
    """
    Split on line boundaries so no chunk exceeds `limit`. Telegram rejects
    messages over 4096 chars with a 400, which would otherwise be swallowed
    and leave the user with no message at all.
    """
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def get_credentials(stream):
    """
    Return (token, [chat_ids]) for a stream, or (None, []) if unconfigured.

    The chat id is normally the same for all three streams (it identifies your
    Telegram account, not the bot), so a shared TELEGRAM_CHAT_ID is used unless
    the stream sets its own — which is what you'd do to route one stream to a
    group while the others stay in your DMs.
    """
    spec = STREAMS[stream]
    token = os.environ.get(spec["token_env"])
    raw = os.environ.get(spec["chat_env"]) or os.environ.get(SHARED_CHAT_ENV) or ""
    chat_ids = [cid.strip() for cid in raw.split(",") if cid.strip()]
    return token, chat_ids


def send_telegram(text, token, chat_ids):
    """
    Send an HTML message to every chat id. Returns True if every chunk reached
    at least one chat — state is only advanced on a fully successful send.
    """
    if not token or not chat_ids:
        log.error("Telegram credentials missing; cannot send message.")
        return False
    url = TELEGRAM_API_BASE.format(token=token)
    all_ok = True
    for chunk in chunk_message(text):
        sent_any = False
        for chat_id in chat_ids:
            try:
                resp = requests.post(
                    url,
                    data={
                        "chat_id": chat_id,
                        "text": chunk,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                sent_any = True
                time.sleep(TELEGRAM_MSG_DELAY)  # gentle rate limiting
            except Exception as exc:  # noqa: BLE001 - one bad chat shouldn't stop others
                # Telegram puts the real reason in the body ("chat not found",
                # "bot can't initiate conversation with a user", ...); the bare
                # status code alone doesn't say which.
                reason = ""
                try:
                    reason = f" — {resp.json().get('description', '')}"
                except Exception:  # noqa: BLE001
                    pass
                log.error("Failed to send to chat %s: %s%s", chat_id, exc, reason)
        all_ok = all_ok and sent_any
    return all_ok


# --------------------------------------------------------------------------- #
# Market data (Kraken)
# --------------------------------------------------------------------------- #
_candle_cache = {}


def fetch_candles(pair, timeframe, limit=CANDLE_LIMIT):
    """
    Fetch closed OHLC candles from Kraken.

    Returns (candles, latest_close_ms), or None on failure. `candles` is
    oldest-first with open/close/close_time.

    Kraken's `result["last"]` is the START timestamp of the last COMMITTED
    candle, so rows are selected with `start <= last`. Dropping the final row
    blindly would be wrong: in the seconds right after a boundary Kraken may
    not have emitted the new forming row yet, and a genuinely closed candle
    would be discarded — pushing the reported close time backwards and
    delaying the alert by a whole period.
    """
    interval = KRAKEN_INTERVALS.get(timeframe)
    if interval is None:
        log.error("Unsupported timeframe %s.", timeframe)
        return None

    cache_key = (pair, timeframe)
    if cache_key in _candle_cache:
        return _candle_cache[cache_key]

    # `since` trims the response from ~720 rows to what we actually need.
    since = int(time.time()) - (limit + 2) * interval * 60
    try:
        resp = requests.get(
            KRAKEN_OHLC_URL,
            params={"pair": pair, "interval": interval, "since": since},
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("error"):
            raise RuntimeError(", ".join(payload["error"]))
        result = payload["result"]
        last = result.get("last")
        # Data sits under a normalized pair key (e.g. XXBTZUSD) alongside "last".
        data_key = next((k for k in result if k != "last"), None)
        if data_key is None or last is None:
            raise RuntimeError("no OHLC data in response")
        raw = result[data_key]
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to fetch candles for %s %s: %s", pair, timeframe, exc)
        _candle_cache[cache_key] = None
        return None
    finally:
        time.sleep(KRAKEN_FETCH_DELAY)  # Kraken public endpoints are IP rate-limited

    # Kraken OHLC row: [time, open, high, low, close, vwap, volume, count].
    # time is the candle START in seconds; the list is oldest-first.
    candles = []
    for row in raw:
        start_s = int(row[0])
        if start_s > int(last):
            continue  # still forming
        candles.append(
            {
                "open": float(row[1]),
                "close": float(row[4]),
                "close_time": (start_s + interval * 60) * 1000,  # ms
            }
        )

    if not candles:
        log.error("No closed candles returned for %s %s.", pair, timeframe)
        _candle_cache[cache_key] = None
        return None

    candles = candles[-limit:]
    out = (candles, candles[-1]["close_time"])
    _candle_cache[cache_key] = out
    return out


def candle_color(candle):
    """Return 'green', 'red', or None (flat / unchanged)."""
    if candle["close"] > candle["open"]:
        return "green"
    if candle["close"] < candle["open"]:
        return "red"
    return None


def compute_streak(candles):
    """
    Trailing streak from the most recent closed candle backwards.
    Returns (color, length); color is None when the last candle is flat.

    A flat candle correctly ends the streak: Kraken fills no-trade periods with
    zero-volume candles where open == close, and treating those as continuing
    would show permanent fake streaks on illiquid pairs.
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
# Formatting
# --------------------------------------------------------------------------- #
def format_price(price):
    """Format a price with thousands separators and 2 decimals."""
    return f"{price:,.2f}"


def format_close_time(close_ms):
    """'12:00 UTC (17:30 IST)' — the candle close, never the wall clock."""
    dt = datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc)
    return f"{dt.strftime('%Y-%m-%d %H:%M')} UTC ({dt.astimezone(IST).strftime('%H:%M')} IST)"


def streak_emoji(color):
    return "🟢" if color == "green" else "🔴" if color == "red" else "⚪"


def format_streak(color, length):
    """'6🔴' — count then colour. The star is added by the table builder."""
    return f"{length}{streak_emoji(color)}"


def streak_move(candles, length):
    """Percent move from the streak's first open to the latest close."""
    if not candles or not length:
        return 0.0
    first_open = candles[-length]["open"]
    if not first_open:
        return 0.0
    return (candles[-1]["close"] - first_open) / first_open * 100


def display_width(text):
    """
    Width of `text` in monospace cells. Emoji render two columns wide but are
    a single character, so padding with len() drifts every column after one.
    """
    width = 0
    for ch in text:
        cp = ord(ch)
        if cp == 0xFE0F:            # variation selector: renders as nothing
            continue
        if (0x1F300 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF
                or 0x2B00 <= cp <= 0x2BFF):
            width += 2
        else:
            width += 1
    return width


def pad(text, width, right=False):
    fill = " " * max(0, width - display_width(text))
    return fill + text if right else text + fill


def build_table(headings, rows, right_align=()):
    """Render a fixed-width table for Telegram's <pre> block."""
    widths = [display_width(h) for h in headings]
    for row in rows:
        for i, cell in enumerate(row[:len(widths)]):
            widths[i] = max(widths[i], display_width(cell))

    def line(cells):
        return "  ".join(
            pad(c, widths[i], right=(i in right_align))
            for i, c in enumerate(cells)
        ).rstrip()

    head = line(headings)
    body = [head, "─" * min(display_width(head) + 2, 32)]
    body += [line(r) for r in rows]
    return "<pre>" + "\n".join(body) + "</pre>"


def header(title, close_ms, force):
    """Two lines: what this is, then the candle close in both zones."""
    dt = datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc)
    when = (f"{dt.strftime('%d %b')} · {dt.strftime('%H:%M')} UTC · "
            f"{dt.astimezone(IST).strftime('%H:%M')} IST")
    if force:
        title = "📸 <b>On-demand snapshot</b>"
    return f"{title}\n<i>{when}</i>\n"


# --------------------------------------------------------------------------- #
# Streams
# --------------------------------------------------------------------------- #
def deliver(stream, message, dry_run):
    """Send `message` to a stream's bot. Returns True when it was delivered."""
    label = STREAMS[stream]["label"]
    if dry_run:
        print(f"\n----- {label} ({stream}) -----\n{message}\n")
        return True
    token, chat_ids = get_credentials(stream)
    if not token or not chat_ids:
        # One unconfigured bot must not take down the other two.
        missing = STREAMS[stream]["token_env"] if not token else (
            f'{STREAMS[stream]["chat_env"]} (or {SHARED_CHAT_ENV})'
        )
        log.warning("%s not set; skipping %s.", missing, label)
        return False
    if send_telegram(message, token, chat_ids):
        log.info("Sent %s message.", label)
        return True
    log.error("Failed to send %s message.", label)
    return False


def run_bot4h(state, force=False, dry_run=False):
    """Rule 1: all ten fixed coins on 4h, as one report per 4h boundary."""
    key = f"bot4h|{BOT4H_TIMEFRAME}"
    rows, latest_close = [], None

    for name, pair in load_coins():
        fetched = fetch_candles(pair, BOT4H_TIMEFRAME)
        if fetched is None:
            rows.append([name, "—", "—", "⚠️"])
            continue
        candles, close_ms = fetched
        color, length = compute_streak(candles)
        latest_close = close_ms if latest_close is None else max(latest_close, close_ms)
        star = " ⭐" if length >= MIN_STREAK else ""
        rows.append([
            name,
            str(length),
            f"{streak_move(candles, length):+.2f}%",
            f"{streak_emoji(color)}{star}",
        ])
        log.info("bot4h %s 4h -> %s %s", name, length, color or "flat")

    if latest_close is None:
        log.error("bot4h: every fetch failed; nothing to report.")
        return
    if not force and not is_due(state, key, latest_close):
        log.info("bot4h: 4h candle %s already reported; nothing due.", latest_close)
        return

    message = (header("🕓 <b>4H T-Bar</b>", latest_close, force)
               + build_table(["COIN", "BARS", "MOVE", ""], rows,
                             right_align={1, 2}))
    delivered = deliver("bot4h", message, dry_run)
    # --force is a snapshot: it must not consume the scheduled report.
    if delivered and not force and not dry_run:
        mark_reported(state, key, latest_close)


def run_watchlist(section, state, config, force=False, dry_run=False):
    """
    Rules 2 and 3: report each configured coin on each of its subscribed
    timeframes, including only what closed at this tick.
    """
    entries = config.get(section) or []
    if not entries:
        log.info("%s: watchlist is empty; nothing to do.", section)
        return

    cells, due_keys, latest_close = {}, [], None
    live_tfs = []

    for entry in entries:
        pair, name = entry["pair"], entry["name"]
        for timeframe in ("30m", "1h", "4h"):
            if timeframe not in entry["timeframes"]:
                continue
            key = f"{section}|{pair}|{timeframe}"
            fetched = fetch_candles(pair, timeframe)
            if fetched is None:
                cells[(name, timeframe)] = "⚠️"
                continue
            candles, close_ms = fetched
            if not force and not is_due(state, key, close_ms):
                continue  # this timeframe hasn't closed again since last report
            color, length = compute_streak(candles)
            star = "⭐" if length >= MIN_STREAK else ""
            cells[(name, timeframe)] = f"{format_streak(color, length)}{star}"
            due_keys.append((key, close_ms))
            if timeframe not in live_tfs:
                live_tfs.append(timeframe)
            latest_close = close_ms if latest_close is None else max(latest_close, close_ms)
            log.info("%s %s %s -> %s %s", section, name, timeframe, length, color or "flat")

    if not due_keys or latest_close is None:
        log.info("%s: nothing due at this tick.", section)
        return

    # Only show timeframe columns that actually closed at this tick.
    live_tfs = [tf for tf in ("30m", "1h", "4h") if tf in live_tfs]
    names, seen = [], set()
    for entry in entries:
        n = entry["name"]
        if n not in seen and any((n, tf) in cells for tf in live_tfs):
            names.append(n)
            seen.add(n)

    rows = [[n] + [cells.get((n, tf), "·") for tf in live_tfs] for n in names]
    message = (header(f"🔍 <b>{SECTION_LABELS.get(section, section)}</b>", latest_close, force)
               + build_table(["COIN"] + live_tfs, rows))
    delivered = deliver(section, message, dry_run)
    if delivered and not force and not dry_run:
        for key, close_ms in due_keys:
            mark_reported(state, key, close_ms)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_scan(force=False, dry_run=False):
    log.info("=== Starting scan (force=%s, dry_run=%s) ===", force, dry_run)
    _candle_cache.clear()
    state = load_state()
    config = load_config()

    for runner in (
        lambda: run_bot4h(state, force=force, dry_run=dry_run),
        lambda: run_watchlist("dayMomentum", state, config, force=force, dry_run=dry_run),
        lambda: run_watchlist("custom", state, config, force=force, dry_run=dry_run),
    ):
        try:
            runner()
        except Exception as exc:  # noqa: BLE001 - one stream must not kill the others
            log.error("Stream failed: %s", exc, exc_info=True)

    if not dry_run:
        save_state(prune_state(state))
    log.info("=== Scan complete ===")


if __name__ == "__main__":
    # --force    report every stream's current status regardless of dueness,
    #            without consuming the next scheduled report (the "Now" button).
    # --dry-run  print the messages instead of sending; never touches state.
    run_scan(force="--force" in sys.argv, dry_run="--dry-run" in sys.argv)
