"""alert-delivery lab 的接收端，只用標準函式庫。

一個行程做四件事，所有時間都是這個容器的 time.time()（牆鐘）：
  GET  /metrics            受控的 exporter：at_ad_flag{scen="x",inst="N"}
  GET  /set?inst=N&v=0|1   改 gauge，記一筆 set 事件
  GET  /mark?what=...      驅動程式的記號（例如 kill 之前）
  POST /hook/<am>          webhook，記一筆 notify 事件，原始 payload 另存
  GET  /log                目前為止的事件（JSON 陣列），給驅動程式等待用
另有一條執行緒每 1s 輪詢兩台 Alertmanager 的 /api/v2/alerts 與 Prometheus 的 /api/v1/alerts，
狀態或 endsAt 改變時記一筆。
"""
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

FLAGS = {"1": 0, "2": 0, "3": 0}
EVENTS = []
LOCK = threading.Lock()
LOG = open("/results/events.jsonl", "a", buffering=1)
RAW = open("/results/payloads.jsonl", "a", buffering=1)


def log(ev):
    ev = {"t": round(time.time(), 3), **ev}
    with LOCK:
        EVENTS.append(ev)
        LOG.write(json.dumps(ev) + "\n")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, body, ctype="text/plain"):
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/metrics":
            lines = [f'at_ad_flag{{scen="x",inst="{i}"}} {v}' for i, v in sorted(FLAGS.items())]
            self.reply("\n".join(lines) + "\n")
        elif u.path == "/set":
            FLAGS[q["inst"]] = int(q["v"])
            log({"ev": "set", "inst": q["inst"], "v": int(q["v"])})
            self.reply("ok")
        elif u.path == "/mark":
            log({"ev": "mark", "what": q.get("what", "")})
            self.reply("ok")
        elif u.path == "/log":
            with LOCK:
                self.reply(json.dumps(EVENTS), "application/json")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        t = time.time()
        am = self.path.rsplit("/", 1)[-1]
        RAW.write(json.dumps({"t": round(t, 3), "am": am, "payload": body}) + "\n")
        alerts = [{"inst": a["labels"].get("inst"), "status": a["status"],
                   "startsAt": a["startsAt"], "endsAt": a["endsAt"]} for a in body.get("alerts", [])]
        log({"ev": "notify", "am": am, "status": body.get("status"), "alerts": alerts})
        self.reply("ok")


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def poll():
    seen = {}
    while True:
        for am in ("a", "b"):
            res = get(f"http://am-{am}:9093/api/v2/alerts")
            if isinstance(res, list):
                cur = {}
                for a in res:
                    k = (am, a["labels"].get("inst"))
                    cur[k] = (a["status"]["state"], a["endsAt"])
                    if seen.get(k) != cur[k]:
                        log({"ev": "am", "am": am, "inst": k[1], "state": cur[k][0], "startsAt": a["startsAt"],
                             "updatedAt": a["updatedAt"], "endsAt": a["endsAt"]})
                for k in [k for k in seen if k[0] == am and k not in cur]:
                    log({"ev": "am", "am": am, "inst": k[1], "state": "gone"})
                seen.update(cur)
                for k in [k for k in seen if k[0] == am and k not in cur]:
                    del seen[k]
        res = get("http://prom:9090/api/v1/alerts")
        cur = {}
        for a in res.get("data", {}).get("alerts", []) if isinstance(res, dict) else []:
            k = ("prom", a["labels"].get("inst"))
            cur[k] = (a["state"], a.get("activeAt"))
            if seen.get(k) != cur[k]:
                log({"ev": "prom", "inst": k[1], "state": a["state"], "activeAt": a.get("activeAt")})
        for k in [k for k in seen if k[0] == "prom" and k not in cur]:
            log({"ev": "prom", "inst": k[1], "state": "inactive"})
            del seen[k]
        seen.update(cur)
        time.sleep(1)


def clock():
    """每 30s（單調時鐘）記一次牆鐘與單調時鐘，看兩者的差（Docker Desktop VM 上單調時鐘會比牆鐘慢）。"""
    while True:
        w, m = time.time(), time.monotonic()
        time.sleep(30)
        log({"ev": "clock", "wall": round(time.time() - w, 3), "mono": round(time.monotonic() - m, 3)})


if __name__ == "__main__":
    threading.Thread(target=poll, daemon=True).start()
    threading.Thread(target=clock, daemon=True).start()
    log({"ev": "start"})
    ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
