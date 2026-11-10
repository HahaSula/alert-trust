#!/usr/bin/env python3
"""產生 promtool 單元測試：每個 range 函式在推導出的最小視窗「差一點」與「剛好」時，結果是空還是有值。

  python3 gen_tests.py      # 寫出 tests-v3.yml、tests-v2.yml、tests-alerts-v3.yml、tests-alerts-v2.yml

所有 series 的 scrape 間隔 I = 30s，時間從 0 開始（promtool 的虛擬時鐘）。每個 case 的 why 會寫進 YAML 註解，
results/ 的報告也由 run.sh 從這裡的 CASES 產生（gen_tests.py --table）。
"""
from __future__ import annotations

import json
import sys

L = '{job="lab"}'
SERIES = {
    # 計數器：0@0s, 10@30s, 20@60s, 30@90s, 40@120s ...
    "c": "0+10x20",
    # Gauge：1@0s, 2@30s, 3@60s, 4@90s, 5@120s ...
    "g": "1+1x20",
    # k = 1：90s 那次 scrape 失敗（沒有 sample）。
    "c_gap": "0 10 20 _ 40 50 60 70 80 90",
    # d = 20s：120s 開始的 scrape 要到 140s 才寫入，所以在 120–140s 之間查詢時它還不存在；只放 0–90s 的 sample。
    "c_inflight": "0 10 20 30",
    # 90s 的 scrape 失敗，Prometheus 寫入 staleness marker。
    "s": "0 10 20 stale",
}

E = None  # 期望空結果


def one(v: float, labels: str = "{}") -> list:
    return [(labels, v)]


