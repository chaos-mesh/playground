import json
import threading
import time
import urllib.request
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET = "http://podinfo:9898/api/info"
sample = {}


def observe():
    started = time.monotonic()
    try:
        with urllib.request.urlopen(TARGET, timeout=1) as response:
            payload = json.load(response)
            return {
                "failed": 0,
                "latency_milliseconds": (time.monotonic() - started) * 1000,
                "replaced": int(payload == {"message": "Replaced by HTTPChaos"}),
                "patched": int(response.headers.get("X-Playground-Chaos") == "patched"),
            }
    except (HTTPException, OSError, ValueError):
        return {"failed": 1}


def collect():
    global sample
    while True:
        sample = observe()
        time.sleep(0.25)


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = "".join(f"playground_http_{name} {value}\n" for name, value in sample.items()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    threading.Thread(target=collect, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), MetricsHandler).serve_forever()
