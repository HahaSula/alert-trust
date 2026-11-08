"""假 target：30 條 lab_item{id} = 1，以及每秒 +1 的 lab_events_total{id}。"""
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

T0 = time.time()
N = 30


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        el = int(time.time() - T0)
        body = "".join(f'lab_item{{id="{i}"}} 1\nlab_events_total{{id="{i}"}} {el}\n' for i in range(N))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


HTTPServer(("", 8000), H).serve_forever()
