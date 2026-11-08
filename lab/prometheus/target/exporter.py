# Fake target: known-true counter (demo_requests_total, 10/s), optional filler series, adjustable response delay.
import os, random, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
SERIES = int(os.environ.get("SERIES", "0")); delay = float(os.environ.get("DELAY", "0")); T0 = time.time()
def variant(seed):
    r = random.Random(seed); out = ["# HELP demo_filler filler series\n# TYPE demo_filler gauge\n"]
    for i in range(SERIES):
        out.append('demo_filler{id="%07d",pod="pod-%d",ns="team-%d"} %.3f\n' % (i, i % 500, i % 20, r.random() * 1000))
    return "".join(out).encode()
V = [variant(s) for s in range(4)] if SERIES else [b""]
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        global delay
        u = urlparse(self.path)
        if u.path == "/set":
            delay = float(parse_qs(u.query)["delay"][0]); body = b"ok\n"
        elif u.path == "/metrics":
            if delay: time.sleep(delay)
            head = ("# HELP demo_requests_total known-true counter, always +10/s\n# TYPE demo_requests_total counter\n"
                    "demo_requests_total %.1f\n# TYPE demo_toggle gauge\ndemo_toggle %d\n" % (10 * (time.time() - T0), 1 if time.time() % 120 < 60 else 0)).encode()
            body = head + V[int(time.time() / 15) % len(V)]
        else:
            self.send_error(404); return
        self.send_response(200); self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
ThreadingHTTPServer(("", 8000), H).serve_forever()
