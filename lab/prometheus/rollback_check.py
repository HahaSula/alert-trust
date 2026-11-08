#!/usr/bin/env python3
"""Write with one version, read with another; compare sample counts in the time range written before the switch."""
import json, time, lab, threading
from lab import sh
def Q(n, e, t=None):
    p = "/api/v1/query?query=" + lab.enc(e) + (f"&time={t}" if t else "")
    return json.loads(sh("docker", "exec", f"{n}-p", "wget", "-qO-", f"http://localhost:9090{p}"))["data"]["result"]
CFG = f"{lab.RES}/upgrade/p.yml"; RULES = f"{lab.RES}/upgrade/r.yml"; out = {}
def prom(n, image, vol, *x):
    sh("docker", "rm", "-f", f"{n}-p", check=False)
    sh("docker", "run", "-d", "--name", f"{n}-p", "--label", "mom-lab=1", "--network", n, "-v", f"{CFG}:/p.yml:ro", "-v", f"{RULES}:/rules.yml:ro", "-v", f"{vol}:/prometheus", image, "--config.file=/p.yml", "--storage.tsdb.path=/prometheus", *x)
def run(tag, a, b):
    n = f"momrb-{tag}"; vol = f"{n}-v"; sh("docker", "network", "create", "--label", "mom-lab=1", n, check=False); sh("docker", "volume", "rm", vol, check=False)
    sh("docker", "run", "-d", "--name", f"{n}-t", "--label", "mom-lab=1", "--network", n, "--network-alias", "t", "-v", f"{lab.ROOT}/target/exporter.py:/e.py:ro", "-e", "SERIES=500", lab.PY, "python", "/e.py")
    prom(n, a, vol, "--storage.tsdb.min-block-duration=5m", "--storage.tsdb.max-block-duration=5m"); time.sleep(660)
    t = time.time() - 30; E = 'sum(count_over_time(demo_filler[11m]))'
    before = Q(n, E, t)[0]["value"][1]; blocks = Q(n, "prometheus_tsdb_blocks_loaded")[0]["value"][1]
    sh("docker", "stop", "-t", "20", f"{n}-p"); prom(n, b, vol); time.sleep(45)
    st = sh("docker", "inspect", "-f", "{{.State.Status}}", f"{n}-p")
    r = {"from": a, "to": b, "blocks_before": blocks, "samples_before": before, "status": st}
    if st == "running":
        r["samples_after"] = Q(n, E, t)[0]["value"][1]; r["blocks_after"] = Q(n, "prometheus_tsdb_blocks_loaded")[0]["value"][1]; r["corruptions"] = Q(n, "prometheus_tsdb_wal_corruptions_total")[0]["value"][1]
        lg = sh("bash", "-c", f"docker logs {n}-p 2>&1 | grep -iE 'level=(warn|error)' | cut -c1-200 | head -5", check=False); r["warnings"] = lg
    out[tag] = r; print(tag, r, flush=True)
    sh("docker", "rm", "-f", f"{n}-p", f"{n}-t", check=False); sh("docker", "volume", "rm", vol, check=False); sh("docker", "network", "rm", n, check=False)
ts = [threading.Thread(target=run, args=x) for x in [("313to251", "prom/prometheus:v3.13.5", "prom/prometheus:v2.51.2"), ("313to255", "prom/prometheus:v3.13.5", "prom/prometheus:v2.55.1"), ("251to313", "prom/prometheus:v2.51.2", "prom/prometheus:v3.13.5")]]
for t in ts: t.start()
for t in ts: t.join()
json.dump(out, open(f"{lab.RES}/upgrade/rollback.json", "w"), indent=1); print("rollback-done", flush=True)
