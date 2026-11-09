#!/usr/bin/env python3
"""exp-graf-*：Grafana-managed alert rule 在資料來源消失、變慢、Grafana 重啟時，Alertmanager 收到什麼。

python3 run.py            跑全部情境（約 15 分鐘），輸出到 results/run.log 與 results/run.json
結束時一定 docker compose down -v。只用標準函式庫。

這台機器的容器時鐘不可靠，所以只記錄「狀態」與「事件順序」，時間只當參考。
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
GRAF = "http://localhost:26000"
AM = "http://localhost:26093"
FAKE = "http://localhost:26080"
AUTH = "Basic " + base64.b64encode(b"admin:admin").decode()
OUT = os.path.join(HERE, "results")
LOG: list = []
RECORD: list = []
SEEN_HOOKS = {"external": 0, "embedded": 0}
T0 = time.time()

METRICS = re.compile(
    r"^grafana_alerting_(rule_evaluations_total|rule_evaluation_failures_total|rule_evaluation_attempt_failures_total"
    r"|rule_evaluation_attempts_total|schedule_rule_evaluations_missed_total|alerts|sender_sent_total|sender_errors_total"
    r"|sender_dropped_total|notifications_total|notifications_failed_total|rule_evaluation_duration_seconds_(sum|count))\b")


def log(msg: str) -> None:
    line = f"[{time.time() - T0:7.1f}s] {msg}"
    print(line, flush=True)
    LOG.append(line)


def http(url: str, method: str = "GET", body=None, auth: bool = False, timeout: float = 10):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if auth:
        req.add_header("Authorization", AUTH)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode()


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=HERE, check=True, capture_output=True)


def wait_grafana() -> None:
    for _ in range(90):
        try:
            http(GRAF + "/api/health")
            return
        except Exception:
            time.sleep(2)
    raise SystemExit("Grafana did not start")


def rules() -> dict:
    out = {}
    d = http(GRAF + "/api/prometheus/grafana/api/v1/rules", auth=True)
    for g in d["data"]["groups"]:
        for r in g["rules"]:
            out[r["name"]] = {
                "state": r.get("state"), "health": r.get("health"),
                "error": (r.get("lastError") or "")[:120],
                "instances": sorted(f"{a['state']}:{a['labels'].get('alertname')}" for a in r.get("alerts", [])),
            }
    return out


def am_alerts() -> list:
    out = []
    for a in http(AM + "/api/v2/alerts"):
        lab = a["labels"]
        out.append({"alertname": lab.get("alertname"), "rulename": lab.get("rulename"),
                    "state": a["status"]["state"], "startsAt": a["startsAt"], "endsAt": a["endsAt"],
                    "updatedAt": a["updatedAt"]})
    return sorted(out, key=lambda x: (x["alertname"], x["rulename"] or ""))


def metrics() -> dict:
    out = {}
    for line in http(GRAF + "/metrics").splitlines():
        if METRICS.match(line):
            k, v = line.rsplit(" ", 1)
            out[k] = float(v)
    return out


def new_hooks() -> dict:
    hooks = http(FAKE + "/stats")["hooks"]
    out = {}
    for name in SEEN_HOOKS:
        items = hooks.get(name, [])
        fresh = items[SEEN_HOOKS[name]:]
        SEEN_HOOKS[name] = len(items)
        out[name] = [f"{a['status']}:{a['alertname']}" + (f"({a['rulename']})" if a.get("rulename") else "")
                     for msg in fresh for a in msg["alerts"]]
    return out


def endsat_gap(a: dict) -> float:
    """endsAt − updatedAt（秒）：Grafana 設的 EndsAt 比 Alertmanager 收到的時間晚多少。同一台主機的牆鐘。"""
    def ts(s):
        s = re.sub(r"\.(\d{6})\d*", r".\1", s.replace("Z", "+00:00"))
        from datetime import datetime
        return datetime.fromisoformat(s).timestamp()
    return round(ts(a["endsAt"]) - ts(a["updatedAt"]), 1)


def snap(scenario: str, label: str) -> dict:
    try:
        r = rules()
    except Exception as e:  # Grafana restarting
        r = {"_error": str(e)[:80]}
    try:
        am = am_alerts()
    except Exception as e:
        am = [{"_error": str(e)[:80]}]
    try:
        m = metrics()
    except Exception:
        m = {}
    hooks = new_hooks()
    rec = {"t": round(time.time() - T0, 1), "scenario": scenario, "label": label,
           "rules": r, "am": am, "metrics": m, "hooks": hooks}
    RECORD.append(rec)
    short_rules = ", ".join(f"{k}={v['state']}/{v['health']}{'[' + ' '.join(v['instances']) + ']'}"
                            for k, v in r.items() if isinstance(v, dict))
    short_am = ", ".join(f"{a['alertname']}" + (f"({a['rulename']})" if a.get("rulename") else "")
                         + (f" gap={endsat_gap(a)}s" if "endsAt" in a else "") for a in am)
    log(f"{scenario}/{label}\n    grafana: {short_rules or r}\n    external AM active: {short_am}"
        f"\n    hooks: {hooks}")
    return rec


def watch(scenario: str, seconds: int, step: int = 10) -> None:
    end = time.time() + seconds
    i = 0
    while time.time() < end:
        time.sleep(step)
        i += 1
        snap(scenario, f"+{i * step}s")


def metric_lines(prefixes: tuple) -> str:
    m = metrics()
    return "\n".join(f"    {k} {v:g}" for k, v in sorted(m.items()) if k.startswith(prefixes))


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    compose("down", "-v")
    compose("up", "-d")
    try:
        wait_grafana()
        log("Grafana up; set admin_config alertmanagersChoice=all（不設就不會送到外部 Alertmanager）")
        http(GRAF + "/api/v1/ngalert/admin_config", "POST", {"alertmanagersChoice": "all"}, auth=True)

        # S0：等 5 條 rule 都 firing 且外部 Alertmanager 收到
        for _ in range(30):
            time.sleep(5)
            try:
                if len([a for a in am_alerts() if a["state"] == "active"]) >= 5:
                    break
            except Exception:
                pass
        snap("baseline", "ready")
        log("metrics at baseline\n" + metric_lines(("grafana_alerting_alerts", "grafana_alerting_sender_sent")))

        # S1：exporter 停止 → lab_value 消失 → NoData
        http(FAKE + "/ctl?exporter=0", "POST", {})
        log("S1 exporter stopped (lab_value disappears → NoData)")
        watch("nodata", 70)
        http(FAKE + "/ctl?exporter=1", "POST", {})
        log("S1 exporter back")
        watch("nodata-recover", 50)

        # S2：Prometheus 停止 → 查詢失敗 → Error
        compose("stop", "prom")
        log("S2 Prometheus stopped (query error → Error)")
        watch("error", 70)
        log("metrics during error\n" + metric_lines(("grafana_alerting_rule_evaluation", "grafana_alerting_alerts")))
        compose("start", "prom")
        log("S2 Prometheus back")
        watch("error-recover", 60)

        # S3：Grafana 重啟 → state 從 DB 還原？外部 Alertmanager 有沒有收到 resolved？
        before = {a["alertname"]: a["startsAt"] for a in am_alerts()}
        compose("restart", "grafana")
        log("S3 Grafana restarted (SIGTERM, same container and sqlite DB)")
        wait_grafana()
        snap("restart", "health ok")
        watch("restart", 60)
        after = {a["alertname"]: a["startsAt"] for a in am_alerts()}
        log(f"S3 startsAt before restart: {before}\n    S3 startsAt after restart:  {after}")

        # S4：資料來源變慢。rule interval 10s，evaluation_timeout 預設 30s
        http(FAKE + "/ctl?delay=25", "POST", {})
        log("S4a slow datasource: delay=25s (< evaluation_timeout 30s, > interval 10s)")
        watch("slow25", 90, 15)
        log("metrics after delay=25\n" + metric_lines(("grafana_alerting_schedule_rule_evaluations_missed",
                                                       "grafana_alerting_rule_evaluation")))
        log(f"fake api: {http(FAKE + '/stats')['api']}")
        http(FAKE + "/ctl?delay=45", "POST", {})
        log("S4b slow datasource: delay=45s (> evaluation_timeout 30s)")
        watch("slow45", 150, 15)
        log("metrics after delay=45\n" + metric_lines(("grafana_alerting_schedule_rule_evaluations_missed",
                                                       "grafana_alerting_rule_evaluation", "grafana_alerting_alerts")))
        log(f"fake api: {http(FAKE + '/stats')['api']}")
        http(FAKE + "/ctl?delay=0", "POST", {})
        watch("slow-recover", 60, 15)
        log("final metrics\n" + metric_lines(("grafana_alerting_",)))
        logs = subprocess.run(["docker", "compose", "logs", "--no-color", "grafana"], cwd=HERE,
                              capture_output=True, text=True).stdout
        keep = [ln for ln in logs.splitlines()
                if re.search(r"Tick dropped|Failed to evaluate rule|State cache has been initialized|"
                             r"Warming state cache|context deadline|Failed to save alert state", ln)]
        with open(os.path.join(OUT, "grafana-log-excerpt.txt"), "w") as f:
            f.write("\n".join(keep[:400]) + "\n")
    finally:
        with open(os.path.join(OUT, "run.log"), "w") as f:
            f.write("\n".join(LOG) + "\n")
        with open(os.path.join(OUT, "run.json"), "w") as f:
            json.dump(RECORD, f, ensure_ascii=False, indent=1)
        compose("down", "-v")
        left = subprocess.run(["docker", "ps", "-a", "--filter", "name=at-graf-", "-q"],
                              capture_output=True, text=True).stdout.strip()
        print("leftover containers:", left or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
