"""
Signal 500 — historical backtest.

For every past trading day in the evaluation window, simulate the screener
as-if it ran that evening: compute hits, score them, pick the top 5. Then
fast-forward 5 / 20 / 60 trading days and record realised returns.

Outputs:
  - picks_log.csv — every pick with date, ticker, type, score, entry price, forward returns
  - Console report: win rates, avg returns, per-score-bucket breakdown,
    per-category breakdown, SPY baseline comparison.

Usage:
    python screener/backtest.py              # 2-year window, 5y fetch
    BACKTEST_YEARS=3 python screener/backtest.py

No Edge Config writes — purely local analysis.
"""
import os
import sys
import csv
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import warnings
warnings.filterwarnings("ignore")

# Reuse the indicator + ticker-fetch functions from run.py
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)
from run import (  # noqa: E402
    get_sp500_tickers,
    compute_wma, compute_rsi, compute_macd,
    volume_divergence_bullish,
)


# ─────────────────────────────────────────────────────────────
# Data download (5y — backtest needs long history)
# ─────────────────────────────────────────────────────────────
def _fetch_one(ticker, yrange):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range={yrange}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = r.json()
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
        volumes = result["indicators"]["quote"][0]["volume"]
        dates = pd.to_datetime(timestamps, unit="s", utc=True).tz_convert("America/New_York").normalize()
        df = pd.DataFrame({"Close": closes, "Volume": volumes}, index=dates).dropna()
        return ticker, (df if not df.empty else None)
    except Exception:
        return ticker, None


def download_all(tickers, yrange="5y", workers=25):
    print(f"Downloading {len(tickers)} tickers (range={yrange}, {workers} workers)...")
    frames = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_fetch_one, t, yrange) for t in tickers]
        for fut in as_completed(futures):
            t, df = fut.result()
            done += 1
            if df is not None:
                frames[t] = df
            if done % 50 == 0:
                print(f"  ...{done}/{len(tickers)} ({len(frames)} frames)")
    print(f"Got {len(frames)}/{len(tickers)} tickers.")
    return frames


# ─────────────────────────────────────────────────────────────
# Pre-compute indicators once per ticker (much faster than per-day)
# ─────────────────────────────────────────────────────────────
def precompute(df):
    close, volume = df["Close"], df["Volume"]
    df["wma20"]  = compute_wma(close, 20)
    df["wma50"]  = compute_wma(close, 50)
    df["wma200"] = compute_wma(close, 200)
    df["rsi"]    = compute_rsi(close)
    macd_line, signal_line = compute_macd(close)
    df["macd"]   = macd_line
    df["signal"] = signal_line
    # Prior-20-day avg volume (exclude today) to match run.py
    df["avg_vol_20"]        = volume.shift(1).rolling(20).mean()
    df["avg_dollar_vol_20"] = (close * volume).shift(1).rolling(20).mean()
    df["week52_high"]       = close.rolling(252).max()


