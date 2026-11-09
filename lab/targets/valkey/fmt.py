"""把 Prometheus /api/v1/query 結果印成多行：instance metric{重要 label}=value。"""
import json
import sys

KEEP = ("job", "role", "master_host", "master_port", "slave_state", "err", "cmd", "event_name", "master_name",
        "master_status", "message", "db", "key_group", "filename", "key", "redis_version", "valkey_version",
        "maxmemory_policy", "cluster")
r = json.load(sys.stdin)
if r.get("status") != "success":
    print("    ERROR", r.get("error")); sys.exit()
res = r["data"]["result"]
if not res:
    print("    (空)")
for x in sorted(res, key=lambda x: sorted(x["metric"].items())):
    m = x["metric"]
    labels = ",".join(f"{k}={m[k]}" for k in KEEP if m.get(k))
    name = m.get("__name__", "")
    print(f"    {m.get('instance', '-'):12} {name}{{{labels}}} = {x['value'][1]}")
