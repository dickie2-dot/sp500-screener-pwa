import json
import os
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        edge_config = os.environ.get("EDGE_CONFIG", "NOT SET")
        edge_config_id = os.environ.get("EDGE_CONFIG_ID", "NOT SET")

        # Mask the token for safety but show the structure
        masked = edge_config[:50] + "..." if len(edge_config) > 50 else edge_config

        response = json.dumps({
            "EDGE_CONFIG_preview": masked,
            "EDGE_CONFIG_ID": edge_config_id
        }).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(response)
