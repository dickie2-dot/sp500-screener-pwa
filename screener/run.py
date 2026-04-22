import re
import time
import json
import os
import requests
import pandas as pd
import warnings
from datetime import datetime
warnings.filterwarnings("ignore")


def get_sp500_tickers():
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    tickers = re.findall(r'href="https://(?:www\.nyse\.com/quote|www\.nasdaq\.com/market-activity/stocks)/[^"]+">([A-Z\-]{1,5})</a>', r.text)
    print(f"Fetched {len(tickers)} tickers")
    return tickers


def download_data(tickers):
    """Polygon.io download with configurable per-call pacing and 429 backoff.

    Env vars:
      POLYGON_API_KEY          required
      POLYGON_RATE_DELAY       seconds between calls (default 13 = safe for free tier 5/min)
      POLYGON_MAX_RETRIES      retries per 429 (default 5)
    """
    print("Downloading data via Polygon.io (rate-limit friendly)...")
    api_key = os.environ.get("POLYGON_API_KEY")
    delay = float(os.environ.get("POLYGON_RATE_DELAY", "13"))
    max_retries = int(os.environ.get("POLYGON_MAX_RETRIES", "5"))

    frames = {}
    end = datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.utcnow() - pd.Timedelta(days=400)).strftime("%Y-%m-%d")

    for idx, ticker in enumerate(tickers, 1):
        url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
        params = {"adjusted": "true", "sort": "asc", "limit": 500, "apiKey": api_key}

        # Retry loop with exponential backoff on 429
        backoff = delay
        for attempt in range(max_retries + 1):
            try:
                r = requests.get(url, params=params, timeout=20)
                if r.status_code == 429:
                    wait = backoff * (2 ** attempt)
                    print(f"  [429] {ticker} — backing off {wait:.0f}s (attempt {attempt+1}/{max_retries})")
                    time.sleep(wait)
                    continue
                data = r.json()
                if data.get("status") not in ("OK", "DELAYED") or not data.get("results"):
                    break
                results = data["results"]
                dates = pd.to_datetime([x["t"] for x in results], unit="ms", utc=True).tz_convert("America/New_York").normalize()
                closes = [x["c"] for x in results]
                volumes = [x["v"] for x in results]
                df = pd.DataFrame({"Close": closes, "Volume": volumes}, index=dates).dropna()
                if not df.empty:
                    frames[ticker] = df
                break
            except Exception as e:
                print(f"  [WARN] {ticker}: {e}")
                break

        if idx % 50 == 0:
            print(f"  ...{idx}/{len(tickers)} ({len(frames)} frames so far)")

        # Polite inter-call delay (skip after the last ticker)
        if idx < len(tickers):
            time.sleep(delay)

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
        if df is None or len(df) < 210:
            return None

        close = df["Close"].squeeze()
        volume = df["Volume"].squeeze()

        wma20  = compute_wma(close, 20)
        wma50  = compute_wma(close, 50)
        wma200 = compute_wma(close, 200)

        price_today  = float(close.iloc[-1])
        wma20_today  = float(wma20.iloc[-1])
        wma50_today  = float(wma50.iloc[-1])
        wma200_today = float(wma200.iloc[-1])

        if any(pd.isna([price_today, wma20_today, wma50_today, wma200_today])):
            return None

        avg_vol_20 = float(volume.iloc[-21:-1].mean())
        if avg_vol_20 < 500_000:
            return None

        week52_high = float(close.expanding().max().iloc[-1])
        rsi = compute_rsi(close)
        rsi_today = float(rsi.iloc[-1])
        vol_today = float(volume.iloc[-1])

        # LIST 1 - TREND RADAR
        wma200_20d_ago    = float(wma200.iloc[-21])
        wma200_rising     = wma200_today > wma200_20d_ago
        rsi_last_5        = rsi.iloc[-6:-1]
        rsi_was_below_30  = bool((rsi_last_5 < 30).any())
        rsi_crossed_above = rsi_was_below_30 and rsi_today > 30
        vol_surge         = vol_today > 1.5 * avg_vol_20

        if (price_today > wma200_today and
                wma50_today > wma200_today and
                wma200_rising and
                price_today > wma50_today and
                rsi_crossed_above and
                rsi_today < 70 and
                vol_surge and
                price_today <= week52_high * 1.05):
            return ("trend", ticker)

        # LIST 2 - QUALITY FALLEN ANGELS
        pct_off_high  = (week52_high - price_today) / week52_high
        below_wma200  = price_today < wma200_today
        deep_enough   = pct_off_high >= 0.25
        not_destroyed = pct_off_high <= 0.75
        macd_cross    = macd_crossed_up_recently(close, lookback=20)
        vol_div       = volume_divergence_bullish(close, volume, lookback=5)
        rsi_recovery  = 30 <= rsi_today <= 55
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
        rsi = compute_rsi(close)
        rsi_today = float(rsi.iloc[-1])
        price = float(close.iloc[-1])
        week52_high = float(close.expanding().max().iloc[-1])
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

    # Market breadth - % of stocks above their 200 WMA
    above_200 = 0
    risers = []
    fallers = []
    for ticker, df in frames.items():
        try:
            close = df["Close"].squeeze()
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

    today_picks = []
    for t in top5:
        df = frames.get(t)
        if df is not None:
            try:
                entry_price = float(df["Close"].squeeze().iloc[-1])
                today_picks.append({
                    "ticker": t,
                    "entry_price": round(entry_price, 2),
                    "score": scores.get(t, 0)
                })
            except Exception:
                pass

    # Drop any prior entry for today (rerun-safe), append today, keep last 7
    new_history = [h for h in prior_history if h.get("date") != today_str]
    new_history.append({"date": today_str, "picks": today_picks})
    new_history = new_history[-7:]

    data = {
        "date": today_str,
        "trend": sorted(trend_hits),
        "turnaround": sorted(turnaround_hits),
        "trend_count": len(trend_hits),
        "turnaround_count": len(turnaround_hits),
        "breadth_pct": breadth_pct,
        "top_risers": top_risers,
        "top_fallers": top_fallers,
        "scores": scores,
        "top5": top5,
        "top5_history": new_history
    }

    push_to_edge_config(data)


if __name__ == "__main__":
    main()
