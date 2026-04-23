import re
import time
import json
import os
import sys
import requests
import pandas as pd
import warnings
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
warnings.filterwarnings("ignore")


def load_env_file(path):
    """Dependency-free .env loader — sets any KEY=VALUE lines into os.environ
    (without clobbering already-set values)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        return True
    except FileNotFoundError:
        return False


# Load .env from a few candidate locations so the .bat's CWD doesn't matter
_here = os.path.dirname(os.path.abspath(__file__))
for _candidate in [
    os.environ.get("SP500_ENV_FILE", ""),
    os.path.join(os.getcwd(), ".env"),
    os.path.join(_here, ".env"),
    os.path.join(_here, "..", ".env"),
    r"C:\Users\owen7\Desktop\sp500-screener\.env",
]:
    if _candidate and load_env_file(_candidate):
        print(f"[env] loaded {_candidate}")
        break


def trim_partial_session(close, volume):
    """If today's bar is mid-session (NY time, pre 16:00), drop it so indicators
    use only complete daily bars. Makes midday manual runs consistent with
    overnight runs."""
    try:
        ny_now = datetime.now(ZoneInfo("America/New_York"))
        last_ts = close.index[-1]
        # Normalize: last_ts may already be midnight-normalized daily date
        last_date = last_ts.date() if hasattr(last_ts, "date") else last_ts
        if last_date == ny_now.date() and ny_now.hour < 16:
            close = close.iloc[:-1]
            volume = volume.iloc[:-1]
    except Exception:
        pass
    return close, volume


def get_sp500_tickers():
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    tickers = re.findall(r'href="https://(?:www\.nyse\.com/quote|www\.nasdaq\.com/market-activity/stocks)/[^"]+">([A-Z\-]{1,5})</a>', r.text)
    print(f"Fetched {len(tickers)} tickers")
    return tickers


def _fetch_one_yahoo(ticker):
    """Fetch a single ticker from Yahoo Finance — 1 year daily bars."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2y"
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


def download_data(tickers):
    """Parallel Yahoo Finance download. Env:
         YAHOO_WORKERS  number of concurrent requests (default 20)
    """
    workers = int(os.environ.get("YAHOO_WORKERS", "20"))
    print(f"Downloading {len(tickers)} tickers via Yahoo Finance ({workers} workers)...")

    frames = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_fetch_one_yahoo, t) for t in tickers]
        for fut in as_completed(futures):
            t, df = fut.result()
            done += 1
            if df is not None:
                frames[t] = df
            if done % 50 == 0:
                print(f"  ...{done}/{len(tickers)} ({len(frames)} frames so far)")

    print(f"Download complete. Got {len(frames)}/{len(tickers)} tickers.")
    return frames


def compute_wma(series, period):
    weights = pd.Series(range(1, period + 1))
    return series.rolling(period).apply(lambda x: (x * weights).sum() / weights.sum(), raw=True)


def compute_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def compute_macd(series):
    ema12 = series.ewm(span=12, min_periods=12).mean()
    ema26 = series.ewm(span=26, min_periods=26).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, min_periods=9).mean()
    return macd_line, signal_line


def macd_crossed_up_recently(series, lookback=20):
    macd_line, signal_line = compute_macd(series)
    for i in range(1, lookback + 1):
        if (macd_line.iloc[-i] > signal_line.iloc[-i] and
                macd_line.iloc[-(i+1)] <= signal_line.iloc[-(i+1)]):
            return True
    return False


def volume_divergence_bullish(close, volume, lookback=5):
    c = close.iloc[-lookback:]
    v = volume.iloc[-lookback:]
    changes = c.diff().iloc[1:]
    vols = v.iloc[1:]
    up_vol = vols[changes > 0].mean()
    down_vol = vols[changes <= 0].mean()
    if pd.isna(down_vol):
        return True
    if pd.isna(up_vol) or down_vol == 0:
        return False
    return up_vol > down_vol


