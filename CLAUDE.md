# Signal 500 — Project Memory

**Live at:** [sp500-screener-pwa.vercel.app](https://sp500-screener-pwa.vercel.app)
**Repo:** github.com/dickie2-dot/sp500-screener-pwa
**Owner:** Owen (finba2903@gmail.com)

This file is auto-loaded by Claude Code at session start — it preserves everything we've built, decided, learned, and what's pending. **Update it when major changes ship.**

---

## What Signal 500 is

A PWA that surfaces S&P 500 "quality fallen angel" and "trend pullback" setups, scores them, and tracks both historical (backtest) and live (forward-test) performance. The core thesis: **good companies trading below their 200-week WMA mean-revert over 6-12 months**. Backtest validates this; live tracking is accumulating from Apr 2026.

Not a day-trading signal. Not a buy recommendation. Designed for multi-quarter position ideas.

---

## Repo layout

```
sp500-screener-pwa/
├── api/
│   ├── signals.py       # Frontend data read — returns screener_results from Edge Config
│   ├── chart.py         # Per-ticker 90d price + WMA chart data (Yahoo)
│   ├── screen.py        # Serverless fallback screener — currently unused (see below)
│   └── debug.py         # Diagnostic: shows EDGE_CONFIG env value
├── screener/
│   ├── run.py           # ★ CANONICAL PIPELINE — downloads, screens, scores, writes Edge Config
│   ├── backtest.py      # Historical simulator — outputs picks_log.csv + console report
│   └── requirements.txt # pandas, requests
├── public/
│   └── index.html       # ★ SINGLE-FILE PWA (vanilla JS + Chart.js CDN, no build step)
├── .github/workflows/
│   └── run-screener.yml # Nightly GH Actions cron — runs screener/run.py at 21:30 UTC weekdays
├── vercel.json          # Deploy config (output dir = public)
└── CLAUDE.md            # ← this file
```

**Desktop context:**
- `.env` with secrets lives at `C:\Users\owen7\Desktop\sp500-screener\.env` (contains `POLYGON_API_KEY`, `EDGE_CONFIG_ID`, `VERCEL_API_TOKEN`, `TIINGO_API_TOKEN`)
- Local venv at `C:\Users\owen7\Desktop\sp500-screener\venv\` (has pandas, requests)
- `run_screener.bat` on Desktop activates venv + runs `screener/run.py`

---

## Data pipeline

```
┌─────────────────────┐    ┌─────────────────────────┐    ┌──────────────────────┐
│ GitHub Actions      │───▶│ screener/run.py         │───▶│ Vercel Edge Config   │
│ (nightly 21:30 UTC) │    │ Yahoo parallel fetch    │    │ key: screener_results│
│ OR Desktop .bat     │    │ → screen → score → log  │    └──────────┬───────────┘
└─────────────────────┘    └─────────────────────────┘               │
                                                                     ▼
                                               ┌────────────────────────────────┐
                                               │ api/signals.py (Vercel Python) │
                                               │ Reads Edge Config, returns JSON│
                                               └────────────┬───────────────────┘
                                                            │
                                                            ▼
                                               ┌────────────────────────────────┐
                                               │ public/index.html (static PWA) │
                                               │ Fetches once on page load      │
                                               └────────────────────────────────┘
```

**Data source:** Yahoo Finance (`/v8/finance/chart`). Parallel fetch via `ThreadPoolExecutor(max_workers=20)`. Default `range=2y` (for live runs); backtest uses `range=10y`.

**No polling.** PWA fetches `/api/signals` once on load and stays static until browser refresh. Removed all `setInterval`/refresh timers to cut API load (data only changes after the nightly scrape).

---

## Edge Config schema (`screener_results` key)

```json
{
  "date": "2026-04-23",
  "last_scraped": "2026-04-23 22:15 UTC",
  "trend": ["...tickers..."],
  "turnaround": ["...tickers..."],
  "trend_count": 1,
  "turnaround_count": 8,
  "breadth_pct": 54.4,
  "top_risers":  [{"ticker": "X", "change": 5.1}, ...],
  "top_fallers": [...],
  "scores": { "APTV": 100, "MKC": 92, ... },
  "top5": ["APTV", "MKC", ...],
  "top5_history": [  // rolling 7 days of top-5 picks for the Watchlist UI
    { "date": "2026-04-17", "picks": [
        { "ticker": "APTV", "entry_price": 45.23, "score": 100, "type": "turnaround" },
        ...
    ]},
    ...
  ],
  "performance_log": [  // append-only forward-test log, capped at 1500 entries
    { "date": "2026-04-22", "ticker": "APTV", "type": "turnaround", "score": 100,
      "entry_price": 45.23,
      "r":     [null, null, null, null, null, null],  // realised % at 5/20/60/120/180/250 days
      "spy_r": [null, null, null, null, null, null]   // SPY baseline over same windows
    },
    ...
  ]
}
```

Edge Config item limit is **512 KB**. Log capped at 1500 entries (~350 KB) to stay well under.

---

## Screener logic (current)

### Universe
S&P 500 from Wikipedia (`List_of_S%26P_500_companies`), regex-scraped. ~500 tickers, ~498 usable after Yahoo download + 260-bar history requirement.

### Liquidity filter
`avg_dollar_vol_20 >= $10M` over prior 20 days (excludes today). Drops cheap, illiquid names.

### Midday safety
`trim_partial_session()` drops today's bar if run pre-16:00 NY time — keeps midday manual runs consistent with overnight runs.

### List 1 — Trend Radar (pullback in uptrend)
Stock must satisfy ALL:
- `price > WMA200`
- `WMA50 > WMA200`
- `WMA200` rising (today > 20 bars ago)
- `price > WMA50`
- RSI touched < 40 in the last 10 days (*pullback occurred*)
- RSI today > 45 (*recovery started*)
- RSI today < 70 (*not overbought*)
- Volume today > 1.5× avg_vol_20 (*volume confirmation*)

**History:** previously required `RSI < 30 in last 5 days then > 30` — that's self-contradictory with the trend criteria (uptrenders rarely hit extreme oversold). Fixed 2026-04-23.

### List 2 — Quality Fallen Angels (mean reversion)
Stock must satisfy ALL:
- `price < WMA200`
- 25% ≤ `pct_off_52w_high` ≤ 45% (*tightened from 25–75% after backtest*)
- MACD crossed up within last 20 days
- Bullish volume divergence over last 5 days (up-day vol > down-day vol)
- 35 ≤ RSI ≤ 45 (*tightened from 30–55*)
- `price > 20d_low × 1.05` (*actual bounce started*)

**History:**
- `week52_high` originally used `close.expanding().max()` (all-time high) — fixed to `close.rolling(252).max()` so old peaks don't permanently gate stocks.
- Bounds tightened based on 2-year backtest showing looser bounds produced -0.28% excess vs SPY; tighter bounds produced +0.39% at 60d and +3.94% at 180d.

### Regime gate
After breadth_pct computed:
- Trend signals require `breadth_pct >= 45%`
- Fallen Angels require `breadth_pct >= 30%`
- Below thresholds, the category is muted entirely. Prevents knife-catching in breadth collapses.

---

## Scoring — Setup Fit (0-100)

**Fallen Angel score:**
```python
rsi_score   = max(0, 50 - abs(rsi - 40) * 2)           # peaks at RSI=40
depth_score = max(0, 50 - max(0, pct_off_high*100 - 35) * 1.5)  # peaks at ≤35% off high
score = rsi_score + depth_score   # cap at 100
```

**Trend Radar score:**
```python
rsi_score = max(0, 50 - abs(rsi - 55) * 2)             # peaks at RSI=55
vol_score = min(50, max(0, (vol_ratio - 1.0) * 25))    # rewards larger volume surges
score = rsi_score + vol_score
```

**Top 5** = highest score across both categories (mixed).

**Known issue:** score isn't monotonic in the 60-89 range — 70-79 and 100 both outperform, 80-89 is dead zone. Pending rework via regression on `picks_log.csv`.

---

## Backtest results (4-year window, Apr 2021 – Apr 2025)

Run via `python screener/backtest.py` (needs `BACKTEST_YEARS=4` + Yahoo 10y fetch, cached after first run).

### Headline numbers (n=2247 picks)

| Horizon | All | Fallen Angels (n=1187) | Trend Radar (n=1060) |
|---|---|---|---|
| 60d excess vs SPY | +0.41% | **+1.22%** | -0.50% |
| 120d excess vs SPY | +2.42% | **+3.09%** | +1.68% |
| **180d excess vs SPY** | **+2.99%** | **+3.94%** (62.7% win) | +1.92% |
| 250d excess vs SPY | +4.42% | +3.74% | **+5.19%** |

### Key finding
Thesis validated on multi-quarter holds. Short horizons (5-20d) are noise-dominated; edge emerges at 60d+, is clearest at 180d for fallen angels. Trend Radar wins longer (250d) in a bull window but underperforms at 60d.

**Frontend `BACKTEST` constant in `public/index.html` hardcodes the current numbers — update after each fresh backtest run.**

---

## Live forward-test tracking

Added 2026-04-23.

Each nightly `run.py`:
1. Fetches SPY alongside the S&P 500 download
2. Reads existing `performance_log` from Edge Config (starts empty, append-only)
3. Seeds today's 5 picks with `r: [null]*6, spy_r: [null]*6`
4. For every prior entry, back-fills realised returns at any horizon that matured since the last run (5/20/60/120/180/250 days)
5. Caps log at 1500 entries (oldest dropped)

Idempotent: reruns on the same date *replace* today's entries rather than duplicating.

**First 180d cells mature ~Oct 2026.** Full seasoned track record ~Apr 2027.

---

## Automation

**GitHub Actions** (`.github/workflows/run-screener.yml`):
- Schedule: `30 21 * * 1-5` (21:30 UTC weekdays; typically fires 22:10-22:20 UTC due to queue)
- Manual trigger: workflow_dispatch
- Uses Yahoo (no Polygon rate-limit concerns). Full run ~1m 30s.
- Secrets required: `EDGE_CONFIG_ID`, `VERCEL_API_TOKEN` (Polygon token still set but unused — ignore)

**Previous `nightly-screener.yml`** was a duplicate I added, then removed — `run-screener.yml` was pre-existing.

**Previous Vercel cron** (`/api/screen` at 21:00 UTC weekdays) was removed from `vercel.json` — it wrote to the wrong Edge Config key (`signals` instead of `screener_results`) and used SMA not WMA. `api/screen.py` still exists (corrected) but isn't cron-triggered.

---

## PWA UI structure

Single-file SPA in `public/index.html`. Three tabs in the header, three dot indicators at the bottom.

### Signals tab
- "Today's Signals" header with count
- **✦ Top Picks section** (rank-ordered from `top5`, rendered first)
  - Disclaimer explains: *Ranked by Setup Fit … Designed for 6-12 month holds, not day-trading. 4y backtest: +3.9% excess vs SPY @ 180d, 62.7% win rate on fallen angels.*
- **Fallen Angels section** (remaining hits not in top-5)
- **Trend Radar section** (remaining hits not in top-5)
- Each card: ticker, name, category chip, Setup Fit score (with "· 6-12mo" caption), face-chip showing `→ WMA50 +X%` / `→ WMA200 +X%`
- Expanded card: 90d price chart (Price / WMA20 / WMA50 / WMA200) + WMA blocks showing each WMA value + % gain to reach + Last Price block

### Market tab
- Animated semi-circular breadth gauge + above/below/total counts
- Biggest Risers / Biggest Fallers cards (5 rows each)

### History tab (order matters)
1. **Backtest Performance** (hardcoded stats in `BACKTEST` object — update on rerun)
2. **Live Track Record** (reads `data.performance_log`, aggregates client-side per horizon)
3. **7-Day Watchlist** (from `top5_history` — unique tickers with entry date/price, live current price, % P/L, `Nx PICKS` badge for repeats)
4. **Signal Activity** (historical count of daily fallen angel signals — currently placeholder; `signal_history` not yet written)

### Header
- Live calibration indicator (static dot + blinking animation)
- `Scraped YYYY-MM-DD HH:MM UTC` — drives from `data.last_scraped`, proves automation ran
- Title "Signal 500" + subtitle "Fallen Angels • S&P 500"

### Mobile quirk fixed
Positive EOD percentages (`+X.XX%`) overflowed tiles on narrow screens because `+` isn't a line-break character (whereas `-` is). Fixed by adding a space between price and percent span plus `overflow-wrap: anywhere` on `.wma-block-val`.

---

## Design tokens / styling

Warm off-white / cream background (`--cream #f5f2ed`), sage green accent (`--sage #6b8f6b`), white cards with subtle shadow, large border-radius (18px on cards, 10-12px on blocks), Inter font.

Critical CSS tokens in `:root`:
```css
--cream: #f5f2ed;   --cream2: #eae6df;   --white: #ffffff;
--border: #e5e0d8;  --text: #1a1a1a;     --muted: #999;
--sage: #6b8f6b;    --amber: #b87d0a;    --red: #b84040;
```

---

## Roadmap (prioritized)

### Near-term
- [ ] **Options paper-trading simulator** — [detailed spec discussed]. Starting balance $10k, 1-2 buys/week from top-5, BSM pricing with ~35% IV assumption, new Portfolio tab on PWA, mark-to-market nightly alongside run.py. Non-trivial (~400 lines). User wants to wait a month of live track-record data before starting.
- [ ] **Score rework via regression** on `picks_log.csv` — fit empirical weights of `rsi_distance`, `pct_off_high`, `macd_age`, `vol_ratio` against 180d forward return. Fix the 60-89 dead-zone in current score.

### Medium-term
- [ ] **Sector / regime overlay.** Gate trend signals by breadth > 60. Gate fallen angels by sector-relative performance.
- [ ] **Entry confirmation filter** — require higher-swing-low structure rather than bouncing off any 20d low.

### Long-term
- [ ] **Fundamentals overlay.** Earnings growth / ROE / debt / FCF from Yahoo quoteSummary. Transforms "technical fallen angel" into real value-investor screener. ~200+ lines, rate-limit work.
- [ ] **Live options execution (Stage 3+).** Broker API integration (Tastytrade / Alpaca Options). Only after paper simulator proves edge. Pair-programmed, not pushed blind.
- [ ] **`signal_history` implementation** for the History tab's "Signal Activity" chart (currently placeholder empty-state).

---

## Things we know but haven't coded

1. **GitHub Actions cron lag.** Scheduled `30 21 * * 1-5` but typically fires 22:10-22:20 due to runner queue. Expected, not a bug.
2. **First scheduled run after workflow changes can skip.** Manually trigger once after a workflow edit to confirm secrets are wired.
3. **Yahoo occasionally 429s.** Current 20-worker parallel fetch tolerates a few failed tickers (just missing from `frames`). If failures climb above ~10%, reduce `YAHOO_WORKERS`.
4. **`api/screen.py` and `screener/run.py` are kept in sync manually.** If one logic changes, apply to both or the fallback endpoint will drift. `screen.py` isn't actively used but shouldn't be allowed to bit-rot.
5. **Backtest cache.** `screener/.backtest_cache.pkl` (gitignored) stores downloaded frames + SPY for 24h so tweak-and-rerun cycles take ~1min instead of ~18min. Set `FRESH_DOWNLOAD=1` to force refetch.

---

## Voice / working preferences

Owen favours:
- Direct, honest assessments — flag what isn't working as readily as what is
- Small, committed, tested increments. `git add -A && git commit && git push` each substantive change
- Clear reasoning in commit messages about *why*, not just *what*
- Mobile-first layouts (primary usage is mobile PWA)
- No mock data — real Edge Config-driven everywhere
- Prefer editing existing code over creating new files
- Surface trade-offs before shipping; avoid over-building hidden complexity

---

_Last meaningful update: 2026-04-23 — after backtest filter tightening, 4y validation run, Backtest Performance panel, Live Track Record panel. Next session likely starts with options paper-trading simulator after ~1 month of live-tracking data accrues._
