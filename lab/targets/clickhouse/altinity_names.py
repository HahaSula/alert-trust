#!/usr/bin/env python3
"""檢查 Altinity clickhouse-operator 規則（release-0.27.4）用到的名稱，在 lab 的 ClickHouse 裡有沒有來源。
  altinity_names.py <prometheus-alert-rules-clickhouse.yaml> <ch HTTP port> <keeper /metrics port> <prometheus-alert-rules-chkeeper.yaml>

chi_clickhouse_metric_X：metrics-exporter 從 system.metrics、system.asynchronous_metrics 與自己的 SQL（LongestRunningQuery、
ChangedSettingsHash、DiskFreeBytes、DetachedParts 等）組出來；chi_clickhouse_event_X 從 system.events（只列出非 0 的事件）；
chi_clickhouse_table_X 從 system.parts／system.mutations。見 pkg/metrics/clickhouse/clickhouse_metrics_fetcher.go。
ClickHouse*_X（Keeper 規則）直接對 Keeper 的原生 /metrics。
"""
import re, sys, urllib.request

EXPORTER_SQL = {  # clickhouse_metrics_fetcher.go 自己用 SQL 算出來的名稱
    "metric": {"LongestRunningQuery", "ChangedSettingsHash", "MemoryDictionaryBytesAllocated", "DiskDataBytes",
               "MemoryPrimaryKeyBytesAllocated", "DiskFreeBytes", "DiskTotalBytes", "DetachedParts", "fetch_errors"},
    "table": {"mutations", "mutations_parts_to_do", "partitions", "parts", "parts_bytes", "parts_rows"},
}

def sql(port, q):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=q.encode(), method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return set(r.read().decode().split())

ch_rules, port, kport, keeper_rules = sys.argv[1:5]
text = open(ch_rules).read()
metrics = sql(port, "SELECT metric FROM system.metrics UNION ALL SELECT metric FROM system.asynchronous_metrics")
events = sql(port, "SELECT event FROM system.events SETTINGS system_events_show_zero_values = 1")
events_nonzero = sql(port, "SELECT event FROM system.events")
print(f"system.metrics＋asynchronous_metrics {len(metrics)} 個名稱；system.events 全部 {len(events)} 個、目前非 0 的 {len(events_nonzero)} 個")
for kind, name in sorted(set(re.findall(r"chi_clickhouse_(metric|event|table)_([A-Za-z0-9_]+)", text))):
    if kind == "event":
        ok = name in events
        note = "" if not ok else ("" if name in events_nonzero else "（目前是 0：system.events 預設不列，exporter 不輸出這條 series）")
    elif kind == "metric":
        ok = name in metrics or name in EXPORTER_SQL["metric"]
        note = "（exporter 自己的 SQL）" if name in EXPORTER_SQL["metric"] else ""
    else:
        ok = name in EXPORTER_SQL["table"]; note = "（exporter 自己的 SQL）"
    print(f"  {'OK     ' if ok else 'MISSING'} chi_clickhouse_{kind}_{name} {note}")

with urllib.request.urlopen(f"http://127.0.0.1:{kport}/metrics", timeout=10) as r:
    keeper = {l.split("{")[0].split(" ")[0] for l in r.read().decode().splitlines() if l and not l.startswith("#")}
print(f"Keeper /metrics {len(keeper)} 個名稱")
for name in sorted(set(re.findall(r"(ClickHouse(?:AsyncMetrics|Metrics|ProfileEvents)_[A-Za-z0-9_]+)", open(keeper_rules).read()))):
    print(f"  {'OK     ' if name in keeper else 'MISSING'} {name}")