def screen_ticker(ticker, frames):
    try:
        df = frames.get(ticker)
        if df is None or len(df) < 260:   # need ~1y of complete bars for rolling(252)
            return None

        close = df["Close"].squeeze()
        volume = df["Volume"].squeeze()

        # Guard: if midday run, drop the partial bar
        close, volume = trim_partial_session(close, volume)
        if len(close) < 260:
            return None

        wma20  = compute_wma(close, 20)
        wma50  = compute_wma(close, 50)
        wma200 = compute_wma(close, 200)

        price_today  = float(close.iloc[-1])
        wma20_today  = float(wma20.iloc[-1])
        wma50_today  = float(wma50.iloc[-1])
        wma200_today = float(wma200.iloc[-1])

        if any(pd.isna([price_today, wma20_today, wma50_today, wma200_today])):
            return None

        # Dollar-volume liquidity filter (>= $10M avg daily traded value)
        avg_vol_20 = float(volume.iloc[-21:-1].mean())
        avg_dollar_vol_20 = float((close * volume).iloc[-21:-1].mean())
        if avg_dollar_vol_20 < 10_000_000:
            return None

        # FIX: true 52-week high (rolling 252), not all-time
        week52_high = float(close.rolling(252).max().iloc[-1])
        if pd.isna(week52_high) or week52_high <= 0:
            return None

        rsi = compute_rsi(close)
        rsi_today = float(rsi.iloc[-1])
        vol_today = float(volume.iloc[-1])

        # ── LIST 1 — TREND RADAR (buy-the-dip in an uptrend) ──
        wma200_20d_ago = float(wma200.iloc[-21])
        wma200_rising  = wma200_today > wma200_20d_ago

        # Softer pullback-within-uptrend: RSI touched <40 in last 10 days and
        # has recovered above 45 today. Replaces the old contradictory <30 rule.
        rsi_last_10      = rsi.iloc[-11:-1]
        rsi_pulled_back  = bool((rsi_last_10 < 40).any())
        rsi_recovered    = rsi_today > 45
        vol_surge        = vol_today > 1.5 * avg_vol_20

        if (price_today > wma200_today and
                wma50_today > wma200_today and
                wma200_rising and
                price_today > wma50_today and
                rsi_pulled_back and
                rsi_recovered and
                rsi_today < 70 and
                vol_surge):
            return ("trend", ticker)

        # ── LIST 2 — QUALITY FALLEN ANGELS ──
        # Tightened 2026-04-23 based on backtest: old 25-75% + RSI 30-55 net
        # underperformed SPY across 1786 picks. Backtest showed only the
        # narrow 'peak' setups (RSI ~40, 25-35% off high) earn alpha, so we
        # narrow the filter to roughly that region.
        pct_off_high  = (week52_high - price_today) / week52_high
        below_wma200  = price_today < wma200_today
        deep_enough   = pct_off_high >= 0.25
        not_destroyed = pct_off_high <= 0.45
        macd_cross    = macd_crossed_up_recently(close, lookback=20)
        vol_div       = volume_divergence_bullish(close, volume, lookback=5)
        rsi_recovery  = 35 <= rsi_today <= 45
        low_20d       = float(close.iloc[-21:-1].min())
        bouncing      = price_today > low_20d * 1.05

        if (below_wma200 and
                deep_enough and
                not_destroyed and
                macd_cross and
                vol_div and
                rsi_recovery and
                bouncing):
            return ("turnaround", ticker)

    except Exception:
        pass

    return None


def read_existing_from_edge_config():
    """Read current screener_results from Edge Config — returns dict or {} on failure."""
    edge_config_id = os.environ.get("EDGE_CONFIG_ID")
    api_token = os.environ.get("VERCEL_API_TOKEN")
    if not edge_config_id or not api_token:
        return {}
    url = f"https://api.vercel.com/v1/edge-config/{edge_config_id}/item/screener_results"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {api_token}"}, timeout=10)
        if r.status_code == 200:
            payload = r.json()
            # Vercel returns {"value": <actual-value>} for the /item endpoint
            val = payload.get("value", payload)
            return val if isinstance(val, dict) else {}
    except Exception as e:
        print(f"[WARN] read existing failed: {e}")
    return {}


