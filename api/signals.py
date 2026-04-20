import json
import os
import requests
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        edge_config_id = os.environ.get("EDGE_CONFIG_ID")
        api_token = os.environ.get("VERCEL_API_TOKEN")

        url = f"https://api.vercel.com/v1/edge-config/{edge_config_id}/item/screener_results"
        headers = {
            "Authorization": f"Bearer {api_token}"
        }

        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()

        response = json.dumps(data).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(response)