# ─────────────────────────────────────────────────────────────
# Screen at a past position (mirrors run.py's screen_ticker exactly)
# ─────────────────────────────────────────────────────────────
def screen_at(df, i):
    """Return (hit_type, score) or None — as-if today were bar index i."""
    if i < 260 or i >= len(df):
        return None
    row = df.iloc[i]
    week52_high = row["week52_high"]
    wma200, wma50, wma20 = row["wma200"], row["wma50"], row["wma20"]
    if pd.isna(week52_high) or week52_high <= 0 or pd.isna(wma200):
        return None
    if pd.isna(row["avg_dollar_vol_20"]) or row["avg_dollar_vol_20"] < 10_000_000:
        return None

    price = row["Close"]
    rsi   = row["rsi"]
    vol   = row["Volume"]
    avg_vol_20 = row["avg_vol_20"]
    if pd.isna(rsi) or pd.isna(avg_vol_20):
        return None

    # ── TREND ──
    wma200_20d_ago = df["wma200"].iloc[i - 20]
    wma200_rising  = wma200 > wma200_20d_ago
    rsi_last_10    = df["rsi"].iloc[i - 10:i]
    rsi_pulled_back = bool((rsi_last_10 < 40).any())
    rsi_recovered   = rsi > 45
    vol_surge       = vol > 1.5 * avg_vol_20

    if (price > wma200 and wma50 > wma200 and wma200_rising and
            price > wma50 and rsi_pulled_back and rsi_recovered and
            rsi < 70 and vol_surge):
        rsi_score = max(0, 50 - abs(rsi - 55) * 2)
        vol_ratio = vol / avg_vol_20 if avg_vol_20 else 1
        vol_score = min(50, max(0, (vol_ratio - 1.0) * 25))
        score = int(max(0, min(100, round(rsi_score + vol_score))))
        return ("trend", score)

    # ── FALLEN ANGEL ── (tightened 2026-04-23; keep in sync with run.py)
    pct_off_high = (week52_high - price) / week52_high
    if not (0.25 <= pct_off_high <= 0.45):
        return None
    if price >= wma200:
        return None
    if not (35 <= rsi <= 45):
        return None
    low_20d = df["Close"].iloc[i - 20:i].min()
    if price <= low_20d * 1.05:
        return None

    macd_s, sig_s = df["macd"], df["signal"]
    macd_cross = False
    for k in range(1, 21):
        if i - k - 1 < 0:
            break
        if (macd_s.iloc[i - k] > sig_s.iloc[i - k] and
                macd_s.iloc[i - k - 1] <= sig_s.iloc[i - k - 1]):
            macd_cross = True
            break
    if not macd_cross:
        return None

    close_hist  = df["Close"].iloc[:i + 1]
    volume_hist = df["Volume"].iloc[:i + 1]
    if not volume_divergence_bullish(close_hist, volume_hist, lookback=5):
        return None

    rsi_score   = max(0, 50 - abs(rsi - 40) * 2)
    depth_score = max(0, 50 - max(0, pct_off_high * 100 - 35) * 1.5)
    score = int(max(0, min(100, round(rsi_score + depth_score))))
    return ("turnaround", score)


# ─────────────────────────────────────────────────────────────
# Backtest loop
# ─────────────────────────────────────────────────────────────
def run_backtest(years=2):
    tickers = get_sp500_tickers()
    # 10y of history so BACKTEST_YEARS up to ~9 has indicator-warmup headroom.
    # (Yahoo caps bars at ~2500 for 10y; more than enough for 260d warmup +
    # iteration window + 60d forward buffer.)
    frames = download_all(tickers, yrange="10y")
    spy_yrange = "10y"

    print("Precomputing indicators...")
    for t in list(frames.keys()):
        if len(frames[t]) < 320:
            del frames[t]
            continue
        precompute(frames[t])
    print(f"{len(frames)} tickers usable.")

    # SPY for baseline — fetch separately (not an S&P component)
    spy_ticker, spy_df = _fetch_one("SPY", spy_yrange)
    if spy_df is not None:
        print(f"SPY baseline: {len(spy_df)} bars.")

    # Master calendar: longest series we have
    ref_ticker = max(frames, key=lambda t: len(frames[t]))
    all_dates = frames[ref_ticker].index

    # Iteration range: leave 60 bars at the end for forward-return buffer
    horizons = [5, 20, 60]
    end_i   = len(all_dates) - max(horizons)
    start_i = max(260, end_i - years * 252)
    print(f"Backtesting {all_dates[start_i].date()} → {all_dates[end_i - 1].date()} "
          f"({end_i - start_i} trading days, {years}y window).")

    # Build quick per-ticker date→position index
    pos_of = {t: {d: i for i, d in enumerate(df.index)} for t, df in frames.items()}
    spy_pos = {d: i for i, d in enumerate(spy_df.index)} if spy_df is not None else {}

    picks_log = []
    processed = 0
    for mi in range(start_i, end_i):
        master_date = all_dates[mi]

        day_hits = []
        for t, df in frames.items():
            pos = pos_of[t].get(master_date)
            if pos is None:
                continue
            result = screen_at(df, pos)
            if result is None:
                continue
            day_hits.append((t, result[0], result[1]))

        if not day_hits:
            processed += 1
            continue

        day_hits.sort(key=lambda x: -x[2])
        top5 = day_hits[:5]

        # SPY baseline at this date
        spy_entry = None
        if spy_df is not None and master_date in spy_pos:
            spy_entry = float(spy_df["Close"].iloc[spy_pos[master_date]])

        for t, tp, sc in top5:
            df = frames[t]
            entry_pos = pos_of[t][master_date]
            entry_px  = float(df["Close"].iloc[entry_pos])
            rec = {
                "date": str(master_date.date()),
                "ticker": t,
                "type": tp,
                "score": sc,
                "entry_px": round(entry_px, 2),
            }
            for h in horizons:
                future_pos = entry_pos + h
                if future_pos < len(df):
                    exit_px = float(df["Close"].iloc[future_pos])
                    rec[f"r{h}"] = round((exit_px - entry_px) / entry_px * 100, 2)
                else:
                    rec[f"r{h}"] = None
                # SPY same-horizon baseline
                if spy_entry is not None and master_date in spy_pos:
                    spy_future = spy_pos[master_date] + h
                    if spy_future < len(spy_df):
                        spy_exit = float(spy_df["Close"].iloc[spy_future])
                        rec[f"spy_r{h}"] = round((spy_exit - spy_entry) / spy_entry * 100, 2)
                    else:
                        rec[f"spy_r{h}"] = None
            picks_log.append(rec)

        processed += 1
        if processed % 50 == 0:
            print(f"  {master_date.date()} — {len(day_hits):>3} hits | {len(picks_log)} picks logged")

    print(f"\nTotal picks logged: {len(picks_log)}")
    return picks_log, horizons


