"""Poll Prometheus every STEP seconds; write one JSON line per poll with query values and rule states.

usage: python3 record.py OUT.jsonl DURATION_S [STEP_S]
Container ids are mapped to compose names via `docker inspect` so the output stays readable.
"""
import json, subprocess, sys, time, urllib.parse, urllib.request

PROM = "http://localhost:34090"
ALERTS = {"ContainerMemoryNearLimit", "ContainerOOMEvent", "HostOOMKill", "HostFilesystemReadOnly", "HostExporterDown"}
NAMES = ["at-host-memhog", "at-host-memhog2", "at-host-oomhog"]
QUERIES = {
    "wss": 'container_memory_working_set_bytes{job="cadvisor",id=~"/docker/.+"}',
    "limit": 'container_spec_memory_limit_bytes{job="cadvisor",id=~"/docker/.+"} > 0',
    "ratio": 'container_memory_working_set_bytes{job="cadvisor",id=~"/docker/.+"} / (container_spec_memory_limit_bytes{job="cadvisor",id=~"/docker/.+"} > 0)',
    "ratio_rule_selector": 'container_memory_working_set_bytes{job=~".*(cadvisor|kubelet).*", container!=""}',
    "oom_events": 'container_oom_events_total{job="cadvisor",id=~"/docker/.+"}',
    "oom_events_increase10m": 'increase(container_oom_events_total{job="cadvisor",id=~"/docker/.+"}[10m])',
    "node_vmstat_oom_kill": 'node_vmstat_oom_kill{job="node"}',
    "node_oom_increase10m": 'increase(node_vmstat_oom_kill{job="node"}[10m])',
    "fs_readonly": 'node_filesystem_readonly{job="node",mountpoint=~"/data-.*"}',
}

def ids():
    out = {}
    for n in NAMES:
        r = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", n], capture_output=True, text=True)
        if r.returncode == 0:
            out["/docker/" + r.stdout.strip()] = n
    return out

def get(path, **params):
    url = PROM + path + ("?" + urllib.parse.urlencode(params) if params else "")
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)["data"]

out, dur = sys.argv[1], float(sys.argv[2])
step = float(sys.argv[3]) if len(sys.argv) > 3 else 10
end = time.time() + dur
with open(out, "a") as f:
    while time.time() < end:
        idmap = ids()
        row = {"wall": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        for k, q in QUERIES.items():
            res = get("/api/v1/query", query=q)
            vals = []
            for s in res["result"]:
                m = s["metric"]
                if "id" in m and m["id"] not in idmap:
                    continue  # other containers on this machine
                lbl = idmap.get(m.get("id"), m.get("mountpoint", m.get("id", "")))
                vals.append({"series": lbl, "t": s["value"][0], "v": s["value"][1]})
            row[k] = vals
        rules = []
        for g in get("/api/v1/rules")["groups"]:
            for r in g["rules"]:
                if r.get("name") in ALERTS:
                    rules.append({"alert": r["name"], "state": r["state"], "health": r["health"],
                                  "active": [{"labels": {k: v for k, v in a["labels"].items() if k in ("id", "mountpoint", "instance")},
                                              "state": a["state"], "activeAt": a["activeAt"]} for a in r.get("alerts", [])]})
        row["rules"] = rules
        f.write(json.dumps(row, ensure_ascii=False) + "\n"); f.flush()
        time.sleep(step)
