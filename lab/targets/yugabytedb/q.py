#!/usr/bin/env python3
"""Lab 小工具：查 lab 的 Prometheus（127.0.0.1:30090），或直接打元件的 HTTP 端點（主機時鐘）。
  q.py q '<promql>'          即時查詢，每條 series 一行
  q.py alerts [regex]        目前 firing／pending 的 alert（可用 regex 篩 alertname）
  q.py get <port> <path> [timeout]   HTTP GET，印狀態碼、大小、耗時（主機 monotonic 時鐘）、內容前 200 字
"""
import json, sys, time, urllib.parse, urllib.request
PROM = "http://127.0.0.1:30090"

def prom(path, **params):
    url = PROM + path + ("?" + urllib.parse.urlencode(params) if params else "")
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)

def lbl(m):
    return "{" + ", ".join(f'{k}="{v}"' for k, v in sorted(m.items())) + "}"

def q(expr):
    d = prom("/api/v1/query", query=expr)
    if d["status"] != "success":
        print("  ERROR", d); return []
    res = d["data"]["result"]
    print(f"  > {expr}  →  {len(res)} series")
    for s in res:
        print(f"    {lbl(s['metric'])} {s['value'][1]}")
    return res

def alerts(rx=None):
    import re
    d = prom("/api/v1/alerts")["data"]["alerts"]
    d = [a for a in d if not rx or re.search(rx, a["labels"]["alertname"])]
    print(f"  alerts{'（' + rx + '）' if rx else ''}: {len(d)}")
    for a in sorted(d, key=lambda a: (a["state"], a["labels"]["alertname"])):
        l = a["labels"]; inst = l.get("instance", l.get("store", ""))
        print(f"    {a['state']:8} {l['alertname']} {inst}")

def get(port, path, timeout=10):
    t = time.monotonic(); url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=float(timeout)) as r:
            b = r.read(); code = r.status
    except urllib.error.HTTPError as e:
        b = e.read(); code = e.code
    except Exception as e:
        print(f"  GET :{port}{path} → {type(e).__name__} {e} after {time.monotonic()-t:.2f}s"); return
    body = b[:200].decode("utf-8", "replace").replace("\n", " ")
    print(f"  GET :{port}{path} → {code} {len(b)}B {time.monotonic()-t:.2f}s {body}")

if __name__ == "__main__":
    cmd, *a = sys.argv[1:]
    {"q": q, "alerts": alerts, "get": get}[cmd](*a)
