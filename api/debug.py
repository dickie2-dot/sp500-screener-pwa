import json
import os
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        edge_config = os.environ.get("EDGE_CONFIG", "NOT SET")

        # Show full URL - it only contains a read token, not sensitive account credentials
        response = json.dumps({
            "EDGE_CONFIG": edge_config
        }).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(response)
