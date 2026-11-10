#!/usr/bin/env bash
# Prometheus 3.13.5（實驗功能）與 VictoriaMetrics v1.155.0 的查詢實驗。約 1 分鐘，不產生負載。
set -euo pipefail
cd "$(dirname "$0")"
WORK=$(mktemp -d)
export PROM_DATA="$WORK/prom" UID GID="$(id -g)"
cleanup() { docker compose down -v >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT
python3 run.py data "$WORK"
mkdir -p "$PROM_DATA"
docker run --rm -u "$(id -u):$(id -g)" -v "$WORK:/w" --entrypoint promtool prom/prometheus:v3.13.5 \
  tsdb create-blocks-from openmetrics /w/data.om /w/prom >/dev/null
docker compose up -d
python3 run.py wait
python3 run.py import "$WORK"
OUT=../results/server-$(date +%Y%m%d)
python3 run.py query "$OUT"
