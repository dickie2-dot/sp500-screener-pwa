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
    frames = {}
    headers = {"User-Agent": "Mozilla/5.0"}
    for i, ticker in enumerate(tickers):
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1y"
            r = requests.get(url, headers=headers, timeout=10)
            data = r.json()
            result = data["chart"]["result"][0]
            timestamps = result["timestamp"]
            closes = result["indicators"]["quote"][0]["close"]
            volumes = result["indicators"]["quote"][0]["volume"]
            dates = pd.to_datetime(timestamps, unit="s", utc=True).tz_convert("America/New_York").normalize()
            df = pd.DataFrame({"Close": closes, "Volume": volumes}, index=dates)
            df = df.dropna()
            if not df.empty:
                frames[ticker] = df
        except Exception:
            pass
        if i % 50 == 0 and i > 0:
            print(f"  ...{i}/{len(tickers)} done")
            time.sleep(2)
    print(f"Download complete. Got {len(frames)} tickers.")
    return frames


def compute_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def detect_swing_low(close, deviation_pct=0.05, lookback=20):
    if len(close) < lookback + 1:
        return False
    recent = close.iloc[-(lookback + 1):]
    local_min_val = recent.min()
    local_min_idx = recent.idxmin()
    prior = close.loc[:local_min_idx].iloc[:-1]
    if prior.empty:
        return False
    local_peak = prior.max()
    significant_drop = (local_peak - local_min_val) / local_peak >= deviation_pct
    price_recovering = close.iloc[-1] > local_min_val
    return significant_drop and price_recovering


def volume_divergence_bullish(close, volume, lookback=5):
    c = close.iloc[-lookback:]
    v = volume.iloc[-lookback:]
    changes = c.diff().iloc[1:]
    vols = v.iloc[1:]
    up_vol = vols[changes > 0].mean()
    down_vol = vols[changes <= 0].mean()
    if pd.isna(up_vol) or pd.isna(down_vol) or down_vol == 0:
        return False
    return up_vol > down_vol


def screen_ticker(ticker, frames):
    try:
        df = frames.get(ticker)
        if df is None or len(df) < 210:
            return None

        close = df["Close"].squeeze()
        volume = df["Volume"].squeeze()

        sma20  = close.rolling(20).mean()
        sma50  = close.rolling(50).mean()
        sma200 = close.rolling(200).mean()

        price_today  = float(close.iloc[-1])
        sma20_today  = float(sma20.iloc[-1])
        sma50_today  = float(sma50.iloc[-1])
        sma200_today = float(sma200.iloc[-1])

        if any(pd.isna([price_today, sma20_today, sma50_today, sma200_today])):
            return None

        avg_vol_20 = float(volume.iloc[-21:-1].mean())
        if avg_vol_20 < 500_000:
            return None

        sma200_20d_ago = float(sma200.iloc[-21])
        sma200_rising = sma200_today > sma200_20d_ago
        week52_high = float(close.rolling(252).max().iloc[-1])

        rsi = compute_rsi(close)
        rsi_today         = float(rsi.iloc[-1])
        rsi_last_5        = rsi.iloc[-6:-1]
        rsi_was_below_30  = bool((rsi_last_5 < 30).any())
        rsi_crossed_above = rsi_was_below_30 and rsi_today > 30

        vol_today = float(volume.iloc[-1])
        vol_surge = vol_today > 1.5 * avg_vol_20

        if (price_today > sma200_today and
                sma50_today > sma200_today and
                sma200_rising and
                price_today > sma50_today and
                rsi_crossed_above and
                rsi_today < 70 and
                vol_surge and
                price_today <= week52_high * 1.05):
            return ("trend", ticker)

        cross_up = (
            float(sma20.iloc[-1]) > float(sma50.iloc[-1]) and
            float(sma20.iloc[-6]) <= float(sma50.iloc[-6])
        )
        swing_low_found = detect_swing_low(close, deviation_pct=0.05, lookback=20)
        deep_drawdown   = price_today <= week52_high * 0.80
        vol_div_bullish = volume_divergence_bullish(close, volume, lookback=5)

        if (price_today < sma200_today and
                sma200_rising and
                cross_up and
                swing_low_found and
                deep_drawdown and
                vol_div_bullish):
            return ("turnaround", ticker)

    except Exception:
        pass

    return None


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
    print(r.text)
    return r.status_code


def main():
    tickers = get_sp500_tickers()
    frames  = download_data(tickers)

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
    print(f"Turnaround hits: {len(turnaround_hits)}")

    data = {
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "trend": sorted(trend_hits),
        "turnaround": sorted(turnaround_hits),
        "trend_count": len(trend_hits),
        "turnaround_count": len(turnaround_hits)
    }

    push_to_edge_config(data)


if __name__ == "__main__":
    main()
