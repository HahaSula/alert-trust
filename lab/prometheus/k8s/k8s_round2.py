#!/usr/bin/env python3
"""Round 2: Alertmanager running, fixed rules and scripts. Confirm alerts fire on real K8s signals."""
import json, os, re, subprocess, sys, time, urllib.parse, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__)); OUT = os.path.join(HERE, "..", "results", "k8s"); SK = sys.argv[1]; NS = "monitoring"; res = {}
def sh(*a, check=True, t=900):
    r = subprocess.run(a, capture_output=True, text=True, timeout=t)
    if check and r.returncode: raise RuntimeError(f"{a}: {r.stderr[-800:]}")
    return r.stdout.strip()
def log(m): print(time.strftime("[%H:%M:%S] ") + str(m), flush=True)
sh("helm", "upgrade", "--install", "kps", sys.argv[2], "-n", NS, "-f", os.path.join(HERE, "values.yaml"), "--wait", "--timeout", "10m")
meta = open(os.path.join(SK, "mom-coverage/references/meta-alerts.yml"), encoding="utf8").read(); body = "\n".join(l for l in meta.splitlines() if not l.startswith("#"))
open(os.path.join(HERE, "rule.yaml"), "w", encoding="utf8").write("apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\nmetadata:\n  name: selfcheck-meta\n  namespace: monitoring\nspec:\n" + "\n".join("  " + l for l in body.splitlines()) + "\n")
sh("kubectl", "apply", "-f", os.path.join(HERE, "rule.yaml")); sh("kubectl", "-n", NS, "rollout", "status", "sts/alertmanager-kps-kube-prometheus-stack-alertmanager", "--timeout=240s"); log("am ok")
svc = "kps-kube-prometheus-stack-prometheus"; pname = sh("kubectl", "-n", NS, "get", "prometheus", "-o", "jsonpath={.items[0].metadata.name}"); pf = None
def forward():
    global pf
    if pf: pf.kill()
    pf = subprocess.Popen(["kubectl", "-n", NS, "port-forward", f"svc/{svc}", "19090:9090"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); time.sleep(4)
def api(path):
    for i in range(8):
        try: return json.load(urllib.request.urlopen("http://127.0.0.1:19090" + path, timeout=40))["data"]
        except Exception: forward(); time.sleep(6)
def q(e): return (api("/api/v1/query?query=" + urllib.parse.quote(e)) or {}).get("result", [])
def patch(cpu, mem): sh("kubectl", "-n", NS, "patch", "prometheus", pname, "--type=merge", "-p", json.dumps({"spec": {"resources": {"limits": {"cpu": cpu, "memory": mem}, "requests": {"cpu": "100m", "memory": "256Mi"}}}}))
def save(): json.dump(res, open(os.path.join(OUT, "round2.json"), "w"), indent=1, ensure_ascii=False)
patch("2", "4Gi"); forward(); time.sleep(600); log("warm")
mine = [g for g in api("/api/v1/rules")["groups"] if g["name"].startswith("pipeline-")]; rows = []
SEL = re.compile(r'[a-zA-Z_:][a-zA-Z0-9_:]*\{[^}]*\}|[a-z]+_[a-zA-Z0-9_:]+')
for g in mine:
    for r in g["rules"]:
        miss = [s for s in sorted(set(SEL.findall(re.sub(r"\b(by|on|ignoring|without|group_left)\s*\([^)]*\)", " ", r["query"])))) if not s.startswith(("max_over", "min_over")) and not q(f"count({s})")]
        rows.append({"alert": r["name"], "health": r.get("health"), "state": r.get("state"), "no_data": miss})
res["rules"] = rows; res["firing_healthy"] = sorted({x["metric"]["alertname"] for x in q('ALERTS{alertstate="firing"}')})
res["sources"] = {k: bool(q(f"count({v})")) for k, v in {"am": "alertmanager_notifications_total", "limits": 'kube_pod_container_resource_limits{container="prometheus",resource="memory"}', "ksm_reason": "kube_pod_container_status_last_terminated_reason", "kubelet_volume": "kubelet_volume_stats_available_bytes", "cfs": 'container_cpu_cfs_periods_total{container="prometheus"}'}.items()}
data = os.path.join(OUT, "data2"); sh(sys.executable, os.path.join(SK, "inventory/scripts/collect.py"), "--prom", "http://127.0.0.1:19090", "--out", data, "--range", "1h")
res["policy"] = sh(sys.executable, os.path.join(SK, "policy-lint/scripts/policy_lint.py"), data); res["coverage"] = sh(sys.executable, os.path.join(SK, "mom-coverage/scripts/check_coverage.py"), data); res["contract"] = sh(sys.executable, os.path.join(SK, "rule-contract/scripts/analyze_rules.py"), data)[:3000]; save(); log("selfcheck")
ws = float(q('max(container_memory_working_set_bytes{container="prometheus"})')[0]["value"][1]); res["ws_gb"] = round(ws / 1e9, 2)
ALT = ["PrometheusCPUThrottled", "PrometheusMemoryNearLimit", "CPUThrottlingHigh", "PrometheusRestarted", "KubePodCrashLooping", "RuleGroupNearInterval", "RuleGroupMissingEvaluations", "RuleGroupStale"]
def firing(): return sorted({x["metric"]["alertname"] for x in q('ALERTS{alertstate="firing"}') if x["metric"]["alertname"] in ALT})
# CPU: long enough for the 10m for
patch("150m", "4Gi"); log("cpu"); series = []
for i in range(9):
    time.sleep(120); series.append({"t": (i + 1) * 120, "firing": firing(), "throttle": [round(float(x["value"][1]), 2) for x in q('max(rate(container_cpu_cfs_throttled_periods_total{container="prometheus"}[2m]) / rate(container_cpu_cfs_periods_total{container="prometheus"}[2m]))')]}); log(series[-1])
res["cpu"] = series; save(); patch("2", "4Gi"); time.sleep(180); forward()
# Memory near limit, not OOM
ws = float(q('max(container_memory_working_set_bytes{container="prometheus"})')[0]["value"][1]); lim = int(ws * 1.12 / 1e6); patch("2", f"{lim}M"); log(f"mem near limit {lim}M"); series = []
for i in range(9):
    time.sleep(120); series.append({"t": (i + 1) * 120, "firing": firing(), "ratio": [round(float(x["value"][1]), 2) for x in q('max(container_memory_working_set_bytes{container="prometheus"} / on (namespace, pod, container) group_left kube_pod_container_resource_limits{container="prometheus",resource="memory"})')]}); log(series[-1])
res["mem_limit"] = lim; res["mem"] = series; res["restarts"] = sh("kubectl", "-n", NS, "get", "pod", f"prometheus-{pname}-0", "-o", "jsonpath={.status.containerStatuses[?(@.name=='prometheus')].restartCount} {.status.containerStatuses[?(@.name=='prometheus')].lastState.terminated.reason}", check=False); save(); patch("2", "4Gi"); log("k8s2-done")
if pf: pf.kill()
