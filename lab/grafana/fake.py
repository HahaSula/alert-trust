#!/usr/bin/env python3
"""假 exporter＋假 Prometheus API＋webhook 接收端。只用標準函式庫。

GET  /metrics            exporter=1 時輸出 lab_value{series="a"} 1；exporter=0 時回 503（scrape 失敗）
GET|POST /api/v1/...     假 Prometheus API：先睡 delay 秒，再回一個 series {series="slow"} 值 1
POST /ctl?exporter=0&delay=25   改變行為
POST /hook/<name>        webhook：記錄收到的每個 alert 的 alertname、status、startsAt、endsAt
GET  /stats              目前設定、每個 webhook 收到的紀錄、API 請求次數與最後一次耗時
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATE = {"exporter": 1, "delay": 0.0}
HOOKS: dict = {}
API = {"requests": 0, "in_flight": 0, "max_in_flight": 0}
LOCK = threading.Lock()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def api(self, params):
        with LOCK:
            API["requests"] += 1
            API["in_flight"] += 1
            API["max_in_flight"] = max(API["max_in_flight"], API["in_flight"])
            delay = STATE["delay"]
        try:
            time.sleep(delay)
            path = urlparse(self.path).path
            if path.endswith("/status/buildinfo"):
                return self.reply(200, json.dumps({"status": "success", "data": {"version": "3.13.5"}}))
            ts = float((params.get("time") or params.get("end") or [time.time()])[0])
            if path.endswith("/query_range"):
                data = {"resultType": "matrix", "result": [{"metric": {"series": "slow"}, "values": [[ts, "1"]]}]}
            elif path.endswith("/query"):
                data = {"resultType": "vector", "result": [{"metric": {"series": "slow"}, "value": [ts, "1"]}]}
            else:
                data = []
            self.reply(200, json.dumps({"status": "success", "data": data}))
        finally:
            with LOCK:
                API["in_flight"] -= 1

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/metrics":
            if STATE["exporter"]:
                return self.reply(200, 'lab_value{series="a"} 1\n', "text/plain; version=0.0.4")
            return self.reply(503, "down\n", "text/plain")
        if u.path == "/stats":
            with LOCK:
                return self.reply(200, json.dumps({"state": STATE, "api": API, "hooks": HOOKS}))
        if u.path.startswith("/api/v1/"):
            return self.api(parse_qs(u.query))
        self.reply(404, "{}")

    def do_POST(self):
        u = urlparse(self.path)
        raw = self.body()
        if u.path == "/ctl":
            q = parse_qs(u.query)
            with LOCK:
                if "exporter" in q:
                    STATE["exporter"] = int(q["exporter"][0])
                if "delay" in q:
                    STATE["delay"] = float(q["delay"][0])
                if "reset" in q:
                    HOOKS.clear()
            return self.reply(200, json.dumps(STATE))
        if u.path.startswith("/hook/"):
            name = u.path.split("/", 2)[2]
            try:
                msg = json.loads(raw or b"{}")
            except ValueError:
                msg = {}
            rec = [{"alertname": a.get("labels", {}).get("alertname"),
                    "rulename": a.get("labels", {}).get("rulename"),
                    "status": a.get("status"),
                    "startsAt": a.get("startsAt"), "endsAt": a.get("endsAt")}
                   for a in msg.get("alerts", [])]
            with LOCK:
                HOOKS.setdefault(name, []).append({"t": round(time.time(), 1), "alerts": rec})
            return self.reply(200, "{}")
        if u.path.startswith("/api/v1/"):
            params = parse_qs(u.query)
            params.update(parse_qs(raw.decode(errors="replace")))
            return self.api(params)
        self.reply(404, "{}")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
