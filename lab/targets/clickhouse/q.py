#!/usr/bin/env python3
"""Lab 小工具：查 lab 的 Prometheus（127.0.0.1:31090），或直接打元件的 HTTP 端點（主機時鐘）。
  q.py q '<promql>'          即時查詢，每條 series 一行
  q.py alerts [regex]        目前 firing／pending 的 alert（可用 regex 篩 alertname）
  q.py get <port> <path> [timeout]   HTTP GET，印狀態碼、大小、耗時（主機 monotonic 時鐘）、內容前 200 字
  q.py sql <port> <SQL>      用 ClickHouse HTTP 介面跑一條 SQL（default 帳號），印狀態碼、耗時、結果前 300 字
  q.py tcp <port> <4lw>      對 Keeper 送 4lw 指令（ruok、mntr），印回應
"""
import json, sys, time, urllib.parse, urllib.request
PROM = "http://127.0.0.1:31090"

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

def sql(port, query, timeout=30):
    t = time.monotonic(); url = f"http://127.0.0.1:{port}/"
    req = urllib.request.Request(url, data=query.encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=float(timeout)) as r:
            b = r.read(); code = r.status
    except urllib.error.HTTPError as e:
        b = e.read(); code = e.code
    except Exception as e:
        print(f"  SQL[:{port}] {query[:90]} → {type(e).__name__} {e} after {time.monotonic()-t:.2f}s"); return
    body = b[:300].decode("utf-8", "replace").strip().replace("\n", " | ")
    print(f"  SQL[:{port}] {query[:90]} → {code} {time.monotonic()-t:.2f}s {body}")

def tcp(port, cmd):
    import socket
    t = time.monotonic()
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=4) as s:
            s.sendall(cmd.encode()); s.shutdown(socket.SHUT_WR); b = b""
            while True:
                c = s.recv(4096)
                if not c: break
                b += c
    except Exception as e:
        print(f"  4LW[:{port}] {cmd} → {type(e).__name__} {e} after {time.monotonic()-t:.2f}s"); return
    lines = [l for l in b.decode("utf-8", "replace").split("\n") if l and (cmd != "mntr" or l.split("\t")[0] in
             ("zk_server_state", "zk_znode_count", "zk_followers", "zk_synced_followers", "zk_outstanding_requests")) or not l.startswith("zk_")]
    print(f"  4LW[:{port}] {cmd} → {time.monotonic()-t:.2f}s " + " ; ".join(l for l in lines if l))

if __name__ == "__main__":
    cmd, *a = sys.argv[1:]
    {"q": q, "alerts": alerts, "get": get, "sql": sql, "tcp": tcp}[cmd](*a)