def compute_score(ticker, df, hit_type):
    """Simple 0-100 score. Turnaround: reward RSI sweet-spot + proximity to high.
       Trend: reward RSI health + volume surge magnitude."""
    try:
        close = df["Close"].squeeze()
        volume = df["Volume"].squeeze()
        close, volume = trim_partial_session(close, volume)
        rsi = compute_rsi(close)
        rsi_today = float(rsi.iloc[-1])
        price = float(close.iloc[-1])
        week52_high = float(close.rolling(252).max().iloc[-1])
        if pd.isna(week52_high) or week52_high <= 0:
            return 0
        pct_off_high = (week52_high - price) / week52_high * 100
        avg_vol_20 = float(volume.iloc[-21:-1].mean())
        vol_today = float(volume.iloc[-1])
        vol_ratio = vol_today / avg_vol_20 if avg_vol_20 else 1

        if hit_type == "turnaround":
            # Peak at RSI ~40, sweet spot pct_off_high ~25-40
            rsi_score = max(0, 50 - abs(rsi_today - 40) * 2)
            depth_score = max(0, 50 - max(0, pct_off_high - 35) * 1.5)
            score = rsi_score + depth_score
        else:  # trend
            rsi_score = max(0, 50 - abs(rsi_today - 55) * 2)
            vol_score = min(50, max(0, (vol_ratio - 1.0) * 25))
            score = rsi_score + vol_score

        return int(max(0, min(100, round(score))))
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────
# Live performance log — advance-by-one-run bookkeeping
# ─────────────────────────────────────────────────────────────
PERF_HORIZONS = [5, 20, 60, 120, 180, 250]
PERF_LOG_CAP = 1500  # ~300 trading days × 5 picks — stays under Edge Config 512KB item limit


def _entry_pos_in_series(df, pick_date_str):
    """Find the row index in df whose date matches pick_date_str (YYYY-MM-DD).
    Returns None if not found. df.index is a tz-aware DatetimeIndex (NY, normalized)."""
    try:
        target = pd.Timestamp(pick_date_str).date()
        mask = df.index.date == target
        if not mask.any():
            return None
        matches = df.index[mask]
        return df.index.get_loc(matches[0])
    except Exception:
        return None


def update_performance_log(prior_log, today_picks, today_str, frames, spy_df):
    """Append today's picks (if not already present) and back-fill any newly-
    reached horizon returns for every entry in the log. Idempotent: reruns on
    the same day replace today's entries rather than duplicating them.
    """
    log = [e for e in prior_log if e.get("date") != today_str]

    # Seed today's picks (with null-filled return arrays)
    for p in today_picks:
        log.append({
            "date":        today_str,
            "ticker":      p["ticker"],
            "type":        p.get("type", "unknown"),
            "score":       p.get("score", 0),
            "entry_price": p["entry_price"],
            "r":     [None] * len(PERF_HORIZONS),  # realised stock return at each horizon
            "spy_r": [None] * len(PERF_HORIZONS),  # SPY baseline over same window
        })

    # Back-fill any newly-reached horizons for ALL entries (including older ones)
    filled = 0
    for entry in log:
        t = entry["ticker"]
        if t not in frames:
            continue
        df = frames[t]
        entry_idx = _entry_pos_in_series(df, entry["date"])
        if entry_idx is None:
            continue
        days_held = (len(df) - 1) - entry_idx
        entry_px  = entry["entry_price"]

        # SPY alignment for same pick date
        spy_entry_idx = _entry_pos_in_series(spy_df, entry["date"]) if spy_df is not None else None

        for hi, h in enumerate(PERF_HORIZONS):
            if entry["r"][hi] is not None:
                continue           # already filled on a previous run
            if days_held < h:
                continue           # horizon not yet reached
            future_idx = entry_idx + h
            if future_idx >= len(df):
                continue
            exit_px = float(df["Close"].iloc[future_idx])
            entry["r"][hi] = round((exit_px - entry_px) / entry_px * 100, 2)
            filled += 1

            # SPY baseline over the same window
            if spy_entry_idx is not None and spy_df is not None:
                spy_future_idx = spy_entry_idx + h
                if spy_future_idx < len(spy_df):
                    spy_entry_px = float(spy_df["Close"].iloc[spy_entry_idx])
                    spy_exit_px  = float(spy_df["Close"].iloc[spy_future_idx])
                    entry["spy_r"][hi] = round((spy_exit_px - spy_entry_px) / spy_entry_px * 100, 2)

    print(f"[perf] filled {filled} horizon cells this run")
    # Cap size (oldest-first); keeps Edge Config item under limit
    return log[-PERF_LOG_CAP:]


