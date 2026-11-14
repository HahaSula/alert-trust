"""假 target：/metrics 輸出 lab_value（0 或 1）；/down 之後 /metrics 回 500（up 變 0），/up 恢復；/value/0、/value/1 改值。"""
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = {"down": False, "value": 0}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        p = self.path
        if p == "/metrics":
            if STATE["down"]:
                self.send_response(500)
                self.end_headers()
                return
            body = f"lab_value {STATE['value']}\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)
            return
        if p == "/down":
            STATE["down"] = True
        elif p == "/up":
            STATE["down"] = False
        elif p.startswith("/value/"):
            STATE["value"] = int(p.rsplit("/", 1)[1])
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok\n")

    def log_message(self, *a):
        pass


HTTPServer(("", 8000), H).serve_forever()
