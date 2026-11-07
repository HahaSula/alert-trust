"""把 Prometheus /api/v1/query 的結果印成一行：series 數與 job/collector=value。"""
import json
import sys

r = json.load(sys.stdin)["data"]["result"]
parts = []
for x in r:
    m = x["metric"]
    key = "/".join(v for v in (m.get("job", ""), m.get("collector", "")) if v)
    parts.append(f"{key or '{}'}={x['value'][1]}")
print(len(r), "series:", " ".join(parts))
