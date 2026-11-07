#!/usr/bin/env bash
# 啟動 lab、等 ruler 跑過幾輪、比對 metric 名稱與規則狀態，最後清掉容器。
set -euo pipefail
cd "$(dirname "$0")"
ROOT=../..
OUT=results
mkdir -p "$OUT"
docker compose up -d --quiet-pull
trap 'docker compose down -v >/dev/null 2>&1 || true' EXIT
sleep 45
python3 "$ROOT/tools/check_metrics.py" \
  prometheus=http://localhost:19091/metrics alertmanager=http://localhost:19093/metrics vmalert=http://localhost:19880/metrics \
  --json "$OUT/prom-2.51.2.json" > "$OUT/prom-2.51.2.txt" || true
python3 "$ROOT/tools/check_metrics.py" prometheus=http://localhost:19092/metrics --json "$OUT/prom-3.13.5.json" > "$OUT/prom-3.13.5.txt" || true
for p in 19091 19092; do
  curl -s "http://localhost:$p/api/v1/rules" | python3 -c '
import json,sys
d=json.load(sys.stdin)["data"]["groups"]
rules=[r for g in d for r in g["rules"]]
bad=[r["name"] for r in rules if r.get("health")!="ok"]
print(f"{len(rules)} rules loaded, unhealthy: {bad or 0}")'
done > "$OUT/rules-health.txt"
curl -s http://localhost:19880/api/v1/rules | python3 -c '
import json,sys
d=json.load(sys.stdin)["data"]["groups"]
rules=[r for g in d for r in g["rules"]]
bad=[(r["name"],r.get("lastError")) for r in rules if r.get("health")!="ok"]
print(f"vmalert: {len(rules)} rules loaded, unhealthy: {bad or 0}")' >> "$OUT/rules-health.txt"
cat "$OUT"/*.txt
