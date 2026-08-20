# T-Bar Alert

Telegram alerts for **T-bars** — 5 or more consecutive same-colour candles — plus
periodic momentum reports, driven by Kraken OHLC data and GitHub Actions.

A "T-bar" is 5+ candles of the same colour in a row on a given timeframe. Counts
are always reported; a count of 5 or more is marked with ⭐.

## The three streams

| Stream | Bot | Coins | When |
|---|---|---|---|
| **Rule 1** | `BOT4H` | Fixed 10: BTC, BNB, ETH, XRP, HYPE, UNI, XLM, ADA, DOGE, NEAR | Every 4h close |
| **Rule 2** | `MomentumBOT` | `dayMomentum` list in `config.json` | Every close of each coin's subscribed timeframes |
| **Rule 3** | `customTbarBOT` | `custom` list in `config.json` | Same, independent list |

Rules 2 and 3 are functionally identical — two separate watchlists so you can run
a settled list and an experimental one side by side.

### Message formats

```
🕓 4H T-Bar — 2026-08-20 12:00 UTC (17:30 IST)

BTC: 3🟢
BNB: 6🔴 ⭐
DOGE: ⚠️ unavailable
```

```
🔍 Scan complete — 2026-08-20 12:00 UTC (17:30 IST)

BTC: 30m 1🟢 | 1h 1🔴
SOL: 30m 2🟢
```

Only the coins and timeframes that actually closed at that tick appear. At a
12:00 tick both 30m and 1h close, so a coin subscribed to both shows both lines
in one message; at 12:30 only the 30m column appears.

## Timing

Time is anchored to **05:30 AM IST**, which is 00:00 UTC. IST is UTC+5:30 with no
DST, so Kraken's native candle boundaries already line up:

- **4h** closes at 05:30 / 09:30 / 13:30 / 17:30 / 21:30 / 01:30 IST
- **1h** closes at :30 past each hour IST
- **30m** closes at :00 and :30 IST

The scanner never reads the wall clock to decide whether to fire. It compares the
latest closed candle (from Kraken's `last` field) against the last close it
already reported, stored in `alert_state.json`. A late or missed run therefore
catches up on the next tick instead of silently skipping an alert.

## Setup

### 1. Telegram bots

Create three bots with [@BotFather](https://t.me/BotFather), then add **four**
repository secrets under **Settings → Secrets and variables → Actions**:

```
TELEGRAM_BOT_TOKEN_4H
TELEGRAM_BOT_TOKEN_MOMENTUM
TELEGRAM_BOT_TOKEN_CUSTOM
TELEGRAM_CHAT_ID              # shared by all three bots
```

One chat id covers all three, because it identifies your Telegram account rather
than the bot. To find it, message [@userinfobot](https://t.me/userinfobot) — the
number it replies with is your chat id.

The id may be a comma-separated list (`123456789,987654321`) to reach several
people. To send one stream somewhere else — a group, say — set the optional
override `TELEGRAM_CHAT_ID_4H`, `TELEGRAM_CHAT_ID_MOMENTUM`, or
`TELEGRAM_CHAT_ID_CUSTOM`; where set, it wins over the shared value. Group ids
are negative, like `-1001234567890`.

A stream whose token is missing is skipped with a warning; the other two still
run.

### 2. Trigger

GitHub's own scheduler is unreliable, so the real trigger is an external cron
(cron-job.org) calling the `workflow_dispatch` API every 30 minutes. Set it to
fire at **:05 and :35** — a few minutes after each boundary, never inside it.
The `schedule:` block in the workflow is a best-effort backup.

### 3. Frontend

Enable **Settings → Pages** with source `master` / `/docs`. The page at
`https://<owner>.github.io/<repo>/` lets you edit the two watchlists and press
**Now** for an immediate snapshot.

It needs a fine-grained PAT scoped to this repository with **Contents:
read/write** and **Actions: read/write**. Paste it into the connection settings
panel; it is stored in your browser's localStorage and sent only to
`api.github.com`.

> ⚠️ A project Pages site is served from `https://<owner>.github.io/` — **one
> origin shared by every project page you own**. Any other page under that
> account can read the token out of localStorage. Scope it to this repo alone and
> give it a short expiry (30–90 days). If the repo is public, your watchlists are
> publicly readable too. To avoid both, skip Pages and open `docs/index.html`
> from disk instead — everything including **Now** still works.

## Files

| File | Purpose |
|---|---|
| `candle_alert_bot.py` | The scanner — all three streams |
| `config.json` | The two watchlists; written by the frontend |
| `alert_state.json` | Last reported candle close per stream; committed by the workflow |
| `docs/index.html` | The frontend, self-contained, no build step |
| `.github/workflows/candle-alert.yml` | Runs the scan and commits state back |

### `config.json`

```json
{
  "dayMomentum": [
    { "pair": "SOLUSD", "name": "SOL", "timeframes": ["30m", "1h"] }
  ],
  "custom": [
    { "pair": "XXBTZUSD", "name": "BTC", "timeframes": ["4h"] }
  ]
}
```

`pair` is the Kraken pair id, `name` the display label. Storing both means adding
a coin never requires a code change. A missing or malformed file is treated as
two empty lists. The frontend caps each section at 25 coins.

### `alert_state.json`

```json
{
  "bot4h|4h":               { "last_close_ms": 1787212800000, "updated": "2026-08-20T12:00:00Z" },
  "dayMomentum|SOLUSD|30m": { "last_close_ms": 1787229000000, "updated": "2026-08-20T12:30:00Z" }
}
```

Rule 1 uses a **single** `bot4h|4h` key for all ten coins, so one failed fetch
can't trigger a bogus one-coin report off-boundary — the coin shows as
`⚠️ unavailable` in an otherwise complete report. Entries untouched for 7 days
are pruned. (If the cron dies for over a week the state empties and all three
bots re-fire together on resume.)

## Running locally

```bash
pip install -r requirements.txt

python candle_alert_bot.py --dry-run          # print what's due, send nothing
python candle_alert_bot.py --force --dry-run  # print everything's status
python candle_alert_bot.py --force            # send a 📸 snapshot to all three bots
python candle_alert_bot.py                    # a normal scan
```

`--force` backs the **Now** button. It reports current status regardless of
dueness and does **not** advance state, so the next scheduled report still
arrives normally. It is labelled `📸 On-demand snapshot` so you can tell the two
apart. `--dry-run` never writes state.

For local runs put the same four values in a `.env` file (gitignored):

```
TELEGRAM_BOT_TOKEN_4H=123456:ABC-your-4h-token
TELEGRAM_BOT_TOKEN_MOMENTUM=123456:ABC-your-momentum-token
TELEGRAM_BOT_TOKEN_CUSTOM=123456:ABC-your-custom-token
TELEGRAM_CHAT_ID=123456789
```

## Notes on the data source

Kraken is used because Binance (HTTP 451) and Bybit (HTTP 403) both geo-block the
US data-centre IPs that GitHub runners use.

Kraken fills no-trade periods with zero-volume candles where `open == close`.
Those count as flat (⚪) and end a streak, which is deliberate: treating them as
streak-continuing would show permanent fake T-bars on illiquid pairs, and the
coin picker offers every Kraken USD pair.
