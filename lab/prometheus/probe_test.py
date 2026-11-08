#!/usr/bin/env python3
"""Does a probe recorded inside the rule group show the real view that an after-the-fact query cannot?"""
import json, os, time, lab
from lab import sh
name = "probe"; d = f"{lab.RES}/{name}"; os.makedirs(d, exist_ok=True); os.chmod(d, 0o777)
json.dump({"global": {"evaluation_interval": "15s"}, "rule_files": ["/rules.yml"], "scrape_configs": [{"job_name": "slow", "scrape_interval": "120s", "scrape_timeout": "115s", "static_configs": [{"targets": ["t:8000"]}]}]}, open(f"{d}/p.yml", "w"))
json.dump({"groups": [{"name": "g", "rules": [
  {"record": "probe:visible_samples", "expr": 'count_over_time(demo_requests_total{job="slow"}[5m])'},
  {"record": "probe:data_age_seconds", "expr": 'time() - timestamp(demo_requests_total{job="slow"})'},
  {"alert": "Target", "expr": 'rate(demo_requests_total{job="slow"}[5m]) > 1', "for": "3m"}]}]}, open(f"{d}/r.yml", "w"))
lab.down(name); sh("docker", "network", "create", "--label", "mom-lab=1", f"mom-{name}")
lab.run(name, "t", lab.PY, ("python", "/e.py"), ("-v", f"{lab.ROOT}/target/exporter.py:/e.py:ro", "-e", "SERIES=0", "-e", "DELAY=100"))
lab.run(name, "prom", "prom/prometheus:v2.51.2", ("--config.file=/p.yml", "--storage.tsdb.path=/prometheus"), ("-v", f"{d}/p.yml:/p.yml:ro", "-v", f"{d}/r.yml:/rules.yml:ro", "-v", f"mom-{name}-data:/prometheus"))
time.sleep(1260)
Q = lambda e: lab.api(name, "/api/v1/query?query=" + lab.enc(e), role="prom")["data"]["result"]
o = {}
for k, e in {"live_min_visible_15m": "min_over_time(probe:visible_samples[15m])", "live_max_visible_15m": "max_over_time(probe:visible_samples[15m])", "live_max_age_15m": "max_over_time(probe:data_age_seconds[15m])", "live_min_age_15m": "min_over_time(probe:data_age_seconds[15m])",
             "after_min_visible_15m": 'min_over_time(count_over_time(demo_requests_total{job="slow"}[5m])[15m:15s])', "after_max_visible_15m": 'max_over_time(count_over_time(demo_requests_total{job="slow"}[5m])[15m:15s])', "for_resets_15m": 'changes(ALERTS_FOR_STATE{alertname="Target"}[15m])', "firing_points": 'count_over_time(ALERTS{alertname="Target",alertstate="firing"}[15m])'}.items():
    r = Q(e); o[k] = r[0]["value"][1] if r else None
json.dump(o, open(f"{d}/probe.json", "w"), indent=1); print(o); lab.down(name)
