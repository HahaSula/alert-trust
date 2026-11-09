#!/usr/bin/env python3
"""Thanos 輕量 lab：store 消失、querier 停機、compactor halt。

  python3 lab/thanos/run.py            # 全部情境，結果寫到 lab/thanos/results/
只用標準函式庫。結束時一律 docker compose down -v。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "results")
PROJECT = "at-thanos"
RULE, QUERY, AM, COMPACT = "http://localhost:25093", "http://localhost:25092", "http://localhost:25094", "http://localhost:25096"
LOG: list[dict] = []


def sh(*args: str, check: bool = True) -> str:
    r = subprocess.run(["docker", "compose", "-p", PROJECT, "-f", os.path.join(HERE, "compose.yml"), *args],
                       capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout


def get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            body = r.read().decode()
            return json.loads(body) if body.startswith(("{", "[")) else body
    except urllib.error.HTTPError as e:
        try:
            return {"http_status": e.code, "body": json.loads(e.read().decode())}
        except Exception:  # noqa: BLE001
            return {"http_status": e.code}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def metrics(url: str, names: tuple[str, ...]) -> list[str]:
    text = get(url + "/metrics")
    if not isinstance(text, str):
        return [json.dumps(text)]
    return [l for l in text.splitlines() if l.startswith(names)]


def snapshot(label: str) -> dict:
    rules = get(RULE + "/api/v1/rules")
    rstate = []
    for g in (rules.get("data", {}).get("groups", []) if isinstance(rules, dict) else []):
        for r in g.get("rules", []):
            rstate.append({"group": g["name"], "rule": r.get("name"), "health": r.get("health"),
                           "state": r.get("state"), "lastError": r.get("lastError", "")[:160],
                           "lastEvaluation": r.get("lastEvaluation")})
    am = get(AM + "/api/v2/alerts")
    amstate = [{"alert": a["labels"].get("alertname"), "strategy": a["labels"].get("strategy"),
                "startsAt": a["startsAt"], "endsAt": a["endsAt"], "updatedAt": a["updatedAt"],
                "state": a["status"]["state"]} for a in am] if isinstance(am, list) else am
    q = {}
    for pr in ("false", "true"):
        qs = urllib.parse.urlencode({"query": 'count(up{job="prometheus"})', "partial_response": pr})
        res = get(QUERY + "/api/v1/query?" + qs)
        if isinstance(res, dict):
            res = {k: res.get(k) for k in ("status", "data", "warnings", "error", "http_status", "body") if k in res}
        q["partial_response=" + pr] = res
    snap = {
        "label": label, "wall": time.strftime("%H:%M:%S"),
        "ruler_rules": rstate, "am_alerts": amstate, "query": q,
        "query_store_conns": metrics(QUERY, ("thanos_store_nodes_grpc_connections",)),
        "ruler_metrics": metrics(RULE, ("thanos_rule_evaluation_with_warnings_total",
                                        "prometheus_rule_evaluation_failures_total")),
    }
    LOG.append(snap)
    print(f"[{snap['wall']}] {label}: am={len(amstate) if isinstance(amstate, list) else amstate} "
          f"rules={[(r['group'], r['rule'], r['health']) for r in rstate]}", flush=True)
    return snap


def wait(sec: float) -> None:
    time.sleep(sec)


def make_overlapping_blocks() -> None:
    """兩個時間範圍相同、external labels 相同的 block，compactor 的 pre compaction overlap check 會 halt。"""
    now = int(time.time())
    lines = ["# HELP lab_m lab", "# TYPE lab_m gauge"]
    for t in range(now - 3 * 3600, now - 3 * 3600 + 600, 15):
        lines.append(f'lab_m{{x="1"}} 1 {t}')
    lines.append("# EOF")
    om = os.path.join(OUT, "blocks.om")
    open(om, "w").write("\n".join(lines) + "\n")
    thanos = '"thanos":{"labels":{"cluster":"lab","replica":"x"},"downsample":{"resolution":0},"source":"sidecar"}'
    script = (
        "set -e; mkdir -p /tmp/b; promtool tsdb create-blocks-from openmetrics /in/blocks.om /tmp/b >/dev/null; "
        "U=$(ls /tmp/b | head -1); V=$(echo $U | sed 's/.$/0/'); [ \"$U\" = \"$V\" ] && V=$(echo $U | sed 's/.$/1/'); "
        f"sed -i '$ s/^}}$/,{thanos}}}/' /tmp/b/$U/meta.json; "
        "cp -r /tmp/b/$U /tmp/b/$V; sed -i \"s/$U/$V/g\" /tmp/b/$V/meta.json; "
        "cp -r /tmp/b/$U /tmp/b/$V /bucket/; echo $U $V"
    )
    r = subprocess.run(["docker", "run", "--rm", "-u", "0", "--entrypoint", "sh",
                        "-v", f"{PROJECT}_bucket:/bucket", "-v", f"{OUT}:/in:ro",
                        "prom/prometheus:v3.13.5", "-c", script], capture_output=True, text=True)
    LOG.append({"label": "blocks-created", "stdout": r.stdout.strip(), "stderr": r.stderr.strip()[-400:]})
    print("blocks:", r.stdout.strip(), r.stderr.strip()[-200:])
    os.remove(om)


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()
    try:
        sh("up", "-d")
        wait(75)
        snapshot("baseline")

        # 1. 只有 sidecar 消失（store gateway 還在，但沒有涵蓋最近時間的 block）
        sh("stop", "sidecar")
        wait(2); snapshot("sidecar-stopped+2s")
        wait(20); snapshot("sidecar-stopped+22s")
        wait(40); snapshot("sidecar-stopped+62s")

        # 2. sidecar 與 store 都消失：querier 沒有任何 StoreAPI
        sh("stop", "store")
        wait(25); snapshot("no-stores+25s")
        wait(40); snapshot("no-stores+65s")

        sh("start", "sidecar", "store")
        wait(45); snapshot("stores-back+45s")

        # 3. querier 停機：ruler 所有查詢失敗
        sh("stop", "query")
        for i in range(1, 9):
            wait(15); snapshot(f"query-stopped+{15 * i}s")
        sh("start", "query")
        wait(45); snapshot("query-back+45s")

        # 4. compactor 遇到重疊的 block
        make_overlapping_blocks()
        sh("--profile", "compact", "up", "-d", "compact")
        wait(60)
        LOG.append({"label": "compact+60s", "wall": time.strftime("%H:%M:%S"),
                    "metrics": metrics(COMPACT, ("thanos_compact_halted", "thanos_compact_iterations_total",
                                                 "thanos_compact_group_compactions_failures_total",
                                                 "thanos_compact_todo_compactions")),
                    "logs": [l for l in sh("logs", "compact", check=False).splitlines()
                             if "halt" in l or "overlap" in l][-3:]})
        st = sh("ps", "--format", "{{.Service}} {{.State}}", "compact", check=False).strip()
        LOG[-1]["compact_state"] = st
        print(json.dumps(LOG[-1], ensure_ascii=False, indent=1)[:1500])
    finally:
        sh("--profile", "compact", "down", "-v", check=False)
        with open(os.path.join(OUT, "run.json"), "w") as f:
            json.dump({"duration_s": round(time.time() - t0), "snapshots": LOG}, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