# (群組, 說明, expr, eval_time, 期望)
CASES_V3 = [
    # A. 3.x 視窗是左開右閉 (t-R, t]
    ("A 左開", "R = 2I，邊界上的 sample 不算：只有 2 個", "count_over_time(c[1m])", "2m", one(2, L)),
    ("A 左開", "R = I：只有 1 個 sample，rate 空", "count(rate(c[30s]))", "1m", E),
    ("A 左開", "R = I：count_over_time 是 1", "count_over_time(c[30s])", "1m", one(1, L)),
    # B. 需要 2 個 sample 的函式；最差相位：eval 在 1m59s，sample 在 0,30,60,90
    *[(f"B 2 個 sample", f"{fn}：R = 2I − 1s 只剩 1 個", f"count({expr.format(r='59s')})", "1m59s", E)
      for fn, expr in [("rate", "rate(c[{r}])"), ("increase", "increase(c[{r}])"), ("delta", "delta(g[{r}])"),
                       ("irate", "irate(c[{r}])"), ("idelta", "idelta(g[{r}])"), ("deriv", "deriv(g[{r}])"),
                       ("predict_linear", "predict_linear(g[{r}], 60)")]],
    *[(f"B 2 個 sample", f"{fn}：R = 2I 有 2 個", f"count({expr.format(r='60s')})", "1m59s", one(1))
      for fn, expr in [("rate", "rate(c[{r}])"), ("increase", "increase(c[{r}])"), ("delta", "delta(g[{r}])"),
                       ("irate", "irate(c[{r}])"), ("idelta", "idelta(g[{r}])"), ("deriv", "deriv(g[{r}])"),
                       ("predict_linear", "predict_linear(g[{r}], 60)")]],
    # C. 1 個 sample 就有值的函式
    *[(f"C 1 個 sample", f"{fn}：R = I − 1s 沒有 sample", f"count({fn}({pre}g[29s]))", "1m59s", E)
      for fn, pre in [("avg_over_time", ""), ("min_over_time", ""), ("max_over_time", ""), ("sum_over_time", ""),
                      ("count_over_time", ""), ("last_over_time", ""), ("quantile_over_time", "0.5, "),
                      ("stddev_over_time", ""), ("stdvar_over_time", ""), ("present_over_time", ""),
                      ("changes", ""), ("resets", "")]],
    *[(f"C 1 個 sample", f"{fn}：R = I 有 1 個", f"count({fn}({pre}g[30s]))", "1m59s", one(1))
      for fn, pre in [("avg_over_time", ""), ("min_over_time", ""), ("max_over_time", ""), ("sum_over_time", ""),
                      ("count_over_time", ""), ("last_over_time", ""), ("quantile_over_time", "0.5, "),
                      ("stddev_over_time", ""), ("stdvar_over_time", ""), ("present_over_time", ""),
                      ("changes", ""), ("resets", "")]],
    ("C 1 個 sample", "changes 只有 1 個 sample 時回傳 0（g 其實每次都變）", "changes(g[30s])", "1m59s", one(0, L)),
    ("C 1 個 sample", "changes 有 2 個 sample 才看得到變化", "changes(g[60s])", "1m59s", one(1, L)),
    ("C 1 個 sample", "absent_over_time 反過來：視窗內沒有 sample 就回傳 1（假的「消失」）",
     "absent_over_time(c[29s])", "1m59s", one(1)),
    ("C 1 個 sample", "absent_over_time：有 1 個 sample 就回傳空", "absent_over_time(c[30s])", "1m59s", E),
    # D. k：90s 的 scrape 失敗
    ("D 失敗 k=1", "rate：R = 2I，缺 1 個只剩 1 個", "count(rate(c_gap[1m]))", "2m", E),
    ("D 失敗 k=1", "rate：R = (2+1)I", "count(rate(c_gap[90s]))", "2m", one(1)),
    ("D 失敗 k=1", "last_over_time：R = I，唯一的 sample 缺了", "count(last_over_time(c_gap[30s]))", "1m59s", E),
    ("D 失敗 k=1", "last_over_time：R = (1+1)I", "count(last_over_time(c_gap[60s]))", "1m59s", one(1)),
    # E. d：120s 的 sample 還沒寫入，eval 在 139s
    ("E 寫入延遲 d=20s", "rate：R = 2I + d − 1s", "count(rate(c_inflight[79s]))", "2m19s", E),
    ("E 寫入延遲 d=20s", "rate：R = 2I + d", "count(rate(c_inflight[80s]))", "2m19s", one(1)),
    ("E 寫入延遲 d=20s", "rate：offset 20s（O ≥ d），R = 2I − 1s", "count(rate(c_inflight[59s] offset 20s))", "2m19s", E),
    ("E 寫入延遲 d=20s", "rate：offset 20s（O ≥ d），R = 2I", "count(rate(c_inflight[60s] offset 20s))", "2m19s", one(1)),
    ("E 寫入延遲 d=20s", "last_over_time：R = I + d − 1s", "count(last_over_time(c_inflight[49s]))", "2m19s", E),
    ("E 寫入延遲 d=20s", "last_over_time：R = I + d", "count(last_over_time(c_inflight[50s]))", "2m19s", one(1)),
    # F. staleness marker
    ("F staleness", "marker 之前：instant selector 有值", "s", "1m20s", one(20, 's{job="lab"}')),
    ("F staleness", "marker 之後：instant selector 立刻變空（不等 5m lookback）", "s", "1m40s", E),
    ("F staleness", "range 函式略過 marker：視窗內只剩 1 個 sample", "count(rate(s[1m]))", "1m40s", E),
    ("F staleness", "range 函式略過 marker：marker 之前的 3 個 sample 仍算", "count_over_time(s[2m])", "1m40s", one(3, L)),
    ("F staleness", "last_over_time 回傳 marker 之前的舊值，看不出 series 已經 stale",
     "last_over_time(s[2m])", "1m40s", one(20, 's{job="lab"}')),
    # G. subquery
    ("G subquery", "內層 rate[30s] 每一步都只有 1 個 sample：整個 subquery 空",
     "max_over_time(rate(c[30s])[2m:30s])", "2m", E),
    ("G subquery", "內層 rate[1m] 每一步都有 2 個 sample", "count(max_over_time(rate(c[1m])[2m:30s]))", "2m", one(1)),
    ("G subquery", "原始 sample：(0,120] 內 c_gap 只有 3 個", "count_over_time(c_gap[2m])", "2m", one(3, L)),
    ("G subquery", "subquery 的步點用 lookback 補值：缺的那一步也有值，算成 4 個",
     "count_over_time(c_gap[2m:30s])", "2m", one(4, L)),
]

# 3.x 的實驗語法（anchored、smoothed）與實驗函式不能用 promtool 測：promtool 3.13.5 的 --enable-feature 只套用在
# 載入 rule 檔的 parser，評估查詢時 engine 仍用預設 parser（cmd/promtool/unittest.go），所以改在 server/ 用真的 Prometheus 查。

