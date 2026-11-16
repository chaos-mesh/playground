import socket
import struct
import sys
import threading
import time
import zlib
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 9000
PACKET_SIZE = 1000
PACKET_INTERVAL = 0.002  # 4 Mbit/s of UDP payload at the offered rate.
WINDOW = 2
HEADER = struct.Struct("!QQ")
CHECKSUM = struct.Struct("!I")
PADDING = bytes(PACKET_SIZE - HEADER.size - CHECKSUM.size)
SO_NO_CHECK = 11  # Linux IPv4 UDP. Verify the payload in the receiving workload.


def send_packets():
    sequence = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stream:
        stream.setsockopt(socket.SOL_SOCKET, SO_NO_CHECK, 1)
        while True:
            try:
                destination = (socket.gethostbyname("network-probe"), PORT)
            except socket.gaierror:
                time.sleep(WINDOW)
                continue

            deadline = time.monotonic() + WINDOW
            next_send = time.monotonic()
            while next_send < deadline:
                time.sleep(max(0, next_send - time.monotonic()))
                payload = HEADER.pack(sequence, time.time_ns()) + PADDING
                stream.sendto(payload + CHECKSUM.pack(zlib.crc32(payload)), destination)
                sequence += 1
                next_send = max(next_send + PACKET_INTERVAL, time.monotonic())


class Receiver:
    sample = {}

    def measure(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stream:
            stream.bind(("", PORT))
            stream.settimeout(0.1)
            peer = None
            last_sequence = -1
            recent_sequences = deque()
            seen = set()
            while True:
                start = time.monotonic()
                first_sequence = last_sequence
                packets = unique = duplicates = corrupted = 0
                delay_total = 0
                while time.monotonic() - start < WINDOW:
                    try:
                        packet, sender = stream.recvfrom(PACKET_SIZE)
                    except TimeoutError:
                        continue
                    packets += 1
                    if len(packet) != PACKET_SIZE or zlib.crc32(packet[:-CHECKSUM.size]) != CHECKSUM.unpack_from(packet, PACKET_SIZE - CHECKSUM.size)[0]:
                        corrupted += 1
                        continue
                    sequence, sent_at = HEADER.unpack_from(packet)
                    if sender != peer:
                        peer = sender
                        first_sequence = last_sequence = sequence - 1
                        recent_sequences.clear()
                        seen.clear()
                        unique = 0
                        delay_total = 0
                    if sequence in seen:
                        duplicates += 1
                        continue
                    seen.add(sequence)
                    recent_sequences.append(sequence)
                    if len(recent_sequences) > 2048:
                        seen.remove(recent_sequences.popleft())
                    if sequence <= first_sequence:
                        continue
                    unique += 1
                    last_sequence = max(last_sequence, sequence)
                    delay_total += max(0, time.time_ns() - sent_at)

                elapsed = time.monotonic() - start
                values = {
                    "udp_reachable": int(packets > 0),
                    "receive_bits_per_second": packets * PACKET_SIZE * 8 / elapsed,
                }
                if packets:
                    values["duplicate_percent"] = 100 * duplicates / packets
                    values["corrupt_percent"] = 100 * corrupted / packets
                if unique:
                    sent = last_sequence - first_sequence
                    values["packet_loss_percent"] = 100 * (sent - unique) / sent
                    values["one_way_milliseconds"] = delay_total / unique / 1_000_000
                elif not packets:
                    peer = None
                self.sample = values


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = "".join(
            f"playground_network_{name} {value:.4f}\n"
            for name, value in self.receiver.sample.items()
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    if sys.argv[1] == "send":
        send_packets()
    else:
        receiver = Receiver()
        threading.Thread(target=receiver.measure, daemon=True).start()
        MetricsHandler.receiver = receiver
        ThreadingHTTPServer(("0.0.0.0", 8080), MetricsHandler).serve_forever()
