import json
import requests
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


def compute_wma(closes, period):
    result = []
    weights = list(range(1, period + 1))
    sum_w = sum(weights)
    for i in range(len(closes)):
        if i < period - 1:
            result.append(None)
        else:
            window = closes[i - period + 1:i + 1]
            val = sum(w * v for w, v in zip(weights, window)) / sum_w
            result.append(round(val, 2))
    return result


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        ticker = params.get('ticker', ['AAPL'])[0].upper()

        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2y"
        headers = {"User-Agent": "Mozilla/5.0"}

        try:
            r = requests.get(url, headers=headers, timeout=10)
            data = r.json()
            result = data["chart"]["result"][0]
            closes_raw = result["indicators"]["quote"][0]["close"]
            timestamps_raw = result["timestamp"]

            # Clean nulls keeping index alignment
            closes = []
            timestamps = []
            for c, t in zip(closes_raw, timestamps_raw):
                if c is not None:
                    closes.append(round(c, 2))
                    timestamps.append(t)

            # Compute WMAs on full year of data
            wma20  = compute_wma(closes, 20)
            wma50  = compute_wma(closes, 50)
            wma200 = compute_wma(closes, 200)

            # Return last 90 days for display
            n = 90
            payload = {
                "ticker": ticker,
                "last_price": closes[-1] if closes else None,
                "closes": closes[-n:],
                "timestamps": timestamps[-n:],
                "wma20": wma20[-n:],
                "wma50": wma50[-n:],
                "wma200": wma200[-n:]
            }

            response = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(response)

        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
