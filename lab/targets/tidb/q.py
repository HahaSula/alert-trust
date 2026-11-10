#!/usr/bin/env python3
"""Lab 小工具：查 lab 的 Prometheus（127.0.0.1:29090），或直接打元件的 HTTP 端點（主機時鐘）。
  q.py q '<promql>'          即時查詢，每條 series 一行
  q.py alerts [regex]        目前 firing／pending 的 alert（可用 regex 篩 alertname）
  q.py get <port> <path> [timeout]   HTTP GET，印狀態碼、大小、耗時（主機時鐘）、內容前 200 字
  q.py rules-empty <file>    官方規則檔裡每條 expr 現在回傳幾條 series
  q.py audit <file>          官方規則檔裡每個 vector selector 現在對到幾條 series（0 代表規則一定回傳空）
  q.py stores <port>         PD API 的 store 狀態（/pd/api/v1/stores）
"""
import json, sys, time, urllib.parse, urllib.request
PROM = "http://127.0.0.1:29090"

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
    t = time.time(); url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=float(timeout)) as r:
            b = r.read(); code = r.status
    except urllib.error.HTTPError as e:
        b = e.read(); code = e.code
    except Exception as e:
        print(f"  GET :{port}{path} → {type(e).__name__} {e} after {time.time()-t:.2f}s"); return
    body = b[:200].decode("utf-8", "replace").replace("\n", " ")
    print(f"  GET :{port}{path} → {code} {len(b)}B {time.time()-t:.2f}s {body}")

def rules_empty(path):
    import yaml
    for g in yaml.safe_load(open(path))["groups"]:
        for r in g["rules"]:
            d = prom("/api/v1/query", query=r["expr"])
            n = len(d["data"]["result"]) if d["status"] == "success" else "ERR"
            print(f"    {r['alert']}: {n}")

SEL = None

def audit(path):
    import re, yaml
    sel = re.compile(r'\b([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(\{[^}]*\})')
    bare = re.compile(r'\b((?:tidb|tikv|pd|etcd|process|go)_[a-zA-Z0-9_:]*)\b(?!\s*[{(])')
    for g in yaml.safe_load(open(path))["groups"]:
        for r in g["rules"]:
            expr = r["expr"]; found = []
            for m in sel.finditer(expr):
                found.append(m.group(1) + m.group(2))
            stripped = sel.sub("", expr)
            for m in bare.finditer(stripped):
                found.append(m.group(1))
            out = []
            for f in dict.fromkeys(found):
                d = prom("/api/v1/query", query=f"count({f})")
                n = int(float(d["data"]["result"][0]["value"][1])) if d["status"] == "success" and d["data"]["result"] else 0
                out.append(f"{f}={n}")
            zero = [o for o in out if o.endswith("=0")]
            print(f"    {'ZERO ' if zero else 'ok   '}{r['alert']}: {'; '.join(out)}")

def stores(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/pd/api/v1/stores", timeout=10) as r:
        d = json.load(r)
    for s in d.get("stores", []):
        st = s["store"]; ss = s.get("status", {})
        print(f"    store {st['id']} {st['address']} state_name={st.get('state_name')} last_heartbeat={ss.get('last_heartbeat_ts','')} leaders={ss.get('leader_count')} regions={ss.get('region_count')}")

if __name__ == "__main__":
    cmd, *a = sys.argv[1:]
    {"q": q, "alerts": alerts, "get": get, "rules-empty": rules_empty, "audit": audit, "stores": stores}[cmd](*a)