def push_to_edge_config(data):
    edge_config_id = os.environ.get("EDGE_CONFIG_ID")
    api_token = os.environ.get("VERCEL_API_TOKEN")
    url = f"https://api.vercel.com/v1/edge-config/{edge_config_id}/items"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "items": [
            {
                "operation": "upsert",
                "key": "screener_results",
                "value": data
            }
        ]
    }
    r = requests.patch(url, headers=headers, json=payload)
    print(f"Edge Config write status: {r.status_code}")
    return r.status_code


def main():
    tickers = get_sp500_tickers()
    frames  = download_data(tickers)

    # Diagnostic: check a known ticker
    if "NKE" in frames:
        df = frames["NKE"]
        close = df["Close"].squeeze()
        print(f"NKE sample - rows: {len(close)}, last price: {float(close.iloc[-1]):.2f}")
    else:
        print("NKE not in frames - possible download issue")

    trend_hits      = []
    turnaround_hits = []

    for ticker in tickers:
        result = screen_ticker(ticker, frames)
        if result:
            if result[0] == "trend":
                trend_hits.append(ticker)
            else:
                turnaround_hits.append(ticker)

    print(f"\nTrend hits: {len(trend_hits)}")
    print(f"Fallen Angel hits: {len(turnaround_hits)}")

    # Market breadth - % of stocks above their 200 WMA (uses trimmed bars)
    above_200 = 0
    risers = []
    fallers = []
    for ticker, df in frames.items():
        try:
            close = df["Close"].squeeze()
            volume = df["Volume"].squeeze()
            close, volume = trim_partial_session(close, volume)
            if len(close) < 200:
                continue
            wma200 = compute_wma(close, 200)
            price = float(close.iloc[-1])
            w200 = float(wma200.iloc[-1])
            if not pd.isna(w200):
                if price > w200:
                    above_200 += 1
                if len(close) >= 2:
                    prev = float(close.iloc[-2])
                    if prev > 0:
                        pct_change = (price - prev) / prev * 100
                        risers.append((pct_change, ticker))
                        fallers.append((pct_change, ticker))
        except:
            pass

    total_valid = len(frames)
    breadth_pct = round(above_200 / total_valid * 100, 1) if total_valid > 0 else 0
    risers.sort(reverse=True)
    fallers.sort()
    top_risers = [{"ticker": t, "change": round(c, 2)} for c, t in risers[:5]]
    top_fallers = [{"ticker": t, "change": round(c, 2)} for c, t in fallers[:5]]

    print(f"Breadth: {breadth_pct}% above WMA200")

    # ── Regime gate ──
    # Trend signals struggle in weak breadth; fallen angels become knife-catching
    # in a full breadth collapse. Thresholds: trend requires >=45%, fallen
    # angels require >=30%.
    gated_trend = []
    gated_turn  = []
    if breadth_pct >= 45:
        gated_trend = trend_hits
    else:
        print(f"[regime] Trend signals muted (breadth {breadth_pct}% < 45%)")
    if breadth_pct >= 30:
        gated_turn = turnaround_hits
    else:
        print(f"[regime] Fallen Angel signals muted (breadth {breadth_pct}% < 30%)")
    trend_hits = gated_trend
    turnaround_hits = gated_turn
    print(f"After regime gate — Trend: {len(trend_hits)}, Fallen Angels: {len(turnaround_hits)}")

    # ── Score every hit and pick top 5 overall ──
    scores = {}
    all_hits = [(t, "turnaround") for t in turnaround_hits] + [(t, "trend") for t in trend_hits]
    for ticker, hit_type in all_hits:
        df = frames.get(ticker)
        if df is not None:
            scores[ticker] = compute_score(ticker, df, hit_type)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top5 = [t for t, _ in ranked[:5]]

    # ── Rolling 7-day top-5 history (watchlist) ──
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    existing = read_existing_from_edge_config()
    prior_history = existing.get("top5_history", []) if isinstance(existing, dict) else []

    # Which category each top-5 pick came from (needed for the live log)
    turn_set, trend_set = set(turnaround_hits), set(trend_hits)
    def _type_of(ticker):
        if ticker in turn_set:  return "turnaround"
        if ticker in trend_set: return "trend"
        return "unknown"

    today_picks = []
    for t in top5:
        df = frames.get(t)
        if df is not None:
            try:
                c, _v = trim_partial_session(df["Close"].squeeze(), df["Volume"].squeeze())
                entry_price = float(c.iloc[-1])
                today_picks.append({
                    "ticker": t,
                    "entry_price": round(entry_price, 2),
                    "score": scores.get(t, 0),
                    "type":  _type_of(t),
                })
            except Exception:
                pass

    # Drop any prior entry for today (rerun-safe), append today, keep last 7
    new_history = [h for h in prior_history if h.get("date") != today_str]
    new_history.append({"date": today_str, "picks": today_picks})
    new_history = new_history[-7:]

    # ── All-time pick counts (survives the 7-day window) ──
    # Rebuilt from scratch each run so it stays consistent: scan every
    # top-5 that has ever appeared in performance_log (the append-only
    # record), then add today's top-5 to get a total count per ticker.
    prior_counts = existing.get("pick_counts", {}) if isinstance(existing, dict) else {}
    pick_counts = dict(prior_counts) if isinstance(prior_counts, dict) else {}
    # If we've never built counts, seed from performance_log entries that
    # predate today (guarantees consistent totals even after a schema bump).
    if not pick_counts:
        prior_log_for_counts = existing.get("performance_log", []) if isinstance(existing, dict) else []
        for e in prior_log_for_counts:
            t = e.get("ticker")
            if t:
                pick_counts[t] = pick_counts.get(t, 0) + 1
    # Rerun-safe: only increment once per ticker per day. Use today's entries
    # in the performance_log as the source of truth (they're written just
    # above and are idempotent on reruns).
    today_tickers = {e["ticker"] for e in performance_log if e.get("date") == today_str}
    already_counted_today = existing.get("pick_counts_date") == today_str if isinstance(existing, dict) else False
    if not already_counted_today:
        for t in today_tickers:
            pick_counts[t] = pick_counts.get(t, 0) + 1

    # ── Live performance log — advance by one run ──
    # Fetches SPY once for baseline, appends today's picks with empty returns,
    # back-fills any newly-reached horizon for every prior pick.
    prior_log = existing.get("performance_log", []) if isinstance(existing, dict) else []
    _, spy_df = _fetch_one_yahoo("SPY")
    if spy_df is not None:
        print(f"[perf] SPY baseline loaded ({len(spy_df)} bars)")
    performance_log = update_performance_log(
        prior_log, today_picks, today_str, frames, spy_df
    )
    print(f"[perf] log size: {len(performance_log)} picks "
          f"(earliest: {performance_log[0]['date'] if performance_log else 'n/a'})")

    # ── Options paper-trading portfolio — advance one day ──
    try:
        from portfolio import update_portfolio
        hit_types = {t: _type_of(t) for t in top5}
        prior_portfolio = existing.get("portfolio") if isinstance(existing, dict) else None
        portfolio = update_portfolio(
            prior_portfolio, top5, scores, hit_types, frames, today_str
        )
        print(f"[port] equity=${portfolio['equity']:.0f} cash=${portfolio['cash']:.0f} "
              f"open={len(portfolio['open_positions'])} closed={len(portfolio['closed_trades'])}")
    except Exception as e:
        print(f"[port] update failed: {e!r} — preserving prior state")
        portfolio = existing.get("portfolio") if isinstance(existing, dict) else None

    data = {
        "date": today_str,
        "last_scraped": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "trend": sorted(trend_hits),
        "turnaround": sorted(turnaround_hits),
        "trend_count": len(trend_hits),
        "turnaround_count": len(turnaround_hits),
        "breadth_pct": breadth_pct,
        "top_risers": top_risers,
        "top_fallers": top_fallers,
        "scores": scores,
        "top5": top5,
        "top5_history": new_history,
        "performance_log": performance_log,
        "pick_counts": pick_counts,
        "pick_counts_date": today_str,
        "portfolio": portfolio,
    }

    push_to_edge_config(data)


if __name__ == "__main__":
    main()
