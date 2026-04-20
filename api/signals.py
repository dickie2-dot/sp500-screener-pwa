import json
import os
import requests
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        edge_config = os.environ.get("EDGE_CONFIG")

        if not edge_config:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "EDGE_CONFIG not set"}).encode())
            return

        # EDGE_CONFIG is already a full URL with token e.g.
        # https://edge-config.vercel.com/ecfg_xxx?token=yyy
        # Append /item/screener_results to read the key
        base = edge_config.split("?")[0]
        token = edge_config.split("token=")[1]

        url = f"{base}/item/screener_results"
        r = requests.get(url, params={"token": token}, timeout=10)
        data = r.json()

        response = json.dumps(data).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(response)
