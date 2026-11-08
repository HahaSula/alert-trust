#!/usr/bin/env python3
"""What does an external blackbox probe of Prometheus and Alertmanager see during each fault?"""
import json, os, subprocess, threading, time, urllib.parse, lab
from lab import sh, c
n = "bbox"; VOL = f"mom-{n}-tv"
lab.down(n); sh("docker", "rm", "-f", c(n, "holder"), check=False); sh("docker", "volume", "rm", VOL, check=False)
sh("docker", "volume", "create", "--driver", "local", "--opt", "type=tmpfs", "--opt", "device=tmpfs", "--opt", "o=size=2g,uid=65534,gid=65534,mode=0777", VOL)
sh("docker", "run", "-d", "--name", c(n, "holder"), "--label", "mom-lab=1", "-v", f"{VOL}:/prometheus", "python:3.12-alpine", "sleep", "86400")
lab.up(n, big=150000, slow_delay=0, data=VOL, prom_args=("--query.max-concurrency=2", "--query.timeout=20s"), docker=("--cpus", "2", "--memory", "4g", "--memory-swap", "4g"))
sh("docker", "run", "-d", "--name", c(n, "bb"), "--label", "mom-lab=1", "--network", f"mom-{n}", "--network-alias", "bb", "-v", f"{lab.ROOT}/config/blackbox.yml:/b.yml:ro", "prom/blackbox-exporter:v0.25.0", "--config.file=/b.yml")
P = {"prom_healthy": "http://prom:9090/-/healthy", "prom_ready": "http://prom:9090/-/ready", "prom_query": "http://prom:9090/api/v1/query?query=vector(1)", "am_healthy": "http://am:9093/-/healthy", "am_ready": "http://am:9093/-/ready", "am_status": "http://am:9093/api/v2/status"}
samples = {k: [] for k in P}; stop = threading.Event(); lock = threading.Lock()
def probe(name, url):
    while not stop.is_set():
        t = time.time(); u = "http://localhost:9115/probe?module=http_2xx&target=" + urllib.parse.quote(url, safe="")
        r = subprocess.run(["docker", "exec", c(n, "bb"), "wget", "-qO-", "-T", "8", u], capture_output=True, text=True).stdout
        ok, dur = None, None
        for l in r.splitlines():
            if l.startswith("probe_success "): ok = float(l.split()[1])
            if l.startswith("probe_duration_seconds "): dur = float(l.split()[1])
        with lock: samples[name].append((t, ok if ok is not None else 0.0, dur or 0.0))
        time.sleep(max(0, 15 - (time.time() - t)))
for k, v in P.items(): threading.Thread(target=probe, args=(k, v), daemon=True).start()
phases = []; ev = {}
def ph(label, sec, act=None):
    if act: act()
    a = time.time(); lab.wait(sec, label); phases.append((label, a, time.time()))
def cpus(v): return lambda: sh("docker", "update", "--cpus", v, c(n, "prom"))
def mem(v): return lambda: sh("docker", "update", "--memory", v, "--memory-swap", v, c(n, "prom"), check=False)
stopc = lambda x: (lambda: sh("docker", "stop", "-t", "1", c(n, x)))
startc = lambda x: (lambda: sh("docker", "start", c(n, x)))
ph("base", 240)
ph("cpu_0.1", 300, cpus("0.1")); ph("rec_cpu", 120, cpus("2"))
hstop = threading.Event()
def hammer():
    q = lab.enc("sum by (id) (avg_over_time(demo_filler[5m]))")
    while not hstop.is_set():
        subprocess.run(["docker", "exec", c(n, "meta"), "wget", "-qO-", "-T", "30", f"http://prom:9090/api/v1/query?query={q}"], capture_output=True)
ph("query_saturated", 240, lambda: [threading.Thread(target=hammer, daemon=True).start() for _ in range(6)]); hstop.set(); ph("rec_query", 90)
ph("target_down", 180, stopc("target-small")); ph("rec_target", 90, startc("target-small"))
ph("receiver_down", 240, stopc("sink")); ph("rec_receiver", 90, startc("sink"))
ph("am_down", 180, stopc("am")); ph("rec_am", 120, startc("am"))
ws = float(lab.q(n, f'container_memory_working_set_bytes{{name="mom-{n}-prom"}}')[0]["value"][1])
ph("oom_crash_loop", 300, mem(f"{int(ws * .7 / 1e6)}m")); ev["restarts"] = sh("docker", "inspect", "-f", "{{.RestartCount}}", c(n, "prom")); ph("rec_oom", 180, mem("4g"))
ph("disk_full", 300, lambda: sh("docker", "exec", c(n, "holder"), "sh", "-c", "dd if=/dev/zero of=/prometheus/filler bs=1M 2>/dev/null; true", check=False))
ph("rec_disk", 120, lambda: sh("docker", "exec", c(n, "holder"), "rm", "-f", "/prometheus/filler"))
def prom_stop():
    sh("docker", "update", "--restart", "no", c(n, "prom")); sh("docker", "stop", "-t", "1", c(n, "prom"))
ph("prom_down", 120, prom_stop); ph("rec_prom", 120, startc("prom"))
stop.set(); time.sleep(10)
out = {}
for lb, a, b in phases:
    row = {}
    for k, v in samples.items():
        rs = [x for x in v if a + 20 <= x[0] <= b]
        row[k] = {"probes": len(rs), "success": sum(1 for x in rs if x[1] == 1), "max_s": round(max([x[2] for x in rs] or [0]), 2)}
    out[lb] = {"dur": round(b - a), "probes": row}
    for e, name in {"alerts": None}.items():
        out[lb]["meta_alerts"] = lab.alerts(n, a, b); out[lb]["sink"] = lab.sink_gaps(n, a, b)
json.dump({"phases": out, "events": ev}, open(f"{lab.RES}/bbox/result.json", "w"), indent=1, ensure_ascii=False)
lab.down(n); sh("docker", "rm", "-f", c(n, "holder"), check=False); sh("docker", "volume", "rm", VOL, check=False); print("bbox-done", flush=True)
