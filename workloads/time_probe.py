import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return

        realtime = time.clock_gettime(time.CLOCK_REALTIME)
        body = f"playground_time_realtime_seconds {realtime}\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), MetricsHandler).serve_forever()
