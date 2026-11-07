"""Minimal health endpoint for simulated devices."""
import argparse
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

device_id = "sim-device"
device_type = "server"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({
                "status": "ok",
                "device": device_id,
                "type": device_type,
                "simulated": True,
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # quiet


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--device-id", default="sim-device")
    parser.add_argument("--device-type", default="server")
    args = parser.parse_args()
    device_id = args.device_id
    device_type = args.device_type
    HTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
