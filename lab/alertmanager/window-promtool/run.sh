#!/bin/sh
# 用兩個版本的 promtool 跑同一份測試，數每條 alert 有幾個時間點 firing（180–330 分鐘每分鐘一點，共 151 點）。
cd "$(dirname "$0")"
python3 gen.py
for v in v2.51.2 v3.13.5; do
  out=$(docker run --rm -v "$PWD":/w -w /w --entrypoint promtool prom/prometheus:$v test rules test.yml 2>&1)
  printf "%s\n" "$out" | grep "alertname:" > ../results/window/promtool-failures-$v.txt
  for a in Rate5mFor5m Increase1h Increase2h; do
    n=$(printf '%s\n' "$out" | grep -c "alertname: $a,")
    echo "$v $a firing_points=$n/151"
  done
done
