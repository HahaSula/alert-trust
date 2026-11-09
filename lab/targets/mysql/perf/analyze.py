"""把 run.sh 的時間窗、Prometheus 時間序列與工作負載逐秒紀錄整理成每個情境的變化表。

  analyze.py dump <prom api> <raw dir>   用 query_range（step 5s）抓整段時間序列，寫 raw/series.csv
  analyze.py summary <raw dir>           印出 Markdown：每個情境、每台資料庫的客戶端 TPS 與各指標的變化與先後

「先動」的定義：情境開始後第一個樣本 |值 − 基線| > max(50% × |基線|, 該指標的最小變化量)；
基線是 s0 情境的中位數。時間是 Prometheus 的樣本時間（wall clock）。
"""
import csv, json, statistics, sys, urllib.parse, urllib.request
from pathlib import Path

ZERO = " or on (instance) (0 * mysql_up)"
# name, expr, 最小變化量, 單位說明
METRICS = [
    ("qps", "rate(mysql_global_status_queries[20s])", 20, "/s"),
    ("commit", 'rate(mysql_global_status_commands_total{command="commit"}[20s])', 20, "/s"),
    ("row_ops", "sum by (instance) (rate(mysql_global_status_innodb_row_ops_total[20s]))", 50, "/s"),
    ("threads_running", "mysql_global_status_threads_running", 3, ""),
    ("threads_connected", "mysql_global_status_threads_connected", 5, ""),
    ("row_lock_waits", "mysql_global_status_innodb_row_lock_current_waits", 1, ""),
    ("row_lock_time", "rate(mysql_global_status_innodb_row_lock_time[20s])", 100, "ms/s"),
    ("mdl_waiting", 'sum by (instance) (mysql_info_schema_processlist_threads{state=~".*metadata.*"})' + ZERO, 1, ""),
    ("oldest_trx_age", "max by (instance) (mysql_info_schema_innodb_trx_oldest_transaction_age_seconds)" + ZERO, 10, "s"),
    ("history_len", "max by (instance) (mysql_global_status_innodb_history_list_length or mysql_info_schema_innodb_metrics_transaction_trx_rseg_history_len)", 1000, ""),
    ("bp_disk_reads", "rate(mysql_global_status_innodb_buffer_pool_reads[20s])", 10, "/s"),
    ("bp_wait_free", "rate(mysql_global_status_innodb_buffer_pool_wait_free[20s])", 1, "/s"),
    ("dirty_pages", 'max by (instance) (mysql_global_status_buffer_pool_pages{state="dirty"})', 200, ""),
    ("log_waits", "rate(mysql_global_status_innodb_log_waits[20s])", 1, "/s"),
    ("checkpoint_age_mb", "(mysql_global_status_innodb_checkpoint_age or (mysql_global_status_innodb_redo_log_current_lsn - mysql_global_status_innodb_redo_log_checkpoint_lsn)) / 1048576", 4, "MB"),
    ("tmp_disk_tables", "rate(mysql_global_status_created_tmp_disk_tables[20s])", 1, "/s"),
    ("slow_queries", "rate(mysql_global_status_slow_queries[20s])", 0.1, "/s"),
    ("conn_refused", 'rate(mysql_global_status_connection_errors_total{error="max_connections"}[20s])', 0.1, "/s"),
    ("aborted_connects", "rate(mysql_global_status_aborted_connects[20s])", 0.1, "/s"),
    ("stmt_seconds", "sum by (instance) (rate(mysql_perf_schema_events_statements_seconds_total[20s]))", 0.5, "s/s"),
    ("stmt_errors", "rate(mysql_perf_schema_events_statements_sum_errors[20s])", 0.5, "/s"),
    ("mdl_wait_seconds", 'sum by (instance) (rate(mysql_perf_schema_events_waits_seconds_total{event_name="wait/lock/metadata/sql/mdl"}[20s]))', 0.1, "s/s"),
    ("mysql_up", "mysql_up", 0.5, ""),
    ("uptime", "mysql_global_status_uptime", 1e12, "s"),
    ("scrape_seconds", 'scrape_duration_seconds{job="mysql"}', 0.5, "s"),
]


