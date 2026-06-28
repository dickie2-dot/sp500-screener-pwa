import json
import os
import requests
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        edge_config = os.environ.get("EDGE_CONFIG")

        if not edge_config:
            self._error("EDGE_CONFIG not set")
            return

        # EDGE_CONFIG is a full URL with token:
        # https://edge-config.vercel.com/ecfg_xxx?token=yyy
        base = edge_config.split("?")[0]
        token = edge_config.split("token=")[1]

        # Read the blob URL pointer — a tiny string, well under the Hobby 8KB limit
        r = requests.get(f"{base}/item/screener_blob_url", params={"token": token}, timeout=10)
        if r.status_code != 200:
            self._error(f"edge config read failed: {r.status_code}")
            return

        blob_url = r.json()
        if not blob_url or not isinstance(blob_url, str):
            self._error("screener_blob_url not set — run the daily screener first")
            return

        # Fetch actual data from Vercel Blob (public URL, no auth needed)
        br = requests.get(blob_url, timeout=15)
        data = br.json()

        response = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(response)

    def _error(self, msg):
        body = json.dumps({"error": msg}).encode()
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
