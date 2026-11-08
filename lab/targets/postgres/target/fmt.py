"""把 Prometheus /api/v1/query 結果印成多行：instance metric{重要 label}=value。"""
import json
import sys

KEEP = ("collector", "datname", "state", "application_name", "slot_name", "slot_type", "wal_status",
        "status", "wait_event_type", "mode", "usename", "upstream_host")
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
