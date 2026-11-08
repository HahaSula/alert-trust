#!/usr/bin/env python3
"""Alertmanager 輕量實驗的驅動程式。

  python3 run.py delivery   # exp-am-delivery，約 2 分鐘
  python3 run.py smtp       # exp-am-smtp，約 3 分鐘
  python3 run.py window     # exp-am-window 的實機部分，約 9 分鐘
  python3 run.py ha         # exp-am-ha，約 5 分鐘

原始輸出寫到 results/<情境>/，最後一定會 docker compose down -v。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
VERS = {"0.27.0": ("am27", 21093, 21080), "0.29.0": ("am29", 21094, 21081), "0.34.2": ("am34", 21095, 21082)}
T0 = time.time()


def t() -> float:
    return round(time.time() - T0, 1)


def sh(*args, check=True, env=None, cwd=HERE):
    e = dict(os.environ, **(env or {}))
    return subprocess.run(args, check=check, env=e, cwd=cwd, capture_output=True, text=True)


def http(url, data=None, method=None, timeout=5):
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:  # noqa: BLE001
        return None, repr(e)


def post_alerts(port, alerts):
    body = [{"labels": a, "annotations": {"summary": "lab"}} for a in alerts]
    return http(f"http://localhost:{port}/api/v2/alerts", json.dumps(body).encode())


def metrics(port, pattern=r"^alertmanager_(notification|notifications|config_last|cluster_(members|failed)|nflog_(snapshot|maintenance))"):
    code, txt = http(f"http://localhost:{port}/metrics")
    if code != 200:
        return {}
    out = {}
    for ln in txt.splitlines():
        if ln.startswith("#") or not re.match(pattern, ln):
            continue
        k, v = ln.rsplit(" ", 1)
        out[k] = float(v)
    return out


def fake_stats(port):
    code, txt = http(f"http://localhost:{port}/stats")
    return json.loads(txt) if code == 200 else {}


def wait_ready(ports, timeout=60):
    end = time.time() + timeout
    for p in ports:
        while http(f"http://localhost:{p}/-/ready")[0] != 200:
            if time.time() > end:
                raise SystemExit(f"port {p} not ready")
            time.sleep(1)


def save(name, obj):
    path = os.path.join(RES, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
    print("wrote", path)


def compose_single(config, profiles=()):
    env = {"AM_CONFIG": config}
    pf = [x for p in profiles for x in ("--profile", p)]
    sh("docker", "compose", *pf, "up", "-d", env=env)
    return env, pf


def down_single(env, pf, sub):
    for v, (svc, _, _) in VERS.items():
        save(f"{sub}/log-{v}.txt", sh("docker", "compose", "logs", "--no-color", svc, env=env, check=False).stdout)
    sh("docker", "compose", *pf, "down", "-v", "--remove-orphans", env=env, check=False)


def failed_view(m):
    """Only the counters that matter, without the zero noise."""
    return {k: v for k, v in m.items() if ("failed" in k or k.startswith("alertmanager_notifications_total")
                                           or k.startswith("alertmanager_notification_requests_total")) and v}


# ---------------------------------------------------------------- delivery
def delivery():
    env, pf = compose_single("./configs/delivery.yml")
    try:
        wait_ready([p for _, p, _ in VERS.values()])
        for v, (_, p, _) in VERS.items():
            print(v, post_alerts(p, [{"alertname": n} for n in ("Wh400", "Wh500", "WhRefused", "Mail554", "WhOk")]))
        timeline = []
        for _ in range(26):  # 約 78 秒，每 3 秒一次
            row = {"t": t()}
            for v, (_, p, _) in VERS.items():
                row[v] = failed_view(metrics(p))
            timeline.append(row)
            time.sleep(3)
        final = {}
        for v, (_, p, fp) in VERS.items():
            final[v] = {
                "healthy": http(f"http://localhost:{p}/-/healthy")[0],
                "ready": http(f"http://localhost:{p}/-/ready")[0],
                "metrics": metrics(p),
                "fake": fake_stats(fp),
            }
        save("delivery/timeline.json", timeline)
        save("delivery/final.json", final)
    finally:
        down_single(env, pf, "delivery")


# ---------------------------------------------------------------- smtp
def smtp():
    env, pf = compose_single("./configs/smtp.yml")
    obs = []

    def snap(label):
        row = {"t": t(), "label": label}
        for v, (_, p, fp) in VERS.items():
            fs = fake_stats(fp)
            row[v] = {"metrics": failed_view(metrics(p)),
                      "hits": {k: x for k, x in fs.get("hits", {}).items() if not k.startswith("t:")},
                      "smtp_events": len(fs.get("smtp", [])), "mails": len(fs.get("mails", [])),
                      "hung": fs.get("hung"),
                      "healthy": http(f"http://localhost:{p}/-/healthy")[0],
                      "ready": http(f"http://localhost:{p}/-/ready")[0]}
        obs.append(row)
        print(json.dumps(row, ensure_ascii=False)[:400])

    try:
        wait_ready([p for _, p, _ in VERS.values()])
        for v, (_, p, _) in VERS.items():
            post_alerts(p, [{"alertname": "Hang", "n": "1"}, {"alertname": "TmplHeader"}, {"alertname": "TmplBody"},
                            {"alertname": "Other", "n": "1"}])
        time.sleep(8)
        snap("first flush done; Hang group should be stuck in email")
        for v, (_, p, _) in VERS.items():  # 同一個 group 加第二條 alert
            post_alerts(p, [{"alertname": "Hang", "n": "1"}, {"alertname": "Hang", "n": "2"}])
        time.sleep(30)
        snap("30s after 2nd alert in Hang group")
        for v, (_, p, _) in VERS.items():  # 其他 group 加一條新 alert
            post_alerts(p, [{"alertname": "Other", "n": "1"}, {"alertname": "Other", "n": "2"}])
        time.sleep(32)
        snap("62s after 2nd alert in Hang group; 32s after new alert in Other group")
        for v, (_, p, _) in VERS.items():
            print(v, "reload", http(f"http://localhost:{p}/-/reload", data=b"", method="POST"))
        time.sleep(6)
        snap("6s after reload")
        for v, (_, p, fp) in VERS.items():  # 關掉卡住的連線
            print(v, "release", http(f"http://localhost:{fp}/release", data=b"", method="POST"))
        time.sleep(6)
        snap("6s after the fake SMTP closed the hung connections")
        mails = {v: fake_stats(fp).get("mails", []) for v, (_, _, fp) in VERS.items()}
        save("smtp/observations.json", obs)
        save("smtp/mails.json", mails)
    finally:
        down_single(env, pf, "smtp")


# ---------------------------------------------------------------- window
def window():
    env, pf = compose_single("./configs/window.yml", profiles=("window",))
    try:
        wait_ready([p for _, p, _ in VERS.values()])
        time.sleep(5)
        for v, (_, p, _) in VERS.items():
            post_alerts(p, [{"alertname": "Wh400"}])
        start = time.time()
        rows = []
        while time.time() - start < 500:
            row = {"t": t()}
            for v, (_, p, _) in VERS.items():
                m = metrics(p)
                row[v] = {k: x for k, x in m.items() if k.startswith("alertmanager_notifications_failed_total")
                          and 'integration="webhook"' in k and x}
            code, txt = http("http://localhost:21090/api/v1/alerts")
            row["alerts"] = [(a["labels"]["alertname"], a["labels"].get("instance"), a["state"])
                             for a in json.loads(txt)["data"]["alerts"]] if code == 200 else txt
            rows.append(row)
            time.sleep(5)
        q = {}
        for name in ("WindowRate", "WindowIncrease"):
            code, txt = http("http://localhost:21090/api/v1/query_range?" + urllib.parse.urlencode({
                "query": f'ALERTS{{alertname="{name}"}}', "start": start, "end": time.time(), "step": "5"}))
            q[name] = json.loads(txt)["data"]["result"] if code == 200 else txt
        code, txt = http("http://localhost:21090/api/v1/query_range?" + urllib.parse.urlencode({
            "query": 'alertmanager_notifications_failed_total{integration="webhook",reason="clientError"}',
            "start": start, "end": time.time(), "step": "1"}))
        q["counter"] = json.loads(txt)["data"]["result"] if code == 200 else txt
        save("window/timeline.json", rows)
        save("window/prometheus.json", q)
    finally:
        down_single(env, pf, "window")


# ---------------------------------------------------------------- ha
def ha():
    import ha_lab
    ha_lab.run(sys.modules[__name__])


if __name__ == "__main__":
    {"delivery": delivery, "smtp": smtp, "window": window, "ha": ha}[sys.argv[1]]()
