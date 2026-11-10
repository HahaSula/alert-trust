"""Server lab 的查詢與期望值。時間都是相對 T0（2026-01-01T00:00:00Z）的秒數；series 與 ../gen_tests.py 相同（I = 30s）。

expect：None = 空結果；數字 = 唯一一個 sample 的值。
step：只對 VictoriaMetrics 有意義；vmalert 預設送 step = -datasource.queryStep（5m）。
"""
T0 = 1767225600  # 2026-01-01T00:00:00Z

PROM = "prom"
VM = "vm"
VM_MINSTALE = "vm-minstale"   # -search.minStalenessInterval=1m

THIRD = 1 / 3

CASES = [
    # --- Prometheus 3.13.5 實驗功能（promtool test rules 跑不了，見 ../gen_tests.py render_ext）---
    (PROM, "H 對照", "沒有 modifier：R = I 只有 1 個 sample，rate 空", "count(rate(c[30s]))", 119, None, None),
    (PROM, "H anchored", "anchored：視窗內 1 個 sample＋lookback 內的錨點", "count(rate(c[30s] anchored))", 119, None, 1),
    (PROM, "H anchored", "anchored：視窗內沒有 sample", "count(rate(c[29s] anchored))", 119, None, None),
    (PROM, "H anchored", "anchored increase：錨點 60s(20) 到 90s(30)，不外推", "increase(c[30s] anchored)", 119, None, 10),
    (PROM, "H anchored", "anchored changes：1 個 sample 也看得到變化", "changes(g[30s] anchored)", 119, None, 1),
    (PROM, "H anchored", "對照：一般 changes 只有 1 個 sample 時是 0", "changes(g[30s])", 119, None, 0),
    (PROM, "H smoothed", "smoothed：視窗後有 sample（120s）時，視窗內 0 個也有值", "count(rate(c[29s] smoothed))", 119,
     None, 1),
    (PROM, "H smoothed", "smoothed 在「現在」：視窗後還沒有 sample，視窗內 0 個就空（d = 20s）",
     "count(rate(c_inflight[49s] smoothed))", 139, None, None),
    (PROM, "H smoothed", "smoothed 在「現在」：視窗內 1 個＋錨點", "count(rate(c_inflight[50s] smoothed))", 139, None, 1),
    (PROM, "H 實驗函式", "double_exponential_smoothing：R = 2I − 1s", "count(double_exponential_smoothing(g[59s], 0.5, 0.5))",
     119, None, None),
    (PROM, "H 實驗函式", "double_exponential_smoothing：R = 2I", "count(double_exponential_smoothing(g[60s], 0.5, 0.5))",
     119, None, 1),
    (PROM, "H 實驗函式", "mad_over_time：R = I − 1s", "count(mad_over_time(g[29s]))", 119, None, None),
    (PROM, "H 實驗函式", "mad_over_time：R = I", "mad_over_time(g[30s])", 119, None, 0),
    (PROM, "H 實驗函式", "first_over_time：R = I − 1s", "count(first_over_time(g[29s]))", 119, None, None),
    (PROM, "H 實驗函式", "first_over_time：R = I", "first_over_time(g[30s])", 119, None, 4),

    # --- MetricsQL（VictoriaMetrics v1.155.0）---
    (VM, "M 左開", "count_over_time：R = 2I，左邊界不算（同 Prometheus 3.x）", "count_over_time(c[1m])", 120, "5m", 2),
    (VM, "M 左開", "count_over_time：R = I", "count_over_time(c[30s])", 60, "5m", 1),
    (VM, "M 前一個 sample", "rate：視窗內 1 個 sample，用視窗前的 60s 補", "rate(c[30s])", 119, "5m", THIRD),
    (VM, "M 前一個 sample", "rate：視窗內 0 個 sample", "rate(c[29s])", 119, "5m", None),
    (VM, "M 前一個 sample", "increase：視窗內 1 個 sample", "increase(c[30s])", 119, "5m", 10),
    (VM, "M 前一個 sample", "increase：視窗內 0 個 sample", "increase(c[29s])", 119, "5m", None),
    (VM, "M 前一個 sample", "irate：視窗內 1 個 sample", "irate(c[30s])", 119, "5m", THIRD),
    (VM, "M 前一個 sample", "delta：視窗內 1 個 sample", "delta(g[30s])", 119, "5m", 1),
    (VM, "M 前一個 sample", "idelta：視窗內 1 個 sample", "idelta(g[30s])", 119, "5m", 1),
    (VM, "M 前一個 sample", "changes：把視窗前的 sample 算進去", "changes(g[30s])", 119, "5m", 1),
    (VM, "M 前一個 sample", "resets：視窗內 1 個 sample", "resets(c[30s])", 119, "5m", 0),
    (VM, "M 前一個 sample", "holt_winters：視窗內 1 個 sample", "count(holt_winters(g[30s], 0.5, 0.5))", 119, "5m", 1),
    (VM, "M Prometheus 相容", "rate_prometheus：要 2 個 sample", "rate_prometheus(c[30s])", 119, "5m", None),
    (VM, "M Prometheus 相容", "increase_prometheus：要 2 個 sample", "increase_prometheus(c[30s])", 119, "5m", None),
    (VM, "M 1 個 sample", "deriv：1 個 sample 時回傳 0（Prometheus 是空）", "deriv(g[30s])", 119, "5m", 0),
    (VM, "M 1 個 sample", "predict_linear：1 個 sample 時斜率當 0", "predict_linear(g[30s], 60)", 119, "5m", 4),
    (VM, "M 1 個 sample", "last_over_time：R = I − 1s", "last_over_time(g[29s])", 119, "5m", None),
    (VM, "M 1 個 sample", "last_over_time：R = I", "last_over_time(g[30s])", 119, "5m", 4),
    (VM, "M 1 個 sample", "absent_over_time：視窗內沒有 sample 回傳 1", "absent_over_time(c[29s])", 119, "5m", 1),
    (VM, "M step", "前一個 sample 要在 step 內：step 20s 時 60s 的 sample 太舊", "rate(c[30s])", 119, "20s", None),
    (VM_MINSTALE, "M step", "同上，加 -search.minStalenessInterval=1m", "rate(c[30s])", 119, "20s", THIRD),
    (VM, "M 省略視窗", "rate(c)：視窗 = step（5m）", "rate(c)", 119, "5m", THIRD),
    (VM, "M 省略視窗", "rate(c)：step 20s 時視窗只有 20s", "rate(c)", 119, "20s", None),
    (VM, "M 失敗 k=1", "rate：90s 缺，R = I，前一個 sample 用 60s", "rate(c_gap[30s])", 120, "5m", THIRD),
    (VM, "M 失敗 k=1", "同上，step 30s：前一個 sample 要在 (tStart − step, tStart] = (60s, 90s] 內，60s 不算", "rate(c_gap[30s])", 120, "30s", None),
    # vm-range-window：R ≥ I + L − D。L = 20s（120s 的 sample 還沒寫入），vmalert 在 139s 執行。
    (VM, "M vm-range-window", "D = 0：R = I + L − 1s", "rate(c_inflight[49s])", 139, "5m", None),
    (VM, "M vm-range-window", "D = 0：R = I + L", "rate(c_inflight[50s])", 139, "5m", THIRD),
    (VM, "M vm-range-window", "D = 20s（查詢時間 119s）：R = I + L − D − 1s", "rate(c_inflight[29s])", 119, "5m", None),
    (VM, "M vm-range-window", "D = 20s：R = I + L − D", "rate(c_inflight[30s])", 119, "5m", THIRD),
]
