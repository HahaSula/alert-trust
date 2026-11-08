#!/usr/bin/env python3
"""C：查詢 slot。Prometheus 只開 1 個 slot（--query.max-concurrency=1），--query.timeout=300ms。

C1 關卡 ①（建立查詢、解析與前處理）不受 query.timeout 限制：
   H 是一個很長的 1+1+…+1（15000 項）。前處理在關卡 ① 裡，耗時隨項數平方成長，約 1–2 秒；
   200 ms 後送出 P = vector(1)，量 P 的總耗時與結果。
C2 關卡 ②（執行）受 query.timeout 限制，失敗時 pending 不變：
   SlotProbe 是永遠成立、for: 1h 的 alert，先讓它進入 pending；再用 6 條執行緒不停送
   count_over_time(vector(1)[1500s:1ms])（每條約 100–150 ms）佔住 slot 20 秒，
   記錄 rule 的 health／lastError、失敗計數與 alert 的 activeAt。

  python3 slots.py          # 約 2 分鐘，結果寫到 results/slots.json
"""
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CASES = [("slot", 21310, "2.55.1"), ("slot3", 21311, "3.13.5")]
GIANT = "query=" + "%2B".join(["1"] * 15000)
LOAD = "count_over_time(vector(1)[1500s:1ms])"


def post(port, body: bytes, timeout=30):
    t0 = time.time()
    req = urllib.request.Request(f"http://localhost:{port}/api/v1/query", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode()
    except urllib.error.HTTPError as e:
        txt = e.read().decode()
    d = json.loads(txt)
    return round((time.time() - t0) * 1000), d.get("status"), d.get("error")


def get(port, path):
    with urllib.request.urlopen(f"http://localhost:{port}{path}", timeout=5) as r:
        return r.read().decode()


def metric(port, name):
    for ln in get(port, "/metrics").splitlines():
        if ln.startswith(name):
            return float(ln.rsplit(" ", 1)[1])
    return None


def probe_state(port):
    rules = json.loads(get(port, "/api/v1/rules"))["data"]["groups"][0]["rules"][0]
    alerts = json.loads(get(port, "/api/v1/alerts"))["data"]["alerts"]
    a = alerts[0] if alerts else {}
    return {"health": rules["health"], "lastError": rules.get("lastError", ""),
            "state": a.get("state"), "activeAt": a.get("activeAt"),
            "failures": metric(port, 'prometheus_rule_evaluation_failures_total{rule_group=')}


def run_case(svc, port, ver):
    subprocess.run(["docker", "compose", "--profile", "slot", "up", "-d", svc], cwd=HERE, check=True,
                   capture_output=True)
    for _ in range(60):
        try:
            if probe_state(port)["state"] == "pending":
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    res = {"version": ver, "flags": "--query.max-concurrency=1 --query.timeout=300ms"}

    # C1
    trials = []
    for _ in range(5):
        h = {}
        th = threading.Thread(target=lambda: h.update(r=post(port, GIANT.encode())))
        th.start()
        time.sleep(0.2)
        p = post(port, urllib.parse.urlencode({"query": "vector(1)"}).encode())
        th.join()
        trials.append({"P_ms": p[0], "P_status": p[1], "P_error": p[2],
                       "H_ms": h["r"][0], "H_status": h["r"][1], "H_error": h["r"][2]})
        time.sleep(1)
    res["c1_gate1"] = trials

    # C2
    before = probe_state(port)
    stop = time.time() + 20
    load = {"ok": 0, "errors": {}}
    lock = threading.Lock()

    def loader():
        body = urllib.parse.urlencode({"query": LOAD}).encode()
        while time.time() < stop:
            _, st, err = post(port, body)
            with lock:
                if st == "success":
                    load["ok"] += 1
                else:
                    load["errors"][err] = load["errors"].get(err, 0) + 1

    ths = [threading.Thread(target=loader) for _ in range(6)]
    for x in ths:
        x.start()
    during = []
    while time.time() < stop:
        s = probe_state(port)
        s["t"] = round(20 - (stop - time.time()), 1)
        during.append(s)
        time.sleep(0.5)
    for x in ths:
        x.join()
    time.sleep(5)
    after = probe_state(port)
    res["c2_gate2"] = {
        "before": before, "after_5s": after, "load": load,
        "during_health": {h: sum(1 for s in during if s["health"] == h) for h in {s["health"] for s in during}},
        "during_errors": sorted({s["lastError"] for s in during if s["lastError"]}),
        "during_states": sorted({(s["state"], s["activeAt"]) for s in during}, key=str),
        "samples": during,
    }
    subprocess.run(["docker", "compose", "--profile", "slot", "rm", "-sf", svc], cwd=HERE, check=True,
                   capture_output=True)
    return res


def main():
    out = [run_case(*c) for c in CASES]
    for r in out:
        print(r["version"], json.dumps(r["c1_gate1"], ensure_ascii=False))
        c2 = r["c2_gate2"]
        print(" ", {k: c2[k] for k in ("before", "after_5s", "load", "during_health", "during_errors", "during_states")})
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    with open(os.path.join(HERE, "results", "slots.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
