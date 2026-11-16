import os
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SAMPLE = Path("/demo-data/sample.bin")
CONTENT = b"Chaos Mesh Playground\n" * 64
measurements = {}


def measure():
    global measurements
    while True:
        values = {}
        start = time.monotonic()
        try:
            content = SAMPLE.read_bytes()
            values["read_milliseconds"] = (time.monotonic() - start) * 1000
            values["read_error"] = 0
            values["content_mismatch"] = int(content != CONTENT)
        except OSError:
            values["read_error"] = 1
        try:
            mode = stat.S_IMODE(SAMPLE.stat().st_mode)
            values["mode_octal"] = format(mode, "o")
        except OSError:
            pass
        measurements = values
        time.sleep(0.25)


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = "".join(f"playground_io_{name} {value}\n" for name, value in measurements.items()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    SAMPLE.write_bytes(CONTENT)
    os.chmod(SAMPLE, 0o644)
    threading.Thread(target=measure, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), MetricsHandler).serve_forever()
