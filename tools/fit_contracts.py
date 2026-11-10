#!/usr/bin/env python3
"""Check rule range windows against the environment's timing, and when they fail, say how to fit either side.

  python3 tools/fit_contracts.py --rules my-rules/*.yml --stack prometheus \
      --scrape-interval 30s --scrape-duration 20s --eval-interval 30s [--query-offset 0s] [--k 1] \
      [--job-interval mysql=60s] [--job-duration mysql=45s] [--group-duration 5s] \
      --out report [--write-fitted report/fitted-rules.yml]

  python3 tools/fit_contracts.py --rules vm-rules/*.yml --stack vmalert \
      --scrape-interval 30s --eval-interval 1m --latency 40s --eval-delay 30s --out report

The report answers, in this order (the order a reviewer needs):
  1. Does the environment itself meet the contracts? (d <= S, T <= E, lookback)  If not: 目前規格不合.
  2. Which rules are valid under the current environment.
  3. For each rule that is not: option A, rewrite the rule (smallest window that fits the current
     environment), and option B, keep the rule and change the environment (largest scrape_interval,
     smallest query_offset / eval_delay that make it fit).
  4. One environment change that makes every current rule fit (option B for all rules at once).

Contracts (docs/contracts.md): Prometheus range-window R >= (2+k)·I + d, or R >= (2+k)·I when the
offset (query_offset + selector offset) >= d; I = S when d <= S, else ceil(d/S)·S. vmalert
vm-range-window R >= I + L - D. A rule reading a recording rule gets I = that group's interval, d = 0
(approximation; say so in the review). Numbers you were not given are reported as assumptions.
"""
from __future__ import annotations

import argparse
import copy
import math
import os
import re
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog as C  # noqa: E402

_DUR = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)")
_UNIT = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}
# selector (optional name + matchers) followed by a range [R] (not a subquery [R:res])
_RANGE = re.compile(r"(?P<sel>[A-Za-z_:][A-Za-z0-9_:]*\s*(?:\{[^}]*\})?|\{[^}]*\})\s*\[(?P<r>[0-9smhdwy.]+)\]"
                    r"(?:\s*offset\s+(?P<off>-?[0-9smhdwy.]+))?")
_JOB_EQ = re.compile(r'job\s*=\s*"([^"]+)"')


def dur(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    parts = _DUR.findall(s)
    if not parts or "".join(a + b for a, b in parts) != s:
        raise ValueError(f"看不懂的時間長度：{v!r}")
    return sum(float(a) * _UNIT[b] for a, b in parts)


def fmt(sec: float) -> str:
    sec = int(round(sec))
    if sec <= 0:
        return "0s"
    out = ""
    for unit, n in (("h", 3600), ("m", 60), ("s", 1)):
        if sec >= n:
            out += f"{sec // n}{unit}"
            sec %= n
    return out


def fmt_max(sec: float) -> str:
    """Upper bounds are rounded down so the printed value still satisfies the contract."""
    return fmt(math.floor(sec))


def nice_up(sec: float) -> float:
    """Round a needed window up to a readable value."""
    step = 15 if sec <= 120 else 30 if sec <= 600 else 60
    return math.ceil(sec / step) * step


def kv(items: list[str]) -> dict[str, float]:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k] = dur(v)
    return out


def eff_interval(S: float, d: float) -> float:
    return S if d <= S else math.ceil(d / S) * S


def need_window(a, I: float, d: float, offset: float) -> float:
    if a.stack == "vmalert":
        return I + a.latency - a.eval_delay
    return (2 + a.k) * I + (0 if offset >= d else d)


def load(files: list[str]) -> list[tuple[str, dict]]:
    return [(p, C._load(p) or {}) for p in files]


