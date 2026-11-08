# 假的 target：demo_requests_total 每秒固定 +10，回應延遲由 ?delay=秒 決定（Prometheus 用 scrape params 帶入）。
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

T0 = time.time()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path != "/metrics":
            self.send_error(404)
            return
        delay = float(parse_qs(u.query).get("delay", ["0"])[0])
        start = time.time()
        if delay:
            time.sleep(delay)
        # 值用「開始時間」計算，與 Prometheus 用 scrape 開始時間當時間戳一致
        body = ("# TYPE demo_requests_total counter\ndemo_requests_total %.1f\n" % (10 * (start - T0))).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("", 8000), H).serve_forever()
