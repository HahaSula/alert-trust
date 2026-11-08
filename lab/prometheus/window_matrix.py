#!/usr/bin/env python3
"""Which (commit lag d, range R, offset O) combinations keep a known-true alert firing? One always-true alert per combination."""
import json, os, sys, time, lab
from lab import sh, c
name, image = sys.argv[1], sys.argv[2]
S, DS, RS, OS = 60, (5, 20, 40, 55), (2, 3, 5), (0, 1)
d = f"{lab.RES}/{name}"; os.makedirs(d, exist_ok=True); os.chmod(d, 0o777)
cfg = {"global": {"scrape_interval": "15s", "evaluation_interval": "15s"}, "rule_files": ["/rules.yml"], "scrape_configs": [{"job_name": f"d{x}", "scrape_interval": f"{S}s", "scrape_timeout": f"{S-1}s", "static_configs": [{"targets": [f"t{x}:8000"]}]} for x in DS]}
rules = []
for x in DS:
    for R in RS:
        for O in OS:
            off = f" offset {O}m" if O else ""
            rules.append({"alert": f"D{x}_R{R}m_O{O}m", "expr": f'rate(demo_requests_total{{job="d{x}"}}[{R}m]{off}) > 1', "for": "3m"})
json.dump(cfg, open(f"{d}/p.yml", "w")); json.dump({"groups": [{"name": "matrix", "rules": rules}]}, open(f"{d}/r.yml", "w"))
lab.down(name); sh("docker", "network", "create", "--label", "mom-lab=1", f"mom-{name}")
for x in DS:
    lab.run(name, f"t{x}", lab.PY, ("python", "/e.py"), ("-v", f"{lab.ROOT}/target/exporter.py:/e.py:ro", "-e", "SERIES=0", "-e", f"DELAY={x}"))
lab.run(name, "prom", image, ("--config.file=/p.yml", "--storage.tsdb.path=/prometheus"), ("-v", f"{d}/p.yml:/p.yml:ro", "-v", f"{d}/r.yml:/rules.yml:ro", "-v", f"mom-{name}-data:/prometheus"))
t0 = time.time(); time.sleep(600); t1 = time.time(); time.sleep(1500); t2 = time.time()
out = {}
Q = lambda e: lab.api(name, "/api/v1/query?query=" + lab.enc(e), role="prom")["data"]["result"]
for row in Q("changes(ALERTS_FOR_STATE[24m])"): out.setdefault(row["metric"]["alertname"], {})["resets_24m"] = float(row["value"][1])
for row in Q('count_over_time(ALERTS{alertstate="firing"}[24m])'): out.setdefault(row["metric"]["alertname"], {})["firing_points_24m"] = int(row["value"][1])
for row in Q('count_over_time(ALERTS{alertstate="pending"}[24m])'): out.setdefault(row["metric"]["alertname"], {})["pending_points_24m"] = int(row["value"][1])
for x in DS:
    r = Q(f'scrape_duration_seconds{{job="d{x}"}}'); out[f"_d{x}"] = {"scrape_duration": r[0]["value"][1] if r else None}
    r = Q(f'count_over_time(up{{job="d{x}"}}[24m])'); out[f"_d{x}"]["scrapes_24m"] = r[0]["value"][1] if r else None
    r = Q(f'min_over_time((count_over_time(demo_requests_total{{job="d{x}"}}[1m]))[1m:])'); 
json.dump(out, open(f"{d}/matrix.json", "w"), indent=1); lab.down(name); print(name, "done")
