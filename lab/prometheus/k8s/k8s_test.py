#!/usr/bin/env python3
"""Validate signals and rule labels on kube-prometheus-stack with Prometheus 2.51.2."""
import json, os, re, subprocess, sys, time, urllib.parse, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__)); OUT = os.path.join(HERE, "..", "results", "k8s"); os.makedirs(OUT, exist_ok=True)
SK = sys.argv[1]; NS = "monitoring"; REL = "kps"
def sh(*a, check=True, t=900):
    r = subprocess.run(a, capture_output=True, text=True, timeout=t)
    if check and r.returncode: raise RuntimeError(f"{a}: {r.stderr[-800:]}")
    return r.stdout.strip()
def log(m): print(time.strftime("[%H:%M:%S] ") + str(m), flush=True)
res = {}
sh("helm", "upgrade", "--install", REL, sys.argv[2], "-n", NS, "--create-namespace", "-f", os.path.join(HERE, "values.yaml"), "--wait", "--timeout", "12m"); log("helm ok")
meta = open(os.path.join(SK, "mom-coverage/references/meta-alerts.yml"), encoding="utf8").read()
body = "\n".join("    " + l if False else l for l in meta.splitlines() if not l.startswith("#"))
pr = "apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\nmetadata:\n  name: selfcheck-meta\n  namespace: monitoring\nspec:\n" + "\n".join("  " + l for l in body.splitlines()) + "\n"
open(os.path.join(HERE, "rule.yaml"), "w", encoding="utf8").write(pr)
exporter = open(os.path.join(HERE, "..", "target", "exporter.py")).read()
sh("kubectl", "-n", NS, "create", "configmap", "demo-exp", "--from-literal=e.py=" + exporter, check=False)
app = """apiVersion: apps/v1
kind: Deployment
metadata: { name: demo, namespace: monitoring, labels: { app: demo } }
spec:
  replicas: 2
  selector: { matchLabels: { app: demo } }
  template:
    metadata: { labels: { app: demo } }
    spec:
      containers:
      - name: e
        image: python:3.12-alpine
        command: [python, /c/e.py]
        env: [{ name: SERIES, value: "150000" }]
        ports: [{ name: m, containerPort: 8000 }]
        volumeMounts: [{ name: c, mountPath: /c }]
      volumes: [{ name: c, configMap: { name: demo-exp } }]
---
apiVersion: v1
kind: Service
metadata: { name: demo, namespace: monitoring, labels: { app: demo } }
spec: { selector: { app: demo }, ports: [{ name: m, port: 8000 }] }
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata: { name: demo, namespace: monitoring, labels: { app: demo } }
spec:
  selector: { matchLabels: { app: demo } }
  endpoints: [{ port: m, interval: 15s }]
"""
open(os.path.join(HERE, "app.yaml"), "w").write(app)
sh("kubectl", "apply", "-f", os.path.join(HERE, "app.yaml")); sh("kubectl", "apply", "-f", os.path.join(HERE, "rule.yaml"))
sh("kubectl", "-n", NS, "rollout", "status", "deploy/demo", "--timeout=300s"); log("app ok")
svc = sh("kubectl", "-n", NS, "get", "svc", "-l", "app=kube-prometheus-stack-prometheus,self-monitor=true", "-o", "jsonpath={.items[0].metadata.name}")
pf = None
def forward():
    global pf
    if pf: pf.kill()
    pf = subprocess.Popen(["kubectl", "-n", NS, "port-forward", f"svc/{svc}", "19090:9090"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); time.sleep(4)
def api(path):
    for i in range(6):
        try: return json.load(urllib.request.urlopen("http://127.0.0.1:19090" + path, timeout=40))["data"]
        except Exception:
            forward(); time.sleep(5)
    return None
def q(e):
    d = api("/api/v1/query?query=" + urllib.parse.quote(e)); return (d or {}).get("result", []) if d else []
forward(); time.sleep(420); log("warm done")
# --- rule validity
rules = api("/api/v1/rules")["groups"]
mine = [g for g in rules if g["name"].startswith("pipeline-")]
res["rule_groups_loaded"] = [g["name"] for g in mine]; rows = []
SEL = re.compile(r'[a-zA-Z_:][a-zA-Z0-9_:]*\{[^}]*\}|[a-z]+_[a-zA-Z0-9_:]+')
for g in mine:
    for r in g["rules"]:
        sels = sorted(set(SEL.findall(r["query"]))); miss = []
        for s in sels:
            if s.startswith(("label_", "max_over", "min_over", "count_over", "absent")): continue
            v = q(f"count({s})")
            if not v: miss.append(s)
        rows.append({"alert": r["name"], "health": r.get("health"), "state": r.get("state"), "selectors_without_data": miss})
res["meta_rules"] = rows; log(f"rules checked {len(rows)}")
ident = q('process_start_time_seconds{pod=~"prometheus-.*"}'); res["prom_job_label"] = ident[0]["metric"] if ident else None
res["data_sources"] = {k: bool(q(f"count({v})")) for k, v in {"prom": "prometheus_tsdb_head_series", "am": "alertmanager_notifications_total", "cadvisor": 'container_cpu_usage_seconds_total{container="prometheus"}', "cfs": 'container_cpu_cfs_periods_total{container="prometheus"}', "ws": 'container_memory_working_set_bytes{container="prometheus"}', "limit": 'container_spec_memory_limit_bytes{container="prometheus"}', "ksm_restarts": 'kube_pod_container_status_restarts_total{container="prometheus"}', "ksm_reason": 'kube_pod_container_status_last_terminated_reason{container="prometheus"}', "kubelet_volume": "kubelet_volume_stats_available_bytes", "node": "node_filesystem_avail_bytes"}.items()}
res["firing_baseline"] = sorted({x["metric"]["alertname"] for x in q('ALERTS{alertstate="firing"}')})
# --- selfcheck scripts against kube-prometheus-stack
data = os.path.join(OUT, "data")
sh(sys.executable, os.path.join(SK, "inventory/scripts/collect.py"), "--prom", "http://127.0.0.1:19090", "--out", data, "--range", "1h")
res["policy_summary"] = sh(sys.executable, os.path.join(SK, "policy-lint/scripts/policy_lint.py"), data)[:6000]
res["coverage"] = sh(sys.executable, os.path.join(SK, "mom-coverage/scripts/check_coverage.py"), data)[:3500]
json.dump(res, open(os.path.join(OUT, "result.json"), "w"), indent=1, ensure_ascii=False); log("selfcheck ok")
PROM = f"{REL}-kube-prometheus-stack-prometheus"; pname = sh("kubectl", "-n", NS, "get", "prometheus", "-o", "jsonpath={.items[0].metadata.name}")
def patch(cpu, mem):
    sh("kubectl", "-n", NS, "patch", "prometheus", pname, "--type=merge", "-p", json.dumps({"spec": {"resources": {"limits": {"cpu": cpu, "memory": mem}, "requests": {"cpu": "100m", "memory": "256Mi"}}}}))
def snap():
    return {"throttle": [round(float(x["value"][1]), 3) for x in q('rate(container_cpu_cfs_throttled_periods_total{container="prometheus"}[2m]) / rate(container_cpu_cfs_periods_total{container="prometheus"}[2m])')],
            "group_ratio_max": [round(float(x["value"][1]), 2) for x in q("max(prometheus_rule_group_last_duration_seconds / prometheus_rule_group_interval_seconds)")],
            "firing": sorted({x["metric"]["alertname"] for x in q('ALERTS{alertstate="firing"}') if x["metric"].get("scope") or x["metric"]["alertname"] in ("CPUThrottlingHigh", "KubePodCrashLooping", "PrometheusMissingRuleEvaluations", "TargetDown")}),
            "restarts": [x["value"][1] for x in q('kube_pod_container_status_restarts_total{container="prometheus"}')],
            "reason": [x["metric"].get("reason") for x in q('kube_pod_container_status_last_terminated_reason{container="prometheus"}') if float(x["value"][1]) == 1]}
# --- CPU limit
base_ws = q('max(container_memory_working_set_bytes{container="prometheus"})'); ws = float(base_ws[0]["value"][1]) if base_ws else 2e9; res["ws_gb"] = round(ws / 1e9, 2)
patch("150m", "4Gi"); log("cpu limit"); time.sleep(120); forward(); res["cpu_series"] = []
for i in range(4):
    time.sleep(120); res["cpu_series"].append(snap()); log(res["cpu_series"][-1])
patch("2", "4Gi"); time.sleep(150); forward(); log("cpu restored")
json.dump(res, open(os.path.join(OUT, "result.json"), "w"), indent=1, ensure_ascii=False)
# --- OOM
lim = f"{int(ws * .6 / 1e6)}M"; patch("2", lim); log(f"mem limit {lim}"); res["oom_limit"] = lim; time.sleep(420)
pod = "prometheus-" + pname + "-0"
res["oom_pod"] = sh("kubectl", "-n", NS, "get", "pod", pod, "-o", "jsonpath={.status.containerStatuses[?(@.name=='prometheus')].restartCount} {.status.containerStatuses[?(@.name=='prometheus')].lastState.terminated.reason} {.status.containerStatuses[?(@.name=='prometheus')].state}", check=False)
log(res["oom_pod"]); patch("2", "4Gi"); log("mem restored"); time.sleep(300); forward()
res["after_oom"] = snap(); log(res["after_oom"])
res["oom_events"] = q('max_over_time(kube_pod_container_status_last_terminated_reason{container="prometheus",reason="OOMKilled"}[20m])') and "seen"
res["corruptions"] = [x["value"][1] for x in q("prometheus_tsdb_wal_corruptions_total")]
res["am_firing"] = sorted({x["metric"]["alertname"] for x in q('ALERTS{alertstate="firing"}')})
json.dump(res, open(os.path.join(OUT, "result.json"), "w"), indent=1, ensure_ascii=False); log("k8s-done")
if pf: pf.kill()