def analyze(a, docs):
    recorded = {}
    for _, doc in docs:
        for g in doc.get("groups", []) or []:
            for r in g.get("rules", []) or []:
                if "record" in r:
                    recorded[r["record"]] = dur(g.get("interval")) or a.E
    rows, fitted_docs = [], []
    for path, doc in docs:
        new = copy.deepcopy(doc)
        for gi, g in enumerate(doc.get("groups", []) or []):
            if g.get("type") not in (None, "", "prometheus"):
                continue
            E = dur(g.get("interval")) or a.E
            qoff = dur(g.get("query_offset")) if g.get("query_offset") is not None else a.O
            for ri, r in enumerate(g.get("rules", []) or []):
                expr = str(r.get("expr", ""))
                name = r.get("alert") or r.get("record")
                new_expr = expr
                for m in _RANGE.finditer(expr):
                    sel = m.group("sel")
                    metric = re.match(r"[A-Za-z_:][A-Za-z0-9_:]*", sel)
                    metric = metric.group(0) if metric else ""
                    R = dur(m.group("r"))
                    off = qoff + (dur(m.group("off")) or 0)
                    if metric in recorded:
                        S, d, src = recorded[metric], 0.0, f"recording rule（group interval {fmt(recorded[metric])}）"
                    else:
                        job = (_JOB_EQ.search(sel) or [None, None])[1]
                        S = a.job_interval.get(job, a.S)
                        d = a.job_duration.get(job, a.d if a.d is not None else S)
                        src = f"job {job}" if job else "預設"
                    I = eff_interval(S, d)
                    need = need_window(a, I, d, off)
                    row = {"file": path, "group": g.get("name"), "rule": name, "metric": metric, "R": R, "S": S,
                           "d": d, "I": I, "offset": off, "need": need, "src": src, "ok": R >= need}
                    if not row["ok"]:
                        fit = nice_up(need)
                        row["fit_R"] = fit
                        new_expr = new_expr.replace(m.group(0), m.group(0).replace(f"[{m.group('r')}]", f"[{fmt(fit)}]", 1), 1)
                        if a.stack == "vmalert":
                            row["opt_b"] = {"eval_delay": max(0.0, I + a.latency - R),
                                            "scrape_interval": R - a.latency + a.eval_delay}
                        else:
                            b = {"scrape_interval": (R - d) / (2 + a.k) if R > d else 0.0,
                                 "scrape_interval_with_offset": R / (2 + a.k), "query_offset": d}
                            row["opt_b"] = b
                    rows.append(row)
                if new_expr != expr:
                    new["groups"][gi]["rules"][ri]["expr"] = new_expr
        fitted_docs.append((path, new))
    return rows, fitted_docs


def env_checks(a) -> list[tuple[str, bool, str]]:
    out = []
    jobs = {None: (a.S, a.d if a.d is not None else a.S)}
    for j, S in a.job_interval.items():
        jobs[j] = (S, a.job_duration.get(j, a.d if a.d is not None else S))
    for j, d in a.job_duration.items():
        jobs.setdefault(j, (a.job_interval.get(j, a.S), d))
    for j, (S, d) in jobs.items():
        who = f"job {j}" if j else "預設"
        out.append((f"scrape 在 interval 內完成（{who}）：d {fmt(d)} ≤ S {fmt(S)}", d <= S,
                    "" if d <= S else f"把 `scrape_interval` 調到 ≥ {fmt(nice_up(d))}，或降低 scrape 耗時（exporter 查詢、`scrape_timeout`）"))
        if a.stack == "prometheus":
            I = eff_interval(S, d)
            out.append((f"lookback（{who}）：I + d = {fmt(I + d)} < {fmt(a.lookback)}", I + d < a.lookback,
                        "" if I + d < a.lookback else "沒有 range 的 selector（例如 `up == 0`）會間歇回傳空；縮短 `scrape_interval`"))
    if a.T is not None:
        out.append((f"rule group 在 interval 內跑完：T {fmt(a.T)} ≤ E {fmt(a.E)}", a.T <= a.E,
                    "" if a.T <= a.E else f"把貴的 rule 拆到別的 group，或把 group `interval` 調到 ≥ {fmt(nice_up(a.T / 0.8))}"))
    return out


