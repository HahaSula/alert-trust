#!/usr/bin/env python3
"""alert-delivery lab 的驅動程式（約 10 分鐘）。

  python3 run.py            # 情境 (a)–(e)，約 10 分鐘，最後 docker compose down -v，再跑 analyze.py
  python3 run.py rounding   # repeat_interval 75s 與 105s（group_interval 30s），約 5 分鐘，結果在 results/rounding/
  python3 analyze.py        # 只重算 results/summary.md

時間一律以接收端容器的牆鐘（events.jsonl 的 t）與 Alertmanager／Prometheus API 的時間戳記為準；
這支程式在主機上，只負責下指令與存 API 快照，不用主機時鐘算任何延遲。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
RECV = "http://localhost:35080"
AMS = {"a": "http://localhost:35093", "b": "http://localhost:35094"}


def sh(*cmd, check=True):
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=HERE, check=check)


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r) if "json" in r.headers.get("Content-Type", "") else r.read().decode()


def events():
    return get(RECV + "/log")


def wait_for(pred, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        ev = [e for e in events() if pred(e)]
        if ev:
            print(f"  got {what}", flush=True)
            return ev[0]
        time.sleep(1)
    print(f"  TIMEOUT waiting for {what}", flush=True)
    return None


def notified(am, inst, status, after=0):
    return lambda e: (e["ev"] == "notify" and e["am"] == am and e["t"] > after
                      and any(a["inst"] == inst and a["status"] == status for a in e["alerts"]))


def snapshot(name):
    d = os.path.join(RES, "snapshots")
    os.makedirs(d, exist_ok=True)
    for am, base in AMS.items():
        for path, tag in (("/api/v2/alerts", "alerts"), ("/api/v2/alerts/groups", "groups")):
            try:
                data = get(base + path)
            except Exception as e:  # noqa: BLE001
                data = {"error": str(e)}
            with open(os.path.join(d, f"{name}-am-{am}-{tag}.json"), "w") as f:
                json.dump(data, f, indent=1)


def mark(what):
    get(RECV + "/mark?what=" + what)


def setf(inst, v):
    get(f"{RECV}/set?inst={inst}&v={v}")


def rounding():
    """repeat_interval 75s（設定 C）與 105s（設定 D）：只看不變的 group 什麼時候重送。"""
    res = os.path.join(RES, "rounding")
    os.makedirs(res, exist_ok=True)
    for f in ("events.jsonl", "payloads.jsonl"):
        if os.path.exists(os.path.join(res, f)):
            os.remove(os.path.join(res, f))
    env = dict(os.environ, AM_A_CONFIG="./configs/am-c.yml", AM_B_CONFIG="./configs/am-d.yml", RESULTS="./results/rounding")
    subprocess.run(["docker", "compose", "up", "-d"], cwd=HERE, check=True, env=env)
    try:
        ready()
        mark("a")
        setf(1, 1)
        wait_for(lambda e: e["ev"] == "notify", 90, "first notification")
        time.sleep(250)
        mark("end")
    finally:
        subprocess.run(["docker", "compose", "down", "-v"], cwd=HERE, env=env)
    subprocess.run([sys.executable, os.path.join(HERE, "analyze.py"), "rounding"], check=True)


def ready():
    for _ in range(60):
        try:
            events()
            get(AMS["a"] + "/-/ready")
            get(AMS["b"] + "/-/ready")
            get("http://localhost:35090/-/ready")
            break
        except Exception:  # noqa: BLE001
            time.sleep(1)
    time.sleep(12)  # 讓 Prometheus 先 scrape 到全為 0 的 gauge


def main():
    if sys.argv[1:] == ["rounding"]:
        return rounding()
    for f in ("events.jsonl", "payloads.jsonl"):
        p = os.path.join(RES, f)
        if os.path.exists(p):
            os.remove(p)
    sh("docker", "compose", "up", "-d")
    try:
        ready()

        print("(a) 新 group：inst 1，6s 後 inst 3", flush=True)
        mark("a")
        setf(1, 1)
        time.sleep(6)
        setf(3, 1)
        first = wait_for(notified("a", "1", "firing"), 90, "first notification on a")
        wait_for(notified("b", "1", "firing"), 30, "first notification on b")
        snapshot("a")

        print("(b) 第一次通知後 5s，inst 2 加入同一個 group", flush=True)
        while time.time() < first["t"] + 5 - 0.5:  # first["t"] 是容器時鐘；只用來大約排程
            time.sleep(0.5)
        mark("b")
        setf(2, 1)
        nb = wait_for(notified("a", "2", "firing"), 120, "inst 2 on a")
        wait_for(notified("b", "2", "firing"), 60, "inst 2 on b")
        snapshot("b")

        print("(c) inst 1 解除", flush=True)
        mark("c")
        setf(1, 0)
        wait_for(notified("a", "1", "resolved", nb["t"]), 120, "inst 1 resolved on a")
        wait_for(notified("b", "1", "resolved", nb["t"]), 60, "inst 1 resolved on b")
        snapshot("c")

        print("(d) 不變的 group，等 240s 看 repeat", flush=True)
        mark("d")
        time.sleep(240)
        snapshot("d")

        print("(e) kill Prometheus，看 EndsAt 到期", flush=True)
        mark("e-kill")
        sh("docker", "compose", "kill", "prom")
        wait_for(notified("a", "2", "resolved"), 180, "inst 2 resolved on a")
        wait_for(notified("b", "2", "resolved"), 60, "inst 2 resolved on b")
        time.sleep(3)
        snapshot("e")
        mark("end")
        with open(os.path.join(RES, "compose-logs.txt"), "w") as f:
            subprocess.run(["docker", "compose", "logs", "--no-color", "am-a", "am-b", "prom"], cwd=HERE,
                           stdout=f, stderr=subprocess.STDOUT)
    finally:
        sh("docker", "compose", "down", "-v", check=False)
    sh(sys.executable, os.path.join(HERE, "analyze.py"))


if __name__ == "__main__":
    main()