# 2.51.2：閉區間 [t-R, t]
CASES_V2 = [
    ("A 閉區間", "R = 2I，兩端邊界的 sample 都算：3 個", "count_over_time(c[1m])", "2m", one(3, L)),
    ("A 閉區間", "R = I：2 個 sample，rate 有值（3.x 是空）", "count(rate(c[30s]))", "1m", one(1)),
    ("A 閉區間", "R = I：count_over_time 是 2", "count_over_time(c[30s])", "1m", one(2, L)),
    ("B 2 個 sample", "rate：R = 2I − 1s，eval 在 1m59s 時左邊界剛好落在 60s，仍有 2 個", "count(rate(c[59s]))", "1m59s",
     one(1)),
    ("B 2 個 sample", "rate：R = 2I − 1s，換成最差相位（1m59.5s）只剩 1 個", "count(rate(c[59s]))", "1m59s500ms", E),
    ("B 2 個 sample", "rate：R = 2I，最差相位也有 2 個", "count(rate(c[60s]))", "1m59s500ms", one(1)),
    ("B 2 個 sample", "holt_winters：R = 2I − 1s，最差相位", "count(holt_winters(g[59s], 0.5, 0.5))", "1m59s500ms", E),
    ("B 2 個 sample", "holt_winters：R = 2I", "count(holt_winters(g[60s], 0.5, 0.5))", "1m59s500ms", one(1)),
    ("C 1 個 sample", "last_over_time：R = I − 1s，最差相位", "count(last_over_time(g[29s]))", "1m59s500ms", E),
    ("C 1 個 sample", "last_over_time：R = I", "count(last_over_time(g[30s]))", "1m59s500ms", one(1)),
    ("F staleness", "marker 之後：instant selector 立刻變空", "s", "1m40s", E),
    ("F staleness", "last_over_time 回傳 marker 之前的舊值", "last_over_time(s[2m])", "1m40s", one(20, 's{job="lab"}')),
]


def q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def render(cases: list, header: str) -> str:
    out = [f"# {header}", "# 由 gen_tests.py 產生，不要手改。", "rule_files: []", "evaluation_interval: 30s", "tests:",
           "  - interval: 30s", "    input_series:"]
    for name, values in SERIES.items():
        out += [f"      - series: {q(name + L)}", f"        values: {q(values)}"]
    out.append("    promql_expr_test:")
    for group, why, expr, at, exp in cases:
        out += [f"      # [{group}] {why}", f"      - expr: {q(expr)}", f"        eval_time: {at}"]
        if exp is None:
            out.append("        exp_samples: []")
        else:
            out.append("        exp_samples:")
            for labels, v in exp:
                out += [f"          - labels: {q(labels)}", f"            value: {v}"]
    return "\n".join(out) + "\n"


ALERT_TESTS = """# rules.yml 的 alert：k = 1 時 R = 2I 永遠不 firing，R = (2+1)I 會 firing。{note}
# 由 gen_tests.py 產生，不要手改。
rule_files: [rules.yml]
evaluation_interval: 30s
tests:
  - interval: 30s
    input_series:
      # 每 3 次 scrape 少 1 次（60s、150s、240s、330s、420s 沒有 sample）
      - series: 'c_gap3{{job="lab"}}'
        values: '0 10 _ 30 40 _ 60 70 _ 90 100 _ 120 130 _ 150 160'
    alert_rule_test:
      - eval_time: 6m
        alertname: RateR60
{r60}
      - eval_time: 6m
        alertname: RateR90
        exp_alerts:
          - exp_labels:
              job: lab
"""


def table(cases: list) -> str:
    rows = ["| 群組 | 說明 | expr | eval_time | 期望 |", "|---|---|---|---|---|"]
    for group, why, expr, at, exp in cases:
        e = "空" if exp is None else "、".join(f"`{l}` {v}" for l, v in exp)
        rows.append(f"| {group} | {why} | `{expr}` | {at} | {e} |")
    return "\n".join(rows)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--table":
        which = sys.argv[2]
        print(table({"v3": CASES_V3, "v2": CASES_V2}[which]))
        return 0
    open("tests-v3.yml", "w", encoding="utf-8").write(render(CASES_V3, "Prometheus 3.13.5：各 range 函式的最小視窗"))
    open("tests-alerts-v3.yml", "w", encoding="utf-8").write(
        ALERT_TESTS.format(note="", r60="        exp_alerts: []"))
    open("tests-alerts-v2.yml", "w", encoding="utf-8").write(ALERT_TESTS.format(
        note="\n# 2.51.2 是閉區間，promtool 的 evaluation 剛好對齊 scrape（都從 0 開始每 30s），[t-60, t] 有 3 個位置，"
             "\n# 缺 1 個還剩 2 個，所以 RateR60 也會 firing。實際環境的 scrape 與 evaluation 相位錯開，不能靠這個。",
        r60="        exp_alerts:\n          - exp_labels:\n              job: lab"))
    open("tests-v2.yml", "w", encoding="utf-8").write(render(CASES_V2, "Prometheus 2.51.2：閉區間對照"))
    print(f"v3 {len(CASES_V3)} cases, v2 {len(CASES_V2)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