# ─────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────
def report(picks_log, horizons):
    if not picks_log:
        print("No picks to analyse.")
        return

    df = pd.DataFrame(picks_log)

    def stats(subset, label):
        if subset.empty:
            return
        print(f"\n── {label}  (n={len(subset)}) ──")
        print(f"  {'Horizon':<10} {'Avg %':>8} {'Win %':>8} {'SPY avg':>10} {'Excess':>10}")
        for h in horizons:
            col = f"r{h}"
            spy = f"spy_r{h}"
            ser = subset[col].dropna()
            if ser.empty:
                continue
            avg = ser.mean()
            win = (ser > 0).mean() * 100
            spy_ser = subset[spy].dropna()
            spy_avg = spy_ser.mean() if not spy_ser.empty else float("nan")
            excess  = avg - spy_avg if not pd.isna(spy_avg) else float("nan")
            print(f"  {h:>2}d        {avg:>7.2f}% {win:>7.1f}% {spy_avg:>9.2f}% {excess:>9.2f}%")

    stats(df, "ALL PICKS")
    stats(df[df["type"] == "turnaround"], "FALLEN ANGELS")
    stats(df[df["type"] == "trend"],      "TREND RADAR")

    print("\n── BY SCORE BUCKET ──")
    print(f"  {'Bucket':<10} {'n':>6} " + " ".join(f"{f'r{h}':>8}" for h in horizons) +
          " " + " ".join(f"{f'win{h}':>7}" for h in horizons))
    for lo in range(40, 101, 10):
        hi = lo + 10
        sub = df[(df["score"] >= lo) & (df["score"] < hi)]
        if sub.empty:
            continue
        line = f"  {lo}-{hi-1:<5} {len(sub):>6} "
        for h in horizons:
            ser = sub[f"r{h}"].dropna()
            line += f"{ser.mean():>7.2f}% " if not ser.empty else f"{'n/a':>8}"
        for h in horizons:
            ser = sub[f"r{h}"].dropna()
            wr = (ser > 0).mean() * 100 if not ser.empty else float("nan")
            line += f"{wr:>6.1f}% " if not pd.isna(wr) else f"{'n/a':>7}"
        print(line)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "picks_log.csv")
    df.to_csv(out, index=False)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    years = int(os.environ.get("BACKTEST_YEARS", "2"))
    picks_log, horizons = run_backtest(years=years)
    report(picks_log, horizons)
