import json
import os
import requests
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Use the auto-injected EDGE_CONFIG connection string
        edge_config_url = os.environ.get("EDGE_CONFIG")

        if not edge_config_url:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "EDGE_CONFIG not set"}).encode())
            return

        # Read screener_results key from Edge Config
        url = f"{edge_config_url}/item/screener_results"
        r = requests.get(url, timeout=10)
        data = r.json()

        response = json.dumps(data).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(response)
