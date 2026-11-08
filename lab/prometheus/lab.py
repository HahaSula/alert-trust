#!/usr/bin/env python3
"""Fault-injection lab helpers. Stacks are named mom-<name>-*, one isolated docker network per stack."""
import json, os, subprocess, sys, time
ROOT = os.path.dirname(os.path.abspath(__file__)); RES = os.path.join(ROOT, "results")
PY = "python:3.12-alpine"; AM = "prom/alertmanager:v0.27.0"
def sh(*a, check=True, cap=True):
    r = subprocess.run(a, capture_output=cap, text=True)
    if check and r.returncode: raise RuntimeError(f"{a}: {r.stderr}")
    return r.stdout.strip() if cap else ""
def c(name, role): return f"mom-{name}-{role}"
def run(name, role, image, args=(), docker=(), alias=None):
    sh("docker", "run", "-d", "--name", c(name, role), "--network", f"mom-{name}", "--network-alias", alias or role,
       "--label", "mom-lab=1", *docker, image, *args)
def up(name, image="prom/prometheus:v2.51.2", prom_args=(), docker=(), big=100000, data=None, query_offset=False, slow_delay=100, block_rules=False):
    os.makedirs(f"{RES}/{name}", exist_ok=True); os.chmod(f"{RES}/{name}", 0o777)
    sh("docker", "network", "create", "--label", "mom-lab=1", f"mom-{name}")
    ro = lambda s, d: ("-v", f"{ROOT}/{s}:{d}:ro")
    for role, ser, dly in (("target-small", 0, 0), ("target-big", big, 0), ("target-slow", 0, slow_delay)):
        run(name, role, PY, ("python", "/e.py"), (*ro("target/exporter.py", "/e.py"), "-e", f"SERIES={ser}", "-e", f"DELAY={dly}"))
    run(name, "sink", PY, ("python", "/s.py"), (*ro("sink/sink.py", "/s.py"), "-v", f"{RES}/{name}:/out"))
    run(name, "am", AM, ("--config.file=/c.yml",), ro("config/alertmanager.yml", "/c.yml"))
    vol = data or f"mom-{name}-data"
    extra = list(ro("config/query-offset.yml", "/etc/prometheus/rules/query-offset.yml")) if query_offset else []
    run(name, "prom", image, ("--config.file=/etc/prometheus/prometheus.yml", "--storage.tsdb.path=/prometheus", "--web.enable-lifecycle", *prom_args),
        (*ro("config/prometheus.yml", "/etc/prometheus/prometheus.yml"), *ro("config/rules/canary.yml", "/etc/prometheus/rules/canary.yml"),
         *extra, "-v", f"{vol}:/prometheus", "--label", "mom-lab=1", "--restart", "always", *docker))
    blockr = list(ro("config/meta-block-rules.yml", "/etc/meta/block-rules.yml")) if block_rules else []
    run(name, "meta", "prom/prometheus:v2.51.2", ("--config.file=/etc/meta/meta.yml", "--storage.tsdb.path=/prometheus"),
        (*ro("config/meta.yml", "/etc/meta/meta.yml"), *ro("config/meta-rules.yml", "/etc/meta/meta-rules.yml"), *blockr))
    ensure_cadvisor(name)
def ensure_cadvisor(name):
    if not sh("docker", "ps", "-q", "--filter", "name=^mom-cadvisor$"):
        sh("docker", "rm", "-f", "mom-cadvisor", check=False)
        sh("docker", "run", "-d", "--name", "mom-cadvisor", "--label", "mom-lab=1", "--privileged", "-v", "/:/rootfs:ro", "-v", "/var/run:/var/run:ro",
           "-v", "/sys:/sys:ro", "-v", "/var/lib/docker/:/var/lib/docker:ro", "gcr.io/cadvisor/cadvisor:v0.49.1", "--docker_only=true", "--housekeeping_interval=5s",
           "--disable_metrics=percpu,sched,tcp,udp,disk,referenced_memory,process,hugetlb")
    sh("docker", "network", "connect", "--alias", "cadvisor", f"mom-{name}", "mom-cadvisor", check=False)
def set_iops(name, wiops="max", wbps="max"):
    cid = sh("docker", "inspect", "-f", "{{.Id}}", c(name, "prom"))
    sh("docker", "run", "--rm", "--privileged", "-v", "/sys/fs/cgroup:/cg", "python:3.12-alpine", "sh", "-c", f'echo "8:0 wiops={wiops} wbps={wbps}" > /cg/docker/{cid}/io.max; cat /cg/docker/{cid}/io.max')
def down(name):
    ids = sh("docker", "ps", "-aq", "--filter", f"name=^mom-{name}-").split()
    if ids: pass
    if ids: sh("docker", "rm", "-f", *ids, check=False)
    sh("docker", "network", "disconnect", "-f", f"mom-{name}", "mom-cadvisor", check=False)
    sh("docker", "volume", "rm", f"mom-{name}-data", check=False); sh("docker", "network", "rm", f"mom-{name}", check=False)
def api(name, path, role="meta", port=9090):
    return json.loads(sh("docker", "exec", c(name, role), "wget", "-qO-", f"http://localhost:{port}{path}"))
def enc(s):
    from urllib.parse import quote; return quote(s, safe="")
def q(name, expr, t=None):
    return api(name, f"/api/v1/query?query={enc(expr)}" + (f"&time={t}" if t else ""))["data"]["result"]
def qr(name, expr, s, e, step=5):
    return api(name, f"/api/v1/query_range?query={enc(expr)}&start={s}&end={e}&step={step}")["data"]["result"]
def stats(name, expr, s, e, step=5):
    vals = [float(v) for r in qr(name, expr, s, e, step) for _, v in r["values"] if v not in ("NaN", "+Inf", "-Inf")]
    return {"n": len(vals), "min": min(vals), "avg": sum(vals) / len(vals), "max": max(vals)} if vals else {"n": 0}
def wait(sec, msg=""):
    print(f"[{time.strftime('%H:%M:%S')}] wait {sec}s {msg}", flush=True); time.sleep(sec)
ALERT_FIRST = 'ALERTS{job!~"federate|small|big|slow|prometheus",alertstate="firing",alertname=~"M_.*"}'
def alerts(name, s, e):
    """First firing time of every M_* alert within [s,e], relative to s."""
    out = {}
    for r in qr(name, 'ALERTS{alertname=~"M_.*",alertstate="firing"}', s, e, 5):
        if r["metric"].get("name", f"mom-{name}-prom") != f"mom-{name}-prom": continue
        a = r["metric"]["alertname"]; t = float(r["values"][0][0]) - s
        out[a] = min(out.get(a, 1e9), round(t))
    return dict(sorted(out.items(), key=lambda x: x[1]))
def sink_gaps(name, s, e, names=("Watchdog", "DataHeartbeat", "KnownTrueCanary")):
    path = f"{RES}/{name}/notifications.jsonl"; res = {}
    if not os.path.exists(path): return res
    rows = [json.loads(l) for l in open(path)]
    for n in names:
        ts = [r["t"] for r in rows if r["alertname"] == n and r["status"] == "firing" and s <= r["t"] <= e]
        res[n] = {"count": len(ts), "max_gap_s": round(max([b - a for a, b in zip([s] + ts, ts + [e])]))}
    return res
def save(name, obj):
    with open(f"{RES}/{name}/summary.json", "w") as f: json.dump(obj, f, indent=1, ensure_ascii=False)
    sh("bash", "-c", f"docker logs {c(name,'prom')} > {RES}/{name}/prom.log 2>&1", check=False)
