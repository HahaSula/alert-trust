# External notification receiver: stands in for the on-call endpoint / dead man's switch service.
import json, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
last, count = {}, {}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers["Content-Length"]))); now = time.time()
        with open("/out/notifications.jsonl", "a") as f:
            for a in d["alerts"]:
                n, s = a["labels"].get("alertname", "?"), a["status"]
                f.write(json.dumps({"t": now, "alertname": n, "status": s, "startsAt": a["startsAt"]}) + "\n")
                if s == "firing": last[n] = now
                count[(n, s)] = count.get((n, s), 0) + 1
        self.send_response(200); self.end_headers()
    def do_GET(self):
        o = ['sink_last_received_seconds{alertname="%s"} %f\n' % (k, v) for k, v in last.items()]
        o += ['sink_notifications_total{alertname="%s",status="%s"} %d\n' % (k[0], k[1], v) for k, v in count.items()]
        b = "".join(o).encode(); self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
ThreadingHTTPServer(("", 9000), H).serve_forever()
