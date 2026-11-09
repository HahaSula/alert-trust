#!/usr/bin/env python3
"""etcd lab 用的小工具：查 Prometheus、直接打 etcd 的 HTTP 端點、透過 gRPC gateway 寫入與讀取。

  kv.py snap                      每台 member 一列：up、三個 probe、has_leader、is_leader、leader_changes
  kv.py q '<PromQL>'              印出 instant query 結果
  kv.py alerts                    印出 component="etcd" 的 ALERTS（pending／firing）
  kv.py get <port> <path> [t]     直接 GET 某台 etcd 的 /metrics、/readyz、/livez、/health，印出狀態碼、耗時（主機時鐘）、大小
  kv.py put <port> <n> <bytes> <prefix>   依序寫入 n 個 key，遇到第一個錯誤就停並印出錯誤
  kv.py load <port> <prefix> <threads> <seconds>   用多條執行緒反覆讀整個 prefix，同時每 0.5 秒量一次 /metrics 的耗時
  kv.py delprefix <port> <prefix>  刪掉整個 prefix，同時量 /metrics 耗時
"""
import base64
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PROM = "http://127.0.0.1:28090"
PORTS = {"e1:2379": 28001, "e2:2379": 28002, "e3:2379": 28003}


def http(url, data=None, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, time.monotonic() - t0, body
    except urllib.error.HTTPError as e:
        return e.code, time.monotonic() - t0, e.read()
    except Exception as e:  # noqa: BLE001 - timeouts and refused connections are results here
        return type(e).__name__, time.monotonic() - t0, str(e).encode()


def q(expr):
    code, _, body = http(f"{PROM}/api/v1/query?query={urllib.parse.quote(expr)}")
    if code != 200:
        return []
    return json.loads(body)["data"]["result"]


def by_instance(expr):
    return {r["metric"].get("instance"): r["value"][1] for r in q(expr)}


def snap():
    cols = [("up", 'up{job="etcd"}'), ("readyz", 'probe_success{job="etcd-readyz"}'),
            ("livez", 'probe_success{job="etcd-livez"}'), ("health", 'probe_success{job="etcd-health"}'),
            ("has_leader", 'etcd_server_has_leader{job="etcd"}'), ("is_leader", 'etcd_server_is_leader{job="etcd"}'),
            ("leader_changes", 'etcd_server_leader_changes_seen_total{job="etcd"}')]
    data = {name: by_instance(e) for name, e in cols}
    print("    instance | " + " | ".join(n for n, _ in cols))
    for inst in sorted(PORTS):
        print(f"    {inst} | " + " | ".join(data[n].get(inst, "（無）") for n, _ in cols))


def alerts():
    rows = q('ALERTS{component="etcd"}')
    if not rows:
        print("    （沒有 etcd 的 pending／firing 告警）")
    for r in sorted(rows, key=lambda r: (r["metric"]["alertname"], r["metric"].get("instance", ""))):
        m = r["metric"]
        print(f"    {m['alertstate']:7} {m['alertname']} {m.get('instance', m.get('job', ''))}")


def b64(s):
    return base64.b64encode(s.encode() if isinstance(s, str) else s).decode()


def prefix_end(p):
    return p[:-1] + chr(ord(p[-1]) + 1)


def get(port, path, timeout=10):
    code, dt, body = http(f"http://127.0.0.1:{port}{path}", timeout=timeout)
    text = body.decode("utf-8", "replace").strip().replace("\n", " ⏎ ")
    print(f"    GET :{port}{path} -> {code}，{dt:.2f}s（主機時鐘），{len(body)} bytes" + (f"：{text[:160]}" if path != "/metrics" else ""))


def put(port, n, size, prefix):
    val = "x" * size
    for i in range(n):
        code, dt, body = http(f"http://127.0.0.1:{port}/v3/kv/put", {"key": b64(f"{prefix}{i:06d}"), "value": b64(val)})
        if code != 200:
            print(f"    put #{i} -> {code}：{body.decode()[:200]}")
            return
    print(f"    put {n} 個 key（每個 {size} bytes）全部成功")


def measure_metrics(port, stop, out):
    while not stop.is_set():
        code, dt, body = http(f"http://127.0.0.1:{port}/metrics", timeout=10)
        out.append((code, dt))
        time.sleep(0.5)


def summary(name, samples):
    ok = [d for c, d in samples if c == 200]
    bad = [c for c, d in samples if c != 200]
    if ok:
        ok.sort()
        print(f"    {name}：/metrics {len(samples)} 次，非 200 {len(bad)} 次；耗時 min {ok[0]:.3f}s、中位 {ok[len(ok)//2]:.3f}s、max {ok[-1]:.3f}s（主機時鐘）")
    else:
        print(f"    {name}：/metrics {len(samples)} 次全部失敗 {bad[:3]}")


def load(port, prefix, threads, seconds):
    base = []
    stop = threading.Event()
    t = threading.Thread(target=measure_metrics, args=(port, stop, base))
    t.start(); time.sleep(5); stop.set(); t.join()
    summary("無負載基線", base)
    stop = threading.Event()
    counts = [0] * threads
    sizes = []

    def reader(i):
        while not stop.is_set():
            code, dt, body = http(f"http://127.0.0.1:{port}/v3/kv/range",
                                  {"key": b64(prefix), "range_end": b64(prefix_end(prefix))}, timeout=30)
            counts[i] += 1
            if not sizes:
                sizes.append(len(body))

    samples = []
    ths = [threading.Thread(target=reader, args=(i,)) for i in range(threads)]
    m = threading.Thread(target=measure_metrics, args=(port, stop, samples))
    for th in ths:
        th.start()
    m.start()
    time.sleep(seconds)
    stop.set()
    for th in ths + [m]:
        th.join()
    print(f"    {threads} 條執行緒 {seconds} 秒內共讀 {sum(counts)} 次整個 prefix（每次回應 {sizes[0] if sizes else 0} bytes）")
    summary("讀取負載中", samples)


def delprefix(port, prefix):
    stop = threading.Event()
    samples = []
    m = threading.Thread(target=measure_metrics, args=(port, stop, samples))
    m.start()
    time.sleep(1)
    code, dt, body = http(f"http://127.0.0.1:{port}/v3/kv/deleterange",
                          {"key": b64(prefix), "range_end": b64(prefix_end(prefix))}, timeout=30)
    time.sleep(1.5)
    stop.set(); m.join()
    print(f"    deleterange -> {code}，{dt:.2f}s：{body.decode()[:120]}")
    summary("刪除期間", samples)


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "snap":
        snap()
    elif cmd == "q":
        for r in q(args[0]):
            lab = {k: v for k, v in r["metric"].items() if k not in ("__name__",)}
            print(f"    {r['metric'].get('__name__', '')}{lab} = {r['value'][1]}")
        if not q(args[0]):
            print("    （空）")
    elif cmd == "alerts":
        alerts()
    elif cmd == "get":
        get(int(args[0]), args[1], float(args[2]) if len(args) > 2 else 10)
    elif cmd == "put":
        put(int(args[0]), int(args[1]), int(args[2]), args[3])
    elif cmd == "load":
        load(int(args[0]), args[1], int(args[2]), int(args[3]))
    elif cmd == "delprefix":
        delprefix(int(args[0]), args[1])
