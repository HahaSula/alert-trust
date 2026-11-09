"""把 Prometheus /api/v1/query_range 的結果印成一列一條 series：label | 每個 step 的值。
argv[1] = 起點 epoch（印相對秒數用）。"""
import json
import sys

KEEP = ("instance", "datname", "state", "wait_event_type", "mode", "relname", "collector", "queryid")
start = float(sys.argv[1])
r = json.load(sys.stdin)
if r.get("status") != "success":
    print("    ERROR", r.get("error")); sys.exit()
res = r["data"]["result"]
if not res:
    print("    (空)"); sys.exit()
def fmt(v):
    f = float(v)
    if f != f:
        return "NaN"
    if abs(f) >= 1e6:
        return f"{f:.3g}"
    return f"{f:.0f}" if abs(f) >= 100 or f == int(f) else f"{f:.2f}"
ts = sorted({int(float(t)) for x in res for t, _ in x["values"]})
print(f"    {'t+秒（牆鐘）':40} " + " ".join(f"{t - int(start):>6}" for t in ts))
for x in sorted(res, key=lambda x: sorted(x["metric"].items())):
    m = x["metric"]
    label = ",".join(f"{m[k]}" for k in KEEP if m.get(k)) or "-"
    vals = {int(float(t)): v for t, v in x["values"]}
    print(f"    {label[:40]:40} " + " ".join(f"{fmt(vals[t]) if t in vals else '·':>6}" for t in ts))