def dump(api, raw):
    raw = Path(raw)
    wins = [l.split("\t") for l in (raw / "windows.tsv").read_text().splitlines() if l]
    start, end = int(wins[0][1]) - 90, int(wins[-1][2]) + 30
    with open(raw / "series.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "instance", "t", "value"])
        for name, expr, _, _ in METRICS:
            qs = urllib.parse.urlencode({"query": expr, "start": start, "end": end, "step": 5})
            d = json.load(urllib.request.urlopen(f"{api}/query_range?{qs}"))
            for s in d["data"]["result"]:
                inst = s["metric"].get("instance", "?")
                for t, v in s["values"]:
                    w.writerow([name, inst, int(float(t)), v])


def load_wl(raw, inst):
    p = raw / f"run-{inst}.log"
    out = []
    for line in p.read_text().splitlines() if p.exists() else []:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def fmt(v):
    if v is None:
        return "—"
    a = abs(v)
    return f"{v:.0f}" if a >= 100 else f"{v:.1f}" if a >= 1 else f"{v:.2g}"


def summary(raw):
    raw = Path(raw)
    wins = [l.split("\t") for l in (raw / "windows.tsv").read_text().splitlines() if l]
    series = {}
    with open(raw / "series.csv") as f:
        for r in csv.DictReader(f):
            try:
                v = float(r["value"])
            except ValueError:
                continue
            series.setdefault((r["metric"], r["instance"]), []).append((int(r["t"]), v))
    meta = {m[0]: m for m in METRICS}
    print("# 情境摘要（analyze.py 產生）\n")
    print("基線＝s0 基線情境的中位數；「先動」＝情境開始後第一個偏離基線超過 50%（且超過最小變化量）的樣本距情境開始的秒數；"
          "前一個情境還沒恢復時，先動會是 0–5 秒，要對照前一節看。「缺樣本」＝情境中 Prometheus 沒有值的 5 秒格數。"
          "客戶端 tx/s＝時間窗內成功交易數 ÷ 時間窗長度；最長停頓＝相鄰兩筆「有成功交易」的紀錄之間最長的 wall clock 間隔。時間是 host wall clock。\n")
    b0 = next(w for w in wins if w[0].startswith("s0"))
    bs, be = int(b0[1]), int(b0[2])
    for name, s, e in wins:
        s, e = int(s), int(e)
        print(f"## {name}（{e - s} 秒）\n")
        for inst in ("mdb", "my"):
            wl = load_wl(raw, inst)
            base_ok = sum(x["ok"] for x in wl if bs <= x["t"] < be)
            dur = [x for x in wl if s <= x["t"] <= e]
            errs = {}
            for x in dur:
                for k, v in x["err"].items():
                    errs[k] = errs.get(k, 0) + v
            p99 = [x["p99_ms"] for x in dur if x["p99_ms"] is not None]
            okt = [x["t"] for x in wl if x["ok"] > 0 and s - 5 <= x["t"] <= e + 5]
            stall = max((b - a for a, b in zip(okt, okt[1:])), default=None)
            print(f"**{inst}** 客戶端：基線 {fmt(base_ok / (be - bs))} tx/s → 情境中 "
                  f"{fmt(sum(x['ok'] for x in dur) / (e - s))} tx/s；最長停頓 {fmt(stall) if stall is not None else '—'} 秒；"
                  f"最大 p99 {fmt(max(p99)) if p99 else '—'} ms；錯誤 {json.dumps(errs, ensure_ascii=False) if errs else '無'}\n")
            rows = []
            for (m, i), pts in series.items():
                if i != inst:
                    continue
                b = [v for t, v in pts if bs <= t < be]
                d = [(t, v) for t, v in pts if s <= t <= e]
                if not d:
                    continue
                bm = statistics.median(b) if b else None
                ext = max(d, key=lambda tv: abs(tv[1] - (bm or 0)))[1]
                thr = max(0.5 * abs(bm or 0), meta[m][2])
                first = next((t - s for t, v in d if bm is not None and abs(v - bm) > thr), None)
                missing = (e - s) // 5 + 1 - len(d)
                if m == "uptime":
                    resets = sum(1 for (t0, v0), (t1, v1) in zip(d, d[1:]) if v1 < v0)
                    if resets:
                        rows.append((-1, f"| uptime | {fmt(bm)} | 歸零 {resets} 次 | 有 | {missing} |"))
                    continue
                if first is None and missing <= 1:
                    continue
                rows.append((first if first is not None else 10**6,
                             f"| {m} | {fmt(bm)} | {fmt(ext)}{meta[m][3]} | {first if first is not None else '—'} | {missing} |"))
            if rows:
                print("| 指標 | 基線 | 情境中極值 | 先動（秒） | 缺樣本 |\n|---|---|---|---|---|")
                for _, r in sorted(rows):
                    print(r)
            print()


if __name__ == "__main__":
    if sys.argv[1] == "dump":
        dump(sys.argv[2], sys.argv[3])
    else:
        summary(sys.argv[2])
