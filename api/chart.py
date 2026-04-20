import json
import os
import requests
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        ticker = params.get('ticker', ['AAPL'])[0].upper()

        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=3mo"
        headers = {"User-Agent": "Mozilla/5.0"}

        try:
            r = requests.get(url, headers=headers, timeout=10)
            data = r.json()
            result = data["chart"]["result"][0]
            closes = result["indicators"]["quote"][0]["close"]
            timestamps = result["timestamp"]

            payload = {
                "closes": closes,
                "timestamps": timestamps
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
