"""依 combos 產生 prometheus.yml 與 rules.yml。"""
import os
from combos import COMBOS, E

def dur(s): return f"{s // 60}m" if s % 60 == 0 else f"{s}s"

out = os.path.dirname(os.path.abspath(__file__))
scrape = [f"global: {{ evaluation_interval: {E}s }}", "rule_files: [/etc/prometheus/rules.yml]", "scrape_configs:"]
rules = ["groups:"]
for cid, S, d, R, O, F in COMBOS:
    job = cid
    scrape += [f"  - job_name: {job}", f"    scrape_interval: {S}s", f"    scrape_timeout: {S}s",
               f"    params: {{ delay: ['{d}'] }}", "    static_configs: [{ targets: ['exporter:8000'] }]"]
    off = f" offset {dur(O)}" if O else ""
    sel = f'demo_requests_total{{job="{job}"}}[{dur(R)}]{off}'
    rules += [f"  - name: {job}", f"    interval: {E}s", "    rules:",
              f"      - record: probe:visible_samples", f"        expr: count_over_time({sel})",
              f"        labels: {{ combo: {job} }}",
              f"      - alert: KnownTrue_{job.replace('-', '_')}", f"        expr: rate({sel}) >= 0",
              f"        for: {dur(F)}", f"        labels: {{ combo: {job} }}"]
open(os.path.join(out, "prometheus.yml"), "w").write("\n".join(scrape) + "\n")
open(os.path.join(out, "rules.yml"), "w").write("\n".join(rules) + "\n")
print(f"{len(COMBOS)} combos")
