#!/usr/bin/env bash
# 跑 Pending 抖動實驗：產生設定、啟動、等 DURATION 秒、分析、清掉容器。
set -euo pipefail
cd "$(dirname "$0")"
DURATION=${DURATION:-1500}
python3 gen.py
docker compose up -d --quiet-pull
trap 'docker compose down -v >/dev/null 2>&1 || true' EXIT
START=$(date +%s)
sleep "$DURATION"
python3 analyze.py "$START" "$(date +%s)" 600
