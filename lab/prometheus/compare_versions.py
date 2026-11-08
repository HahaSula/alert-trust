#!/usr/bin/env python3
"""Same load at the same time on several versions. 15m blocks so compaction is part of the measurement."""
import json, re, sys, threading, time, lab
from lab import sh, c
from scenarios import Run
VERS = {"cmp251": "prom/prometheus:v2.51.2", "cmp255": "prom/prometheus:v2.55.1", "cmp313": "prom/prometheus:v3.13.5"}
EXTRA = {"tsdb_head_series": 'prometheus_tsdb_head_series{job="prom"}', "compaction_dur_sum": 'prometheus_tsdb_compaction_duration_seconds_sum{job="prom"}', "compaction_count": 'prometheus_tsdb_compaction_duration_seconds_count{job="prom"}',
         "query_samples_rate": 'rate(prometheus_engine_query_samples_total{job="prom"}[1m])', "heap_inuse": 'go_memstats_heap_inuse_bytes{job="prom"}', "alloc_rate": 'rate(go_memstats_alloc_bytes_total{job="prom"}[1m])',
         "wal_disk": 'prometheus_tsdb_wal_storage_size_bytes{job="prom"}', "blocks_bytes": 'prometheus_tsdb_storage_blocks_bytes{job="prom"}', "goroutines": 'go_goroutines{job="prom"}'}
def one(name, image, out):
    r = Run(name, image=image, big=300000, slow_delay=0, prom_args=("--storage.tsdb.min-block-duration=15m", "--storage.tsdb.max-block-duration=15m"))
    r.phase("warm", 600)
    a = time.time(); r.phase("measure", 2400); b = time.time()
    res = {"image": image, "measure": {}}
    import scenarios
    for k, e in {**scenarios.METRICS, **EXTRA}.items():
        e2 = e.replace('container_cpu_cfs_throttled_periods_total', f'container_cpu_cfs_throttled_periods_total{{name="mom-{name}-prom"}}').replace('container_cpu_cfs_periods_total', f'container_cpu_cfs_periods_total{{name="mom-{name}-prom"}}').replace('container_memory_working_set_bytes', f'container_memory_working_set_bytes{{name="mom-{name}-prom"}}')
        try: res["measure"][k] = {x: (round(v, 4) if isinstance(v, float) else v) for x, v in lab.stats(name, e2, a, b).items()}
        except Exception as ex: res["measure"][k] = {"err": str(ex)[:80]}
    res["cpu_seconds_used"] = float(lab.q(name, 'process_cpu_seconds_total{job="prom"}')[0]["value"][1])
    res["disk_mb"] = sh("docker", "exec", c(name, "prom"), "sh", "-c", "du -sm /prometheus /prometheus/wal /prometheus/chunks_head | tr '\\n' ' '", check=False)
    res["rules"] = [(g["name"], round(g["evaluationTime"], 3)) for g in lab.api(name, "/api/v1/rules", role="prom")["data"]["groups"]]
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    sh("docker", "restart", "-t", "0", c(name, "prom")); time.sleep(120)
    log = sh("bash", "-c", f"docker logs --since {since} {c(name, 'prom')} 2>&1")
    res["replay"] = re.findall(r'total_replay_duration=(\S+)', log)
    r.phase("after_restart", 60)
    out[name] = res; sh("bash", "-c", f"docker logs {c(name,'prom')} > {lab.RES}/{name}.log 2>&1", check=False); lab.down(name); print(name, "done", flush=True)
out = {}; ts = []
for n, i in VERS.items():
    t = threading.Thread(target=one, args=(n, i, out)); t.start(); ts.append(t); time.sleep(5)
for t in ts: t.join()
json.dump(out, open(f"{lab.RES}/compare.json", "w"), indent=1); print("compare-done", flush=True)
