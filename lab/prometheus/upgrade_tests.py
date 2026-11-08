#!/usr/bin/env python3
"""A: can an older version open a TSDB written by a newer one?  B: how long does alert restore block a group?"""
import json, os, re, subprocess, time, lab
from lab import sh
OUT = f"{lab.RES}/upgrade"; os.makedirs(OUT, exist_ok=True); os.chmod(OUT, 0o777)
res = {"downgrade": {}, "restore": {}}
CFG = f"{OUT}/p.yml"
json.dump({"global": {"scrape_interval": "15s", "evaluation_interval": "15s"}, "rule_files": ["/rules.yml"], "scrape_configs": [{"job_name": "t", "static_configs": [{"targets": ["t:8000"]}]}, {"job_name": "self", "static_configs": [{"targets": ["localhost:9090"]}]}]}, open(CFG, "w"))
RULES = f"{OUT}/r.yml"
json.dump({"groups": [{"name": "many", "rules": [{"alert": "ManyInstances", "expr": "demo_filler >= 0", "for": "30m"}]}]}, open(RULES, "w"))
def net(n): sh("docker", "network", "create", "--label", "mom-lab=1", n, check=False)
def target(n, series):
    sh("docker", "rm", "-f", f"{n}-t", check=False)
    sh("docker", "run", "-d", "--name", f"{n}-t", "--label", "mom-lab=1", "--network", n, "--network-alias", "t", "-v", f"{lab.ROOT}/target/exporter.py:/e.py:ro", "-e", f"SERIES={series}", lab.PY, "python", "/e.py")
def prom(n, image, vol, *extra):
    sh("docker", "rm", "-f", f"{n}-p", check=False)
    sh("docker", "run", "-d", "--name", f"{n}-p", "--label", "mom-lab=1", "--network", n, "-v", f"{CFG}:/p.yml:ro", "-v", f"{RULES}:/rules.yml:ro", "-v", f"{vol}:/prometheus", image, "--config.file=/p.yml", "--storage.tsdb.path=/prometheus", *extra)
def api(n, path):
    return json.loads(sh("docker", "exec", f"{n}-p", "wget", "-qO-", f"http://localhost:9090{path}"))
def state(n):
    time.sleep(40)
    run = sh("docker", "inspect", "-f", "{{.State.Status}} exit={{.State.ExitCode}}", f"{n}-p")
    logs = subprocess.run(["docker", "logs", f"{n}-p"], capture_output=True, text=True); text = logs.stdout + logs.stderr
    errs = [re.sub(r'^ts=\S+ ', '', l)[:230] for l in text.splitlines() if re.search(r'level=(error|ERROR)|unknown index|opening storage failed', l)][:4]
    blocks = None
    if run.startswith("running"):
        try: blocks = api(n, "/api/v1/query?query=prometheus_tsdb_blocks_loaded")["data"]["result"]
        except Exception: pass
    return {"status": run, "blocks": blocks[0]["value"][1] if blocks else None, "errors": errs}
# ---- A: build a TSDB on one version, then open it with another ----
def chain(tag, first, seconds):
    n = f"momup-{tag}"; vol = f"{n}-v"; net(n); sh("docker", "volume", "rm", vol, check=False); target(n, 500)
    prom(n, first, vol, "--storage.tsdb.min-block-duration=5m", "--storage.tsdb.max-block-duration=5m"); time.sleep(600)
    b = sh("docker", "exec", f"{n}-p", "sh", "-c", "ls -d /prometheus/01* | wc -l")
    sh("docker", "exec", f"{n}-p", "sh", "-c", "true", check=False)
    ver = sh("docker", "exec", f"{n}-p", "sh", "-c", "for f in /prometheus/01*/index; do head -c 5 $f | od -An -tu1; done | sort -u", check=False)
    entry = {"blocks_written": b, "index_header_bytes": ver}; sh("docker", "stop", "-t", "20", f"{n}-p")
    for second in seconds:
        tmp = f"{n}-copy-{second.split(':v')[1].replace('.', '')}"
        sh("docker", "volume", "rm", tmp, check=False); sh("docker", "volume", "create", tmp)
        sh("docker", "run", "--rm", "-v", f"{vol}:/a:ro", "-v", f"{tmp}:/b", lab.PY, "sh", "-c", "cp -a /a/. /b/ && chmod -R a+rwX /b")
        prom(n, second, tmp); entry[second] = state(n); sh("docker", "rm", "-f", f"{n}-p", check=False); sh("docker", "volume", "rm", tmp, check=False)
    res["downgrade"][first] = entry
    sh("docker", "rm", "-f", f"{n}-t", check=False); sh("docker", "volume", "rm", vol, check=False); sh("docker", "network", "rm", n, check=False)
def restore(version, instances=30000, rounds=2):
    tag = version.replace(".", ""); n = f"momre-{tag}"; vol = f"{n}-v"; net(n); sh("docker", "volume", "rm", vol, check=False); target(n, instances)
    prom(n, f"prom/prometheus:v{version}", vol, "--log.level=debug"); time.sleep(150)
    out = {"active": len(api(n, "/api/v1/alerts")["data"]["alerts"]), "rounds": []}
    for i in range(rounds):
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        sh("docker", "restart", "-t", "20", f"{n}-p"); time.sleep(70)
        logs = subprocess.run(["docker", "logs", "--since", since, f"{n}-p"], capture_output=True, text=True); text = logs.stdout + logs.stderr
        from datetime import datetime
        ts = [re.search(r'(?:ts|time)=(\S+)', l).group(1) for l in text.splitlines() if "state restored" in l and re.search(r'(?:ts|time)=(\S+)', l)]
        parse = lambda x: datetime.fromisoformat(x.replace("Z", "+00:00"))
        row = {"restored_lines": len(ts), "span_s": round((parse(ts[-1]) - parse(ts[0])).total_seconds(), 3) if len(ts) > 1 else None}
        m = api(n, "/api/v1/query?query=prometheus_rule_group_last_restore_duration_seconds")["data"]["result"]
        row["metric_s"] = m[0]["value"][1] if m else None
        row["alerts_after"] = len(api(n, "/api/v1/alerts")["data"]["alerts"]); out["rounds"].append(row); print(version, row, flush=True)
        time.sleep(30)
    res["restore"][version] = out
    sh("docker", "rm", "-f", f"{n}-p", f"{n}-t", check=False); sh("docker", "volume", "rm", vol, check=False); sh("docker", "network", "rm", n, check=False)
import threading
ts = [threading.Thread(target=chain, args=("v313", "prom/prometheus:v3.13.5", ["prom/prometheus:v2.51.2", "prom/prometheus:v2.55.1"])),
      threading.Thread(target=chain, args=("v251", "prom/prometheus:v2.51.2", ["prom/prometheus:v2.55.1", "prom/prometheus:v3.13.5"])),
      threading.Thread(target=chain, args=("v255", "prom/prometheus:v2.55.1", ["prom/prometheus:v2.51.2", "prom/prometheus:v3.13.5"]))]
for t in ts: t.start()
for v in ("2.51.2", "2.55.1", "3.13.5"): restore(v)
for t in ts: t.join()
json.dump(res, open(f"{OUT}/result.json", "w"), indent=1, ensure_ascii=False); print("upgrade-done", flush=True)
