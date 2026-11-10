#!/usr/bin/env bash
# promtool 單元測試（虛擬時鐘）＋ server/ 的查詢實驗。全部約 2 分鐘，不產生負載。
set -euo pipefail
cd "$(dirname "$0")"
python3 gen_tests.py
DATE=$(date +%Y%m%d)
OUT=results/promtool-$DATE.md
P=(docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/w" -w /w --entrypoint promtool)
{
  echo "# promtool test rules（$DATE）"
  echo
  echo "由 \`run.sh\` 產生。I = 30s，時間從 0 開始（promtool 的虛擬時鐘）。期望值寫在 \`gen_tests.py\`，下面是 promtool 的輸出與每個 case。"
  echo
  echo "## Prometheus 3.13.5"
  echo
  echo '```'
  "${P[@]}" prom/prometheus:v3.13.5 --version 2>&1 | head -1
  "${P[@]}" prom/prometheus:v3.13.5 test rules tests-v3.yml tests-alerts-v3.yml 2>&1 | grep -v '^$'
  echo '```'
  echo
  python3 gen_tests.py --table v3
  echo
  echo "## Prometheus 2.51.2"
  echo
  echo '```'
  "${P[@]}" prom/prometheus:v2.51.2 --version 2>&1 | head -1
  "${P[@]}" prom/prometheus:v2.51.2 test rules tests-v2.yml tests-alerts-v2.yml 2>&1 | grep -v '^$'
  echo '```'
  echo
  python3 gen_tests.py --table v2
  echo
  echo "## 同一個 alert 測試換版本跑（預期失敗，證明 2.x 與 3.x 不同）"
  echo
  echo '```'
  "${P[@]}" prom/prometheus:v3.13.5 test rules tests-alerts-v2.yml 2>&1 | grep -v '^$' || true
  echo '```'
} > "$OUT"
grep -c SUCCESS "$OUT"
./server/run.sh
