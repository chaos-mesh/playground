import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET = "podinfo.playground-demo.svc.cluster.local."
EXPECTED = os.environ["PODINFO_SERVICE_HOST"]
STATIC = "192.0.2.42"
sample = {}


def collect():
    global sample
    while True:
        try:
            addresses = set(socket.gethostbyname_ex(TARGET)[2])
            sample = {
                "resolution_failed": 0,
                "unexpected_address": int(addresses != {EXPECTED}),
                "static_address": int(addresses == {STATIC}),
            }
        except OSError:
            sample = {"resolution_failed": 1}
        time.sleep(0.25)


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = "".join(f"playground_dns_{name} {value}\n" for name, value in sample.items()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    threading.Thread(target=collect, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), MetricsHandler).serve_forever()