def report(a, rows, checks, assumptions) -> str:
    out = ["# Rule 時間契約：合不合目前的規格\n"]
    if assumptions:
        out.append("**假設**（沒有給的數字）：" + "；".join(assumptions) + "。拿到實際值後重跑。\n")
    out.append("## 1. 環境本身\n")
    bad_env = [c for c in checks if not c[1]]
    out.append("**目前規格不合。**下面的不合格項目沒處理前，任何 rule 的視窗算得再對都不可靠。\n" if bad_env
               else "環境本身合格。\n")
    out.append("| 檢查 | 結果 | 怎麼改 |\n|---|---|---|")
    out += [f"| {t} | {'合格' if ok else '**不合格**'} | {fix or '—'} |" for t, ok, fix in checks]
    ok_rows = [r for r in rows if r["ok"]]
    bad = [r for r in rows if not r["ok"]]
    out.append("\n## 2. 在目前規格下合格的 rule\n")
    if ok_rows:
        out.append("| rule | metric | 視窗 R | 需要 ≥ | 邊際 |\n|---|---|---|---|---|")
        out += [f"| `{r['rule']}` | `{r['metric']}` | {fmt(r['R'])} | {fmt(r['need'])} | +{fmt(r['R'] - r['need'])} |"
                for r in ok_rows]
    else:
        out.append("沒有。")
    out.append("\n## 3. 不合格的 rule：兩個方向\n")
    if not bad:
        out.append("沒有。")
    else:
        out.append("方案 A：改 rule，配合目前的環境。方案 B：不改 rule，改環境。\n")
        if a.stack == "vmalert":
            out.append("| rule | metric | 視窗 R | 需要 ≥ | A：改成 | B：`eval_delay` 至少 | B：或 `scrape_interval` 至多 |\n|---|---|---|---|---|---|---|")
            for r in bad:
                b = r["opt_b"]
                out.append(f"| `{r['rule']}` | `{r['metric']}` | {fmt(r['R'])} | {fmt(r['need'])} | `[{fmt(r['fit_R'])}]` "
                           f"| {fmt(b['eval_delay'])} | {fmt(b['scrape_interval']) if b['scrape_interval'] > 0 else '做不到'} |")
        else:
            out.append("| rule | metric | 來源 | 視窗 R | 需要 ≥ | A：改成 | B：`scrape_interval` 至多 | B：或設 `query_offset` ≥ d，`scrape_interval` 至多 |\n|---|---|---|---|---|---|---|---|")
            for r in bad:
                b = r["opt_b"]
                s1 = fmt_max(b["scrape_interval"]) if b["scrape_interval"] >= r["d"] and b["scrape_interval"] > 0 else f"做不到（d {fmt(r['d'])} 太大）"
                s2 = (f"`query_offset` {fmt(b['query_offset'])}，{fmt_max(b['scrape_interval_with_offset'])}"
                      if b["scrape_interval_with_offset"] >= r["d"] else f"做不到（d {fmt(r['d'])} 太大）")
                if r["src"].startswith("recording"):
                    s1, s2 = f"上游 group `interval` 至多 {fmt_max(b['scrape_interval_with_offset'])}", "—"
                out.append(f"| `{r['rule']}` | `{r['metric']}` | {r['src']} | {fmt(r['R'])} | {fmt(r['need'])} | `[{fmt(r['fit_R'])}]` | {s1} | {s2} |")
        out.append("\nA 的視窗是把需要的長度往上取整；視窗變長，`rate` 對短暫尖峰的反應會變慢（突波被稀釋成約 t/R）。")
        raw = [r for r in bad if not r["src"].startswith("recording")]
        out.append("\n## 4. 一次讓所有 rule 合格的環境設定（方案 B）\n")
        if a.stack == "vmalert":
            dmin = max(r["opt_b"]["eval_delay"] for r in bad)
            out.append(f"- `-rule.evalDelay`（或 group `eval_delay`）至少 {fmt(math.ceil(dmin))}；代價是每條 rule 都晚這麼久才看到資料。")
        elif raw:
            by_src: dict[str, list] = {}
            for r in raw:
                by_src.setdefault(r["src"], []).append(r)
            out.append("| 來源 | 不加 offset：`scrape_interval` 至多 | 或 `query_offset` ≥ | 加 offset 後 `scrape_interval` 至多 |\n|---|---|---|---|")
            for src, rs in by_src.items():
                d = max(r["d"] for r in rs)
                smax = min(r["opt_b"]["scrape_interval"] for r in rs)
                so = min(r["opt_b"]["scrape_interval_with_offset"] for r in rs)
                c1 = fmt_max(smax) if smax >= d and smax > 0 else f"做不到（d {fmt(d)}）"
                c3 = fmt_max(so) if so >= d else f"做不到（d {fmt(d)}）：要先降低 scrape 耗時"
                out.append(f"| {src} | {c1} | {fmt(d)} | {c3} |")
            out.append("\n`query_offset` 要 Prometheus 2.53 以上（全域 `rule_query_offset` 或 group `query_offset`）；代價是每條 rule 都晚這麼久才判斷。")
        if any(r["src"].startswith("recording") for r in bad):
            out.append("- 讀 recording rule 的 rule：調整的是上游 recording rule 所在 group 的 `interval`，不是 `scrape_interval`。")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rules", nargs="+", required=True)
    ap.add_argument("--stack", choices=["prometheus", "vmalert"], required=True)
    ap.add_argument("--scrape-interval", required=True, help="S：預設的 scrape_interval")
    ap.add_argument("--scrape-duration", help="d：尖峰 scrape 耗時（scrape_duration_seconds）；沒給就假設 d = S")
    ap.add_argument("--job-interval", action="append", default=[], help="job=S，個別 job 的 scrape_interval")
    ap.add_argument("--job-duration", action="append", default=[], help="job=d，個別 job 的尖峰 scrape 耗時")
    ap.add_argument("--eval-interval", required=True, help="E：evaluation_interval（group 沒寫 interval 時）")
    ap.add_argument("--group-duration", help="T：尖峰 rule group 耗時")
    ap.add_argument("--query-offset", default="0s", help="O：全域 rule_query_offset（Prometheus）")
    ap.add_argument("--k", type=int, default=1, help="容忍漏掉幾次 scrape（建議至少 1）")
    ap.add_argument("--lookback", default="5m")
    ap.add_argument("--latency", default="0s", help="L：資料延遲（vmalert）")
    ap.add_argument("--eval-delay", default="30s", help="D：-rule.evalDelay（vmalert，預設 30s）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--write-fitted", help="把方案 A 改好的 rule 寫到這個檔（不動原檔；註解不會保留）")
    a = ap.parse_args()
    assumptions = []
    a.S, a.d, a.E, a.O = dur(a.scrape_interval), dur(a.scrape_duration), dur(a.eval_interval), dur(a.query_offset)
    a.T, a.lookback, a.latency, a.eval_delay = dur(a.group_duration), dur(a.lookback), dur(a.latency), dur(a.eval_delay)
    a.job_interval, a.job_duration = kv(a.job_interval), kv(a.job_duration)
    if a.d is None:
        assumptions.append(f"尖峰 scrape 耗時 d 沒給，用最壞情況 d = S（{fmt(a.S)}）")
    if a.T is None:
        assumptions.append("rule group 耗時 T 沒給，沒檢查 T ≤ E")
    if a.stack == "vmalert" and a.latency == 0:
        assumptions.append("資料延遲 L 沒給，用 0")
    rows, fitted = analyze(a, load(a.rules))
    checks = env_checks(a)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "contracts-fit.md"), "w", encoding="utf-8") as f:
        f.write(report(a, rows, checks, assumptions))
    if a.write_fitted:
        groups = [g for _, doc in fitted for g in doc.get("groups", []) or []]
        with open(a.write_fitted, "w", encoding="utf-8") as f:
            f.write("# 由 tools/fit_contracts.py 依目前環境調整視窗（方案 A）；原檔未改動。\n")
            yaml.safe_dump({"groups": groups}, f, allow_unicode=True, sort_keys=False, width=1000)
    bad = sum(1 for r in rows if not r["ok"])
    env_bad = sum(1 for c in checks if not c[1])
    print(f"環境不合格 {env_bad} 項；rule 視窗合格 {len(rows) - bad}、不合格 {bad}")
    return 1 if bad or env_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
