from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CGROUP = Path("/sys/fs/cgroup")


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        try:
            cpu = dict(line.split() for line in (CGROUP / "cpu.stat").read_text().splitlines())
            memory = dict(line.split() for line in (CGROUP / "memory.stat").read_text().splitlines())
        except OSError:
            self.send_error(503, "Container cgroup metrics unavailable")
            return
        body = (
            "# TYPE playground_stress_cpu_seconds_total counter\n"
            f"playground_stress_cpu_seconds_total {int(cpu['usage_usec']) / 1_000_000}\n"
            "# TYPE playground_stress_anonymous_memory_bytes gauge\n"
            f"playground_stress_anonymous_memory_bytes {int(memory['anon'])}\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), MetricsHandler).serve_forever()
