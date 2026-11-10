#!/usr/bin/env python3
"""MongoDB lab 用的小工具：查 Prometheus、直接抓 exporter（透過 Prometheus 容器的 wget），量耗時（主機時鐘）。

  q.py q '<PromQL>'            印出 instant query 結果（每列 labels 與值）
  q.py snap                    每個 member 一列：up、mongodb_up、myState、isWritablePrimary、compat lag、optime lag、heartbeat
  q.py alerts                  印出 component="mongodb_exporter" 的 ALERTS（pending／firing）
  q.py targets                 每個 target 的 job、instance、health、lastScrapeDuration、lastError
  q.py names <job>             這個 job 目前有的 metric 名稱（一行一個）
  q.py fetch <url> [timeout]   在 Prometheus 容器裡 wget 一個 exporter URL，印出耗時（主機時鐘）、行數、mongodb_up 那幾行
"""
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request

PROM = "http://127.0.0.1:32090"
MEMBERS = ["m1:27017", "m2:27017", "m3:27017"]


def get(path, timeout=15):
    with urllib.request.urlopen(PROM + path, timeout=timeout) as r:
        return json.loads(r.read())


def q(expr):
    try:
        return get("/api/v1/query?query=" + urllib.parse.quote(expr))["data"]["result"]
    except Exception as e:  # noqa: BLE001 - a failed query is a result here
        print(f"    （查詢失敗：{e}）")
        return []


def fmt(m):
    return "{" + ", ".join(f'{k}="{v}"' for k, v in sorted(m.items())) + "}"


def cmd_q(expr):
    rows = q(expr)
    print(f"  > {expr}")
    if not rows:
        print("    （空）")
    for r in rows:
        print(f"    {fmt(r['metric'])} {r['value'][1]}")


def by_inst(expr, key="instance"):
    return {r["metric"].get(key): r["value"][1] for r in q(expr)}


def snap():
    cols = [
        ("up", 'up{job="mongodb"}'),
        ("mongodb_up", 'mongodb_up{job="mongodb"}'),
        ("myState", 'mongodb_rs_myState{job="mongodb"}'),
        ("writablePrimary", 'mongodb_ss_repl_isWritablePrimary{job="mongodb"}'),
        ("compat_lag(primary 看)", None),
        ("rs_state label", None),
    ]
    up = by_inst(cols[0][1])
    mu = by_inst(cols[1][1])
    ms = by_inst(cols[2][1])
    wp = by_inst(cols[3][1])
    lag = {}
    for r in q('mongodb_mongod_replset_member_replication_lag{job="mongodb"}'):
        lag.setdefault(r["metric"]["name"], []).append(f'{r["metric"]["instance"].split(":")[0]}:{r["value"][1]}')
    rss = {}
    for r in q('count by (instance, rs_state) (mongodb_ss_uptime{job="mongodb"})'):
        rss[r["metric"]["instance"]] = r["metric"].get("rs_state", "")
    print("    member | up | mongodb_up | rs_myState | isWritablePrimary | compat lag（被誰報、值） | rs_state label")
    for i in MEMBERS:
        print(f"    {i} | {up.get(i, '（無）')} | {mu.get(i, '（無）')} | {ms.get(i, '（無）')} | {wp.get(i, '（無）')} | "
              f"{' '.join(lag.get(i, [])) or '（無）'} | {rss.get(i, '（無）')}")


def alerts():
    rows = q('ALERTS{component="mongodb_exporter"}')
    if not rows:
        print("    （沒有 mongodb_exporter 的 pending／firing 告警）")
    for r in sorted(rows, key=lambda r: (r["metric"]["alertname"], r["metric"].get("instance", ""))):
        m = r["metric"]
        print(f"    {m['alertstate']:7} {m['alertname']} {m.get('instance', m.get('rs_nm', m.get('cluster', '')))}")


def targets():
    for t in get("/api/v1/targets")["data"]["activeTargets"]:
        lb = t["labels"]
        print(f"    {lb['job']:18} {lb['instance']:14} {t['health']:5} {t['lastScrapeDuration']:.3f}s {t.get('lastError', '')[:120]}")


def names(job):
    rows = q('count by (__name__) ({job="%s"})' % job)
    for n in sorted(r["metric"]["__name__"] for r in rows):
        print(n)


def fetch(url, timeout="12"):
    t0 = time.monotonic()
    p = subprocess.run(["docker", "compose", "exec", "-T", "prometheus", "wget", "-q", "-T", timeout, "-O", "-",
                        "--header", "X-Prometheus-Scrape-Timeout-Seconds: 4", url],
                       capture_output=True, text=True)
    dt = time.monotonic() - t0
    lines = [ln for ln in p.stdout.splitlines() if ln and not ln.startswith("#")]
    ups = [ln for ln in lines if ln.startswith("mongodb_up")]
    print(f"    GET {url} -> rc={p.returncode}，{dt:.2f}s（主機時鐘，含 docker exec 約 0.1～0.3s），{len(lines)} 行 sample；"
          f"{' '.join(ups) or '沒有 mongodb_up'} {p.stderr.strip()[:100]}")


if __name__ == "__main__":
    c, args = sys.argv[1], sys.argv[2:]
    {"q": lambda: cmd_q(args[0]), "snap": snap, "alerts": alerts, "targets": targets,
     "names": lambda: names(args[0]), "fetch": lambda: fetch(*args)}[c]()
