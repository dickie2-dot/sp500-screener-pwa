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
    print("Downloading data via Tiingo...")
    api_token = os.environ.get("TIINGO_API_TOKEN")
    headers = {"Content-Type": "application/json", "Authorization": f"Token {api_token}"}
    frames = {}
    end = datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.utcnow() - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    for i, ticker in enumerate(tickers):
        try:
            url = f"https://api.tiingo.com/tiingo/daily/{ticker}/prices?startDate={start}&endDate={end}&resampleFreq=daily"
            r = requests.get(url, headers=headers, timeout=10)
            data = r.json()
            if not isinstance(data, list) or len(data) == 0:
                continue
            df = pd.DataFrame(data)
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
            df = df.set_index("date")[["close", "volume"]].rename(columns={"close": "Close", "volume": "Volume"})
            df = df.dropna()
            if not df.empty:
                frames[ticker] = df
        except Exception as e:
            print(f"[WARN] {ticker}: {e}")
        if i % 50 == 0 and i > 0:
            print(f"  ...{i}/{len(tickers)} done")
            time.sleep(0.5)
    print(f"Download complete. Got {len(frames)} tickers.")
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
