#!/usr/bin/env python3
"""Check rule range windows against the environment's timing, and when they fail, say how to fit either side.

  python3 tools/fit_contracts.py --rules my-rules/*.yml --stack prometheus \
      --scrape-interval 30s --scrape-duration 20s --eval-interval 30s [--query-offset 0s] [--k 1] \
      [--job-interval mysql=60s] [--job-duration mysql=45s] [--group-duration 5s] \
      --out report [--write-fitted report/fitted-rules.yml]

  python3 tools/fit_contracts.py --rules vm-rules/*.yml --stack vmalert \
      --scrape-interval 30s --eval-interval 1m --latency 40s --eval-delay 30s --out report

  python3 tools/fit_contracts.py --rules my-rules/*.yml --stack prometheus --config prometheus.yml \
      --job-duration mysqld=9.8s --job-p99 mysqld=6s --out report
      (--config reads global and per-job scrape_interval / scrape_timeout, evaluation_interval and
       rule_query_offset; explicit flags win)

The report answers, in this order (the order a reviewer needs):
  0. 建議先做: a short list of what to change, each with 建議 and 代價 (advice, not a verdict).
  1. Does the environment itself meet the contracts? (d <= S, τ < S, X < τ, durations not cut off by τ,
     p99 <= 0.8τ, E >= S, T <= E, lookback)  Each failed row says 「不符合契約 <id>」 + 建議 + 代價.
  2. Which rules are valid under the current environment.
  3. For each rule that is not: option A, rewrite the rule (smallest window that fits the current
     environment), and option B, keep the rule and change the environment (largest scrape_interval,
     smallest query_offset / eval_delay that make it fit).
  4. One environment change that makes every current rule fit (option B for all rules at once).
     Option B is checked for feasibility: the suggested scrape_interval must stay > τ, with
     τ >= max(d_max/0.9, p99/0.8) (docs/tools/baseline.md) and >= d_max; otherwise 做不到 with the reason.
  5. Threshold rules with `for` whose selector has no range: one failed scrape writes staleness
     markers, the selector is empty at the next evaluation and the pending alert is dropped
     (docs/contracts.md#lookback, #range-window). Reported as a recommendation (last_over_time(x[R]),
     R from range-window-1sample), labelled 推論.

A rule whose job has no --job-* value (and is not in --config) uses the defaults; every such row says
用預設值 and the jobs are listed in the assumptions. Exit code (CI only): 1 when something does not meet
a contract, else 0; it is never printed as a judgement.

Contracts (docs/contracts.md): Prometheus R >= (n+k)·I + max(0, d - O), O = query_offset + selector
offset; n = 2 for rate/increase/delta/irate/idelta/deriv/predict_linear/changes/resets (range-window),
n = 1 for *_over_time (range-window-1sample). I = S when d <= S, else ceil(d/S)·S as an upper bound.
vmalert vm-range-window R >= (n+k)·I + max(0, L - D), n = 1 when (1+k)·I < -datasource.queryStep
(MetricsQL uses the sample before the window), else 2; *_prometheus functions always 2. A rule reading a recording rule gets I = that group's interval, d = 0
(approximation; say so in the review). Numbers you were not given are reported as assumptions.

--baseline prints suggested S, τ, exporter deadline X, E, k and R_min per job from measured
d_p99 / d_max / failure runs (procedure in docs/tools/baseline.md); --rules is optional then.
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
import check_metric_sources as MS  # noqa: E402 - shared PromQL selector parser

_DUR = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)")
_UNIT = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}


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
    """1h2m3s; one decimal when not whole seconds (9.75 -> 9.8s), two significant digits below 1s."""
    if sec is None:
        return "—"
    if sec < 0.005:
        return "0s"
    if sec < 1:
        return f"{sec:.2g}s"
    r = round(sec, 1)
    whole = int(r)
    frac = round(r - whole, 1)
    out = ""
    for unit, n in (("h", 3600), ("m", 60)):
        if whole >= n:
            out += f"{whole // n}{unit}"
            whole %= n
    if frac:
        out += f"{whole + frac:.1f}s"
    elif whole or not out:
        out += f"{whole}s"
    return out


def fmt_max(sec: float) -> str:
    """Upper bounds are rounded down (to 0.1s) so the printed value still satisfies the contract."""
    return fmt(math.floor(sec * 10 + 1e-9) / 10)


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
    """Data interval. When d > S the next scrape starts right after the slow one (one pending tick),
    so I is about max(S, d); ceil(d/S)·S is kept as a conservative upper bound (docs/contracts.md)."""
    return S if d <= S else math.ceil(d / S) * S


# samples needed in the window for a non-empty / meaningful result (docs/contracts.md#range-functions)
TWO_SAMPLES = {"rate", "increase", "delta", "irate", "idelta", "deriv", "predict_linear", "holt_winters",
               "double_exponential_smoothing", "changes", "resets"}  # changes/resets: 2 to detect a change
ANCHORED_OK = {"rate", "increase", "delta", "changes", "resets"}  # Prometheus 3.x `anchored` modifier
ONE_SAMPLE = {f"{x}_over_time" for x in ("avg", "min", "max", "sum", "count", "stddev", "stdvar", "quantile",
                                          "last", "present", "absent", "mad")}


enclosing_func = MS.enclosing_func  # innermost function around a position (shared with check_metric_sources)


def samples_needed(a, func: str, I: float, k: int) -> int:
    if a.stack == "vmalert":
        # MetricsQL uses the last sample before the window when it is within the query step
        if func.endswith("_prometheus") or (1 + k) * I >= a.query_step:
            return 2
        return 1
    if func in ONE_SAMPLE:
        return 1
    return 2  # TWO_SAMPLES, or an unknown wrapper: conservative


def k_for(a, job) -> int:
    """Tolerated missed scrapes: --k, raised to the longest failure run seen for that job."""
    return max(a.k, int(a.job_failrun.get(job, 0)))


def need_window(a, I: float, d: float, offset: float, k: int | None = None, n: int = 2) -> float:
    """range-window / range-window-1sample: (n+k)·I + max(0, d − O); vmalert: (n+k)·I + max(0, L − D)."""
    k = a.k if k is None else k
    if a.stack == "vmalert":
        return (n + k) * I + max(0.0, a.latency - a.eval_delay)
    return (n + k) * I + max(0.0, d - offset)


def load(files: list[str]) -> list[tuple[str, dict]]:
    return [(p, C._load(p) or {}) for p in files]


def job_params(a, job) -> dict:
    """S, d, τ, p99 for a job, and which of them fell back to the defaults (reported per row)."""
    defaults = []
    if job in a.job_interval:
        S = a.job_interval[job]
    else:
        S = a.S
        defaults.append(f"S {fmt(a.S)}")
    if job in a.job_duration:
        d, measured = a.job_duration[job], True
    elif a.d is not None:
        d, measured = a.d, True
        defaults.append(f"d {fmt(a.d)}")
    else:
        d, measured = S, False
        defaults.append("d = S")
    tau = a.job_timeout.get(job, a.timeout)
    if tau is not None and job not in a.job_timeout:
        defaults.append(f"τ {fmt(tau)}")
    return {"S": S, "d": d, "measured": measured, "tau": tau, "p99": a.job_p99.get(job, a.p99), "defaults": defaults}


def s_floor(P: dict) -> tuple[float, str, float | None]:
    """Smallest feasible scrape_interval for a job (docs/tools/baseline.md steps 4–5): d_max ≤ S and S > τ, with
    τ ≥ max(d_max / 0.9, p99 / 0.8) rounded up to whole seconds. Without measurements the current τ is the floor."""
    parts, tau_need = [], 0.0
    if P["measured"]:
        tau_need = P["d"] / 0.9
        parts.append(f"d_max {fmt(P['d'])}")
    if P["p99"]:
        if P["p99"] / 0.8 > tau_need:
            tau_need = P["p99"] / 0.8
        parts.append(f"p99 {fmt(P['p99'])}")
    if not tau_need and P["tau"]:
        tau_need = P["tau"]
        parts.append(f"目前的 τ {fmt(P['tau'])}")
    if not tau_need:
        return 0.0, "", None
    tau_i = float(math.ceil(tau_need - 1e-9))
    smin = max(tau_i + 1, P["d"] if P["measured"] else 0.0)
    return smin, f"{'、'.join(parts)} 要 τ ≥ {fmt(tau_i)}，`scrape_interval` 要 > τ，也就是至少 {fmt(smin)}", tau_i


def b_text(smax: float, P: dict, prefix: str = "") -> str:
    """Option B cell: the largest scrape_interval, or 做不到 with the reason."""
    smin, why, tau_i = s_floor(P)
    if smax <= 0:
        return "做不到：視窗比 scrape 耗時還短"
    if smax < smin:
        return f"做不到：要 ≤ {fmt_max(smax)}，但 {why}；先降低 scrape 耗時，或用方案 A"
    out = prefix + fmt_max(smax)
    if tau_i is not None and P["tau"] is not None and P["tau"] >= math.floor(smax * 10) / 10:
        out += f"（τ 要降到 {fmt(tau_i)}～{fmt_max(smax - 1)}）"
    elif tau_i is not None and P["tau"] is not None and P["tau"] < tau_i:
        out += f"（τ 調到 ≥ {fmt(tau_i)}）"
    if not P["measured"]:
        out += "，假設 d = S"
    return out


def range_contract(a, n: int) -> str:
    if a.stack == "vmalert":
        return "vm-range-window"
    return "range-window" if n >= 2 else "range-window-1sample"


def analyze(a, docs):
    recorded = {}
    for _, doc in docs:
        for g in doc.get("groups", []) or []:
            for r in g.get("rules", []) or []:
                if "record" in r:
                    recorded[r["record"]] = dur(g.get("interval")) or a.E
    cfg_jobs = getattr(a, "cfg_jobs", None)
    rows, fitted_docs = [], []
    for path, doc in docs:
        new = copy.deepcopy(doc)
        for gi, g in enumerate(doc.get("groups", []) or []):
            if g.get("type") not in (None, "", "prometheus"):
                continue
            qoff = dur(g.get("query_offset")) if g.get("query_offset") is not None else a.O
            for ri, r in enumerate(g.get("rules", []) or []):
                expr = str(r.get("expr", ""))
                name = r.get("alert") or r.get("record")
                ann = r.get("annotations") or {}
                keep = name in a.keep_range or str(ann.get("keep_range", "")).lower() in ("true", "yes", "1")
                new_expr = expr
                for sel in MS.selectors(expr):
                    if not sel["range"] or ":" in sel["range"]:
                        continue  # instant selector or subquery
                    metric = sel["metric"] or ""
                    text = expr[sel["pos"]:sel["range_end"]]
                    R = dur(sel["range"])
                    off = qoff + (dur(sel["offset"]) or 0)
                    job, defaults, notes = None, [], []
                    if metric in recorded:
                        S, d, src = recorded[metric], 0.0, f"recording rule（group interval {fmt(recorded[metric])}）"
                        P = {"S": S, "d": 0.0, "measured": True, "tau": None, "p99": None, "defaults": []}
                    else:
                        eq = [v for (l, op, v) in sel["matchers"] if l == "job" and op == "="]
                        rx = [v for (l, op, v) in sel["matchers"] if l == "job" and op != "="]
                        job = eq[0] if eq else None
                        P = job_params(a, job)
                        S, d = P["S"], P["d"]
                        defaults = P["defaults"]
                        if job is None:
                            src = f"預設（job 條件是正規式 `{rx[0]}`）" if rx else "預設（沒有 job 條件）"
                        else:
                            src = f"job {job}" + (f"（用預設值：{'、'.join(defaults)}）" if defaults else "")
                        if job and cfg_jobs is not None and job not in cfg_jobs:
                            notes.append(f"job {job} 不在 config 裡")
                    I = eff_interval(S, d)
                    if a.stack == "vmalert" and a.dedup and metric not in recorded:
                        I = max(I, a.dedup)  # VictoriaMetrics keeps one sample per -dedup.minScrapeInterval
                    k = k_for(a, job)
                    func = enclosing_func(expr, sel["pos"])
                    n = samples_needed(a, func, I, k)
                    need = need_window(a, I, d, off, k, n)
                    row = {"file": path, "group": g.get("name"), "rule": name, "metric": metric or text, "func": func, "n": n,
                           "R": R, "S": S, "d": d, "I": I, "offset": off, "need": need, "src": src, "ok": R >= need,
                           "job": job, "defaults": defaults, "notes": notes, "params": P, "k": k,
                           "contract": range_contract(a, n), "keep": keep}
                    if a.stack == "prometheus" and func in ANCHORED_OK:
                        row["need_anchored"] = need_window(a, I, d, off, k, 1)
                    if not row["ok"]:
                        fit = nice_up(need)
                        row["fit_R"] = fit
                        if not keep:
                            new_expr = new_expr.replace(text, text.replace(f"[{sel['range']}]", f"[{fmt(fit)}]", 1), 1)
                        if a.stack == "vmalert":
                            row["opt_b"] = {"eval_delay": max(0.0, (n + k) * I + a.latency - R),
                                            "scrape_interval": (R - max(0.0, a.latency - a.eval_delay)) / (n + k)}
                        elif P["measured"]:
                            row["opt_b"] = {"scrape_interval": (R - d) / (n + k) if R > d else 0.0,
                                            "scrape_interval_with_offset": R / (n + k), "query_offset": d}
                        else:  # d unknown, worst case d = S moves with S: R ≥ (n+k)·S + S
                            row["opt_b"] = {"scrape_interval": R / (n + k + 1),
                                            "scrape_interval_with_offset": R / (n + k), "query_offset": None}
                    rows.append(row)
                if new_expr != expr:
                    new["groups"][gi]["rules"][ri]["expr"] = new_expr
        fitted_docs.append((path, new))
    return rows, fitted_docs


INSTANT_SKIP = ("up", "ALERTS", "ALERTS_FOR_STATE")


def for_checks(a, docs) -> list[dict]:
    """Threshold rules with `for` that read an instant selector (no range) on a job that can miss scrapes.
    A failed scrape writes staleness markers, so the selector is empty at the next evaluation
    (docs/contracts.md#lookback); an empty result drops the pending alert and `for` starts over
    (docs/contracts.md#range-window). The combination is inferred, not tested on its own."""
    if a.stack != "prometheus":
        return []  # VictoriaMetrics / vmalert keep the last value for the query step: opposite risk
    recorded = {r["record"] for _, doc in docs for g in doc.get("groups", []) or [] for r in g.get("rules", []) or []
                if "record" in r}
    out = []
    for path, doc in docs:
        for g in doc.get("groups", []) or []:
            if g.get("type") not in (None, "", "prometheus"):
                continue
            qoff = dur(g.get("query_offset")) if g.get("query_offset") is not None else a.O
            for r in g.get("rules", []) or []:
                if "alert" not in r or not dur(r.get("for")):
                    continue
                expr = str(r.get("expr", ""))
                by_job: dict = {}
                for sel in MS.selectors(expr):
                    m = sel["metric"]
                    if sel["range"] is not None or m in recorded or (m or "").startswith(INSTANT_SKIP) \
                            or (m or "").startswith("scrape_"):
                        continue  # up / scrape_* are written on a failed scrape too; ranges are checked above
                    if MS.ABSENT_FUNCS & set(MS.enclosing_funcs(expr, sel["pos"])):
                        continue
                    eq = [v for (l, op, v) in sel["matchers"] if l == "job" and op == "="]
                    by_job.setdefault(eq[0] if eq else None, []).append(expr[sel["pos"]:sel["end"]])
                for job, sels in by_job.items():
                    k = k_for(a, job)
                    if k < 1:
                        continue
                    P = job_params(a, job)
                    I = eff_interval(P["S"], P["d"])
                    need = (1 + k) * I + max(0.0, P["d"] - qoff)
                    out.append({"file": path, "rule": r["alert"], "for": dur(r.get("for")), "job": job, "k": k,
                                "selectors": list(dict.fromkeys(sels)), "R": nice_up(need), "need": need,
                                "defaults": P["defaults"], "failrun": job in a.job_failrun})
    return out


def env_checks(a) -> list[tuple]:
    """(text, ok, 建議, contract id or None, 代價) per check."""
    out = []
    cost_tau = "τ 變大，壞掉的 target 要等更久才判定失敗"
    jobs = {None: (a.S, a.d if a.d is not None else a.S)}
    for j, S in a.job_interval.items():
        jobs[j] = (S, a.job_duration.get(j, a.d if a.d is not None else S))
    for j, d in a.job_duration.items():
        jobs.setdefault(j, (a.job_interval.get(j, a.S), d))
    for j in set(a.job_timeout) | set(a.exporter_deadline) | set(a.job_p99):
        jobs.setdefault(j, (a.job_interval.get(j, a.S), a.job_duration.get(j, a.d if a.d is not None else a.job_interval.get(j, a.S))))
    for j, (S, d) in jobs.items():
        who = f"job {j}" if j else "預設"
        measured = a.d is not None or j in a.job_duration
        tau = a.job_timeout.get(j, a.timeout)
        if tau is not None:
            out.append((f"`scrape_timeout` 小於 `scrape_interval`（{who}）：τ {fmt(tau)} < S {fmt(S)}", tau < S,
                        "" if tau < S else "Prometheus 會拒絕載入設定；vmagent 會自動截成 S、不報錯。把 τ 設在 S 以下",
                        "exporter-deadline", "" if tau < S else cost_tau))
            if measured:
                cut = d >= 0.95 * tau
                out.append((f"耗時數據沒有被逾時截斷（{who}）：d_max {fmt(d)} < 0.95 × τ（{fmt(0.95 * tau)}）", not cut,
                            "" if not cut else "d_max 幾乎等於 τ，代表有 scrape 逾時，真正的耗時不知道；先看 exporter 自己的耗時 metric，或暫時放寬 τ（仍 < S）再量",
                            "exporter-deadline", "" if not cut else "量測期間 τ 放寬，壞掉的 target 晚一點才判定失敗"))
            p99 = a.job_p99.get(j, a.p99)
            if p99 is not None:
                out.append((f"平常的 scrape 不碰 80% 預警線（{who}）：p99 {fmt(p99)} ≤ 0.8 × τ（{fmt(0.8 * tau)}）", p99 <= 0.8 * tau,
                            "" if p99 <= 0.8 * tau else f"τ 調到 ≥ {fmt(math.ceil(p99 / 0.8))}（仍要 < S），或降低 exporter 耗時",
                            "exporter-deadline", "" if p99 <= 0.8 * tau else cost_tau))
            x = a.exporter_deadline.get(j)
            if x is not None:
                out.append((f"exporter 自己的期限小於 τ（{who}）：X {fmt(x)} < τ {fmt(tau)}", x < tau,
                            "" if x < tau else f"把 exporter 的期限設成 τ − max(1s, 10%) = {fmt(tau - max(1, 0.1 * tau))}；"
                            "否則 Prometheus 先放棄，`up` 變 0、所有 series 消失，看起來像 exporter 掛了",
                            "exporter-deadline", "" if x < tau else "慢的 collector 會先被 exporter 放棄，少一部分 metric"))
        if not measured and j is None:
            continue  # d unknown: d = S is an assumption, checking d ≤ S against itself says nothing
        out.append((f"scrape 在 interval 內完成（{who}）：d {fmt(d)} ≤ S {fmt(S)}", d <= S,
                    "" if d <= S else f"把 `scrape_interval` 調到 ≥ {fmt(nice_up(d))}，或降低 scrape 耗時（exporter 查詢、`scrape_timeout`）",
                    "scrape-within-interval", "" if d <= S else "S 變大，偵測變慢、視窗要跟著拉長"))
        if a.stack == "prometheus":
            I = eff_interval(S, d)
            out.append((f"lookback（{who}）：I + d = {fmt(I + d)} < {fmt(a.lookback)}", I + d < a.lookback,
                        "" if I + d < a.lookback else "沒有 range 的 selector（例如 `up == 0`）會間歇回傳空；縮短 `scrape_interval`",
                        "lookback", "" if I + d < a.lookback else "scrape 次數變多"))
    if a.E < a.S:
        out.append((f"`evaluation_interval` 不小於 `scrape_interval`：E {fmt(a.E)} ≥ S {fmt(a.S)}", False,
                    "E < S 時同一批資料被重算，pending 依相位循環；一般取 E = S", None, "E 變大，偵測最多晚 E"))
    if a.T is not None:
        out.append((f"rule group 在 interval 內跑完：T {fmt(a.T)} ≤ E {fmt(a.E)}", a.T <= a.E,
                    "" if a.T <= a.E else f"把貴的 rule 拆到別的 group，或把 group `interval` 調到 ≥ {fmt(nice_up(a.T / 0.8))}",
                    "group-within-interval", "" if a.T <= a.E else "group 變多要管理；interval 變大偵測變慢"))
    return out


STD_INTERVALS = [15, 30, 60, 120, 300]


def baseline(a) -> str:
    """Suggested S / τ / X / E / R_min per job from measured durations (docs/tools/baseline.md, procedure)."""
    jobs = sorted(set(a.job_duration) | set(a.job_p99) | set(a.job_interval), key=str) or [None]
    out = ["## 建議的基準值（由量到的耗時推導）\n",
           "步驟見 [scrape 與 evaluation 的基準值怎麼定](https://hahasula.github.io/alert-trust/tools/baseline/)。"
           "0.8 是 alert-trust 的預警線，不是官方數字。\n",
           "| job | 目前 S | d_p99／d_max | 建議 S | τ | exporter 期限 X | E | k | R_min（無 offset） | R_min（`query_offset` = d_max） | lookback |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for j in jobs:
        S0 = a.job_interval.get(j, a.S)
        dmax = a.job_duration.get(j, a.d)
        if dmax is None:
            out.append(f"| {j or '預設'} | {fmt(S0)} | 沒給 | 要先量 d_max | | | | | | | |")
            continue
        p99 = a.job_p99.get(j, a.p99) or dmax
        S = S0 if dmax <= 0.8 * S0 else next((x for x in STD_INTERVALS if dmax <= 0.8 * x), math.ceil(dmax / 0.8))
        tau = max(math.ceil(dmax / 0.9), math.ceil(p99 / 0.8))  # d_max stays below the 0.95τ cut-off check
        while tau >= S:
            S = next((x for x in STD_INTERVALS if x > S), S * 2)
        X = tau - max(1, 0.1 * tau)
        k = k_for(a, j)
        r1, r2 = (2 + k) * S + dmax, (2 + k) * S
        note = "" if S == S0 else "（調大）"
        out.append(f"| {j or '預設'} | {fmt(S0)} | {fmt(p99)}／{fmt(dmax)} | {fmt(S)}{note} | {fmt(tau)} | {fmt(X)} | {fmt(S)} | {k} "
                   f"| {fmt(r1)} → `[{fmt(nice_up(r1))}]` | {fmt(r2)} → `[{fmt(nice_up(r2))}]` | {fmt(S + dmax)} {'< 5m' if S + dmax < a.lookback else '**≥ 5m**'} |")
    out.append("\n讀多個 job 的 rule group，E 取這些 S 的最大值；再量 T，確認 `T ≤ 0.8 E`。")
    return "\n".join(out) + "\n"


def escape_table_pipes(md: str) -> str:
    """A `|` inside a code span still splits a GitHub/MkDocs table cell: escape it on table rows."""
    out = []
    for line in md.split("\n"):
        if line.startswith("|"):
            parts = line.split("`")
            line = "`".join(p.replace("|", "\\|") if i % 2 else p for i, p in enumerate(parts))
        out.append(line)
    return "\n".join(out)


def verdict(ok: bool, cid: str | None) -> str:
    if ok:
        return "符合"
    return f"**不符合契約 [`{cid}`]({SITE}/contracts/#{cid})**" if cid else "**不符合建議**（docs/tools/baseline.md）"


SITE = C.SITE_URL if hasattr(C, "SITE_URL") else "https://hahasula.github.io/alert-trust"
COST_A = ("視窗變長，`rate` 對短暫尖峰的反應變慢（突波被稀釋成約 t/R），也可能改變 rule 的語意")
COST_B = ("`scrape_interval` 變短，scrape 次數與 series 寫入量照比例增加；慢的 exporter 更容易逾時。"
          "`query_offset` 讓每條 rule 都晚這麼久才判斷")


def advice(a, rows, checks, fors) -> list[str]:
    out = []
    for t, ok, fix, cid, cost in checks:
        if not ok:
            out.append(f"- {t}：不符合契約 `{cid}`。**建議**：{fix}。**代價**：{cost or '—'}。" if cid
                       else f"- {t}：不符合建議。**建議**：{fix}。**代價**：{cost or '—'}。")
    bad = [r for r in rows if not r["ok"]]
    if bad:
        names = list(dict.fromkeys(r["rule"] for r in bad))
        cids = sorted({r["contract"] for r in bad})
        out.append(f"- {len(names)} 條 rule 的視窗不符合契約 {'、'.join('`' + c + '`' for c in cids)}（{'、'.join('`' + n + '`' for n in names[:6])}）："
                   "視窗內可能少於需要的 sample 數，查詢回空、`for` 歸零。**建議**：見第 3 節，方案 A 改視窗、方案 B 改環境。"
                   f"**代價**：A {COST_A}；B {COST_B}。")
    if fors:
        names = list(dict.fromkeys(f["rule"] for f in fors))
        out.append(f"- {len(names)} 條門檻型 rule 有 `for`、selector 沒有 range（{'、'.join('`' + n + '`' for n in names[:6])}"
                   f"{' 等' if len(names) > 6 else ''}）：一次 scrape 失敗就讓 series 變 stale，pending 被刪、`for` 從頭算（推論）。"
                   "**建議**：見第 5 節，用 `last_over_time(x[R])` 撐過 k 次失敗。**代價**：真的消失的 series 要多 R 才不再成立。")
    dj = sorted({r["job"] for r in rows + fors if r.get("defaults") and r.get("job")})
    if dj:
        out.append(f"- job {', '.join(dj)} 沒有個別的數字，用了預設值。**建議**：補 `--job-duration`／`--job-interval`（或 `--config`）後重跑；"
                   "如果這些 job 根本不在 config 裡，先修 rule 的 job 條件（`check_metric_sources.py`）。")
    return out


def assumptions_for(a, rows, fors) -> list[str]:
    out = list(a.assumptions)
    used = [r for r in rows + fors if r.get("defaults")]
    by_job: dict = {}
    for r in used:
        by_job.setdefault(r.get("job") or "（沒有 job 條件）", set()).update(r["defaults"])
    if by_job:
        out.append("這些 job 沒有個別數字，用預設值：" + "；".join(f"{j}（{'、'.join(sorted(v))}）" for j, v in by_job.items()))
    if a.d is None and any("d = S" in r.get("defaults", []) for r in used):
        out.append("尖峰 scrape 耗時 d 沒給的 job 用最壞情況 d = S")
    cfg = getattr(a, "cfg_jobs", None)
    if cfg is not None:
        miss = sorted({r["job"] for r in rows + fors if r.get("job") and r["job"] not in cfg})
        if miss:
            out.append(f"job {', '.join(miss)} 不在 config 的 scrape job 裡（有：{', '.join(sorted(cfg))}）：這些 rule 可能根本沒有資料，"
                       "先跑 `check_metric_sources.py`")
    return out


def report(a, rows, checks, assumptions, fors=None) -> str:
    fors = fors or []
    out = ["# Rule 時間契約：目前的規格下哪些 rule 可信、要怎麼調\n"]
    if assumptions:
        out.append("**假設**（沒有給的數字）：" + "；".join(assumptions) + "。拿到實際值後重跑。\n")
    out.append("## 建議先做\n")
    adv = advice(a, rows, checks, fors)
    out += adv or ["- 目前的規格下，環境與每條 rule 都符合契約。"]
    out.append("\n## 1. 環境本身\n")
    bad_env = [c for c in checks if not c[1]]
    out.append(f"{len(bad_env)} 項不符合。環境不符合契約時，rule 的視窗算得再對也不可靠，建議先處理。\n" if bad_env
               else "環境本身符合契約。\n")
    out.append("| 檢查 | 結果 | 建議 | 代價 |\n|---|---|---|---|")
    out += [f"| {t} | {verdict(ok, cid)} | {fix or '—'} | {cost or '—'} |" for t, ok, fix, cid, cost in checks]
    ok_rows = [r for r in rows if r["ok"]]
    bad = [r for r in rows if not r["ok"]]
    out.append("\n## 2. 在目前規格下符合契約的 rule\n")
    if ok_rows:
        out.append("| rule | 函式（需要幾個 sample） | metric | 來源 | 視窗 R | 需要 ≥ | 邊際 |\n|---|---|---|---|---|---|---|")
        out += [f"| `{r['rule']}` | `{r['func'] or '—'}`（{r['n']}） | `{r['metric']}` | {r['src']} | {fmt(r['R'])} | {fmt(r['need'])} | +{fmt(r['R'] - r['need'])} |"
                for r in ok_rows]
    else:
        out.append("沒有。")
    out.append("\n## 3. 不符合契約的 rule：兩個方向\n")
    if not bad:
        out.append("沒有。")
    else:
        out.append(f"方案 A：改 rule，配合目前的環境。**代價**：{COST_A}。方案 B：不改 rule，改環境。**代價**：{COST_B}。"
                   "B 會檢查做不做得到：`scrape_interval` 要大於 τ，τ 要 ≥ max(d_max ÷ 0.9, p99 ÷ 0.8)"
                   "（[基準值](https://hahasula.github.io/alert-trust/tools/baseline/)）。\n")
        A = lambda r: "保留（`keep_range`），見下一節" if r.get("keep") else f"`[{fmt(r['fit_R'])}]`"  # noqa: E731
        if a.stack == "vmalert":
            out.append("| rule | metric | 契約 | 視窗 R | 需要 ≥ | A：改成 | B：`eval_delay` 至少 | B：或 `scrape_interval` 至多 |\n|---|---|---|---|---|---|---|---|")
            for r in bad:
                b = r["opt_b"]
                out.append(f"| `{r['rule']}` | `{r['metric']}` | `{r['contract']}` | {fmt(r['R'])} | {fmt(r['need'])} | {A(r)} "
                           f"| {fmt(b['eval_delay'])} | {b_text(b['scrape_interval'], r['params'])} |")
        else:
            out.append("| rule | metric | 來源 | 契約 | 視窗 R | 需要 ≥ | A：改成 | B：`scrape_interval` 至多 | B：或設 `query_offset` ≥ d，`scrape_interval` 至多 |\n|---|---|---|---|---|---|---|---|---|")
            for r in bad:
                b = r["opt_b"]
                s1 = b_text(b["scrape_interval"], r["params"])
                qo = fmt(b["query_offset"]) if b["query_offset"] is not None else "S"
                s2 = b_text(b["scrape_interval_with_offset"], r["params"], f"`query_offset` {qo}，")
                if r["src"].startswith("recording"):
                    s1, s2 = f"上游 group `interval` 至多 {fmt_max(b['scrape_interval_with_offset'])}", "—"
                src = r["src"] + (f"；{'；'.join(r['notes'])}" if r.get("notes") else "")
                out.append(f"| `{r['rule']}` | `{r['metric']}` | {src} | `{r['contract']}` | {fmt(r['R'])} | {fmt(r['need'])} | {A(r)} | {s1} | {s2} |")
        out.append("\nA 的視窗是把需要的長度往上取整。視窗是刻意選的（例如就是要「最近 1 分鐘」），在 rule 加 annotation `keep_range: \"true\"`"
                   "或用 `--keep-range <rule>`，A 就不會改它，改看下一節。")
        keep = [r for r in bad if r.get("keep")]
        if keep:
            out.append("\n### 視窗要保留時\n")
            out.append("視窗內實際看得到的 sample 數約是 R ÷ I；`scrape_interval` 比 R 的一半還長時，「最近 R」的精度其實由 "
                       "`scrape_interval` 決定，不是由 R 決定。保留語意的做法：\n")
            out.append("| rule | 函式 | R | 實際約幾個 sample | B：該 job `scrape_interval` 至多 | C：`anchored`（Prometheus 3.x 實驗功能） |\n|---|---|---|---|---|---|")
            for r in keep:
                if a.stack == "vmalert":
                    c = "MetricsQL 已經用視窗前的 sample（n = 1）"
                elif "need_anchored" in r:
                    c = ("可行：`" + r["func"] + "(…[" + fmt(r["R"]) + "] anchored)`，需要 ≥ " + fmt(r["need_anchored"])
                         + "；要開 `--enable-feature=promql-extended-range-selectors`，不外推")
                    if r["R"] < r["need_anchored"]:
                        no_d = r["need_anchored"] - max(0.0, r["d"] - r["offset"])
                        c = (f"單獨不夠（需要 ≥ {fmt(r['need_anchored'])}）；再加 `query_offset` ≥ {fmt(r['d'])} 後需要 ≥ {fmt(no_d)}，"
                             + ("可行" if r["R"] >= no_d else "仍不夠，要縮短 `scrape_interval`"))
                else:
                    c = "不適用這個函式"
                bs = b_text(r["opt_b"]["scrape_interval"], r["params"])
                out.append(f"| `{r['rule']}` | `{r['func'] or '—'}` | {fmt(r['R'])} | {r['R'] / r['I']:.1f} | {bs} | {c} |")
            out.append("\n`anchored` 用視窗前 lookback 內的最後一個 sample 當起點，視窗內只要 1 個 sample；它不外推，"
                       "值是視窗兩端的實際差，比 `rate` 更接近「最近 R 內增加多少」的語意。實驗功能，見"
                       "[各 range 函式需要的視窗](https://hahasula.github.io/alert-trust/contracts/#range-functions)。")
        raw = [r for r in bad if not r["src"].startswith("recording")]
        out.append("\n## 4. 一次讓所有 rule 符合契約的環境設定（方案 B）\n")
        if a.stack == "vmalert":
            dmin = max(r["opt_b"]["eval_delay"] for r in bad)
            out.append(f"- `-rule.evalDelay`（或 group `eval_delay`）至少 {fmt(math.ceil(dmin))}；代價是每條 rule 都晚這麼久才看到資料。")
        elif raw:
            by_src: dict[str, list] = {}
            for r in raw:
                by_src.setdefault(r["job"] or r["src"], []).append(r)
            out.append("| 來源 | 不加 offset：`scrape_interval` 至多 | 或 `query_offset` ≥ | 加 offset 後 `scrape_interval` 至多 |\n|---|---|---|---|")
            for src, rs in by_src.items():
                P = rs[0]["params"]
                smax = min(r["opt_b"]["scrape_interval"] for r in rs)
                so = min(r["opt_b"]["scrape_interval_with_offset"] for r in rs)
                qo = fmt(max(r["d"] for r in rs)) if P["measured"] else "S"
                out.append(f"| {('job ' + src) if rs[0]['job'] else src} | {b_text(smax, P)} | {qo} | {b_text(so, P)} |")
            out.append("\n`query_offset` 要 Prometheus 2.53 以上（全域 `rule_query_offset` 或 group `query_offset`）；代價是每條 rule 都晚這麼久才判斷。")
        if any(r["src"].startswith("recording") for r in bad):
            out.append("- 讀 recording rule 的 rule：調整的是上游 recording rule 所在 group 的 `interval`，不是 `scrape_interval`。")
    if fors:
        out.append("\n## 5. 門檻型 rule 的 `for` 與 scrape 失敗（建議）\n")
        out.append("Scrape 失敗時 Prometheus 寫 staleness marker，沒有 range 的 selector 在下一次評估立刻變空"
                   "（[lookback](https://hahasula.github.io/alert-trust/contracts/#lookback)，有實驗）；查詢回空時 pending 被刪除、"
                   "`for` 歸零（[range-window](https://hahasula.github.io/alert-trust/contracts/#range-window)，有實驗）。"
                   "兩者合起來：`for` 期間只要失敗一次，告警就從頭等——**這是推論，沒有單獨實驗**。"
                   "`up` 與 `scrape_*` 在失敗時照樣寫入，不受影響，所以不列。\n")
        out.append("| rule | `for` | 沒有 range 的 selector | job（k） | 建議 |\n|---|---|---|---|---|")
        for f in fors:
            sels = "<br>".join(f"`{x}`" for x in f["selectors"][:3]) + (" 等" if len(f["selectors"]) > 3 else "")
            ex = f["selectors"][0]
            dflt = f"；用預設值：{'、'.join(f['defaults'])}" if f["defaults"] else ""
            kk = f"k = {f['k']}" + ("（觀察到的連續失敗）" if f["failrun"] else "（`--k`）")
            out.append(f"| `{f['rule']}` | {fmt(f['for'])} | {sels} | {f['job'] or '—'}（{kk}{dflt}） "
                       f"| `last_over_time({ex}[{fmt(f['R'])}])`（[`range-window-1sample`](https://hahasula.github.io/alert-trust/contracts/#range-window-1sample)：(1+k)·I + max(0, d − O) = {fmt(f['need'])}） |")
        out.append("\n表格只寫出第一個 selector 的改法；同一條 rule 列出的每個 selector 都要包。"
                   "`last_over_time` 保留最後一個 sample 的值，語意和原本的 instant selector 一樣；`max_over_time`／`min_over_time` "
                   "也能撐過失敗，但會改變判斷（任何一點超過／全部超過）。**代價**：series 真的消失（target 拿掉、exporter 停了）後，"
                   "規則還會成立最多 R。另一個做法 `keep_firing_for` 只在告警已經 firing 後有用（避免 resolve 再 fire），"
                   "幫不了還在 pending 的告警。")
    return escape_table_pipes("\n".join(out)) + "\n"


def apply_config(a, paths: list[str]) -> None:
    """Fill S / τ / E / O and per-job scrape_interval / scrape_timeout from prometheus.yml; explicit flags win."""
    g, jobs = {}, []
    for p in paths:
        g.update(MS.load_global(p))
        jobs += MS.load_config(p)
    gS = dur(g.get("scrape_interval")) or 60.0      # Prometheus defaults: 1m / 10s / 1m
    gT = dur(g.get("scrape_timeout")) or 10.0
    if a.S is None:
        a.S = gS
    if a.timeout is None:
        a.timeout = min(gT, a.S)
    if a.E is None:
        a.E = dur(g.get("evaluation_interval")) or 60.0
    if a.O is None:
        a.O = dur(g.get("rule_query_offset")) or 0.0
    for j in jobs:
        S = dur(j.get("scrape_interval")) or gS
        a.job_interval.setdefault(j["job"], S)
        # unset scrape_timeout: the global one, capped at the job's interval (Prometheus config loading)
        a.job_timeout.setdefault(j["job"], dur(j.get("scrape_timeout")) or min(gT, S))
    a.cfg_jobs = {j["job"] for j in jobs}
    a.assumptions.append(f"S、τ、E 由 config 讀入（global：S {fmt(gS)}、τ {fmt(gT)}、E {fmt(a.E)}；"
                         f"job：{', '.join(f'{j} S {fmt(a.job_interval[j])}／τ {fmt(a.job_timeout[j])}' for j in sorted(a.cfg_jobs))}）")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rules", nargs="+", default=[], help="rule 檔；只要 --baseline 時可省略")
    ap.add_argument("--stack", choices=["prometheus", "vmalert"], required=True)
    ap.add_argument("--config", action="append", default=[],
                    help="prometheus.yml：讀 global 與每個 job 的 scrape_interval／scrape_timeout、evaluation_interval、rule_query_offset")
    ap.add_argument("--scrape-interval", help="S：預設的 scrape_interval（有 --config 時可省略）")
    ap.add_argument("--scrape-duration", help="d：尖峰 scrape 耗時（scrape_duration_seconds）；沒給就假設 d = S")
    ap.add_argument("--job-interval", action="append", default=[], help="job=S，個別 job 的 scrape_interval")
    ap.add_argument("--job-duration", action="append", default=[], help="job=d，個別 job 的尖峰 scrape 耗時")
    ap.add_argument("--eval-interval", help="E：evaluation_interval（group 沒寫 interval 時；有 --config 時可省略）")
    ap.add_argument("--group-duration", help="T：尖峰 rule group 耗時")
    ap.add_argument("--scrape-timeout", help="τ：預設的 scrape_timeout")
    ap.add_argument("--job-timeout", action="append", default=[], help="job=τ")
    ap.add_argument("--scrape-p99", help="平常的 scrape 耗時（p99）")
    ap.add_argument("--job-p99", action="append", default=[], help="job=p99")
    ap.add_argument("--exporter-deadline", action="append", default=[], help="job=X，exporter 自己的查詢期限")
    ap.add_argument("--job-failrun", action="append", default=[], help="job=N，觀察到的最長連續失敗次數（k 至少取這個）")
    ap.add_argument("--baseline", action="store_true", help="依量到的耗時輸出建議的 S、τ、X、E、R_min")
    ap.add_argument("--query-offset", help="O：全域 rule_query_offset（Prometheus；預設 0，或 --config 的值）")
    ap.add_argument("--k", type=int, default=1, help="容忍漏掉幾次 scrape（建議至少 1）")
    ap.add_argument("--lookback", default="5m")
    ap.add_argument("--latency", default="0s", help="L：資料延遲（vmalert）")
    ap.add_argument("--eval-delay", default="30s", help="D：-rule.evalDelay（vmalert，預設 30s）")
    ap.add_argument("--query-step", default="5m", help="vmalert -datasource.queryStep（預設 5m）")
    ap.add_argument("--dedup-interval", help="VictoriaMetrics 的 -dedup.minScrapeInterval；大於 S 時實際間隔用它")
    ap.add_argument("--keep-range", action="append", default=[], help="這條 rule 的視窗是刻意選的，不要建議拉長（也可在 rule 加 annotation keep_range: \"true\"）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--write-fitted", help="把方案 A 改好的 rule 寫到這個檔（不動原檔；註解不會保留）")
    a = ap.parse_args()
    a.assumptions = []
    a.S, a.d, a.E, a.O = dur(a.scrape_interval), dur(a.scrape_duration), dur(a.eval_interval), dur(a.query_offset)
    a.T, a.lookback, a.latency, a.eval_delay = dur(a.group_duration), dur(a.lookback), dur(a.latency), dur(a.eval_delay)
    a.job_interval, a.job_duration = kv(a.job_interval), kv(a.job_duration)
    a.timeout, a.p99, a.dedup = dur(a.scrape_timeout), dur(a.scrape_p99), dur(a.dedup_interval)
    a.query_step = dur(a.query_step)
    a.job_timeout, a.job_p99, a.exporter_deadline = kv(a.job_timeout), kv(a.job_p99), kv(a.exporter_deadline)
    a.job_failrun = {k: int(v) for k, _, v in (x.partition("=") for x in a.job_failrun)}
    a.cfg_jobs = None
    if a.config:
        apply_config(a, a.config)
    if a.S is None or a.E is None:
        ap.error("要給 --scrape-interval 與 --eval-interval，或用 --config prometheus.yml")
    if a.O is None:
        a.O = 0.0
    if not a.rules and not a.baseline:
        ap.error("要給 --rules，或用 --baseline")
    if a.T is None:
        a.assumptions.append("rule group 耗時 T 沒給，沒檢查 T ≤ E")
    if a.stack == "vmalert" and a.latency == 0:
        a.assumptions.append("資料延遲 L 沒給，用 0")
    docs = load(a.rules)
    rows, fitted = analyze(a, docs)
    fors = for_checks(a, docs)
    checks = env_checks(a)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "contracts-fit.md"), "w", encoding="utf-8") as f:
        f.write(report(a, rows, checks, assumptions_for(a, rows, fors), fors) if a.rules else "# 基準值\n")
        if a.baseline:
            f.write("\n" + baseline(a))
    if a.write_fitted:
        groups = [g for _, doc in fitted for g in doc.get("groups", []) or []]
        with open(a.write_fitted, "w", encoding="utf-8") as f:
            f.write("# 由 tools/fit_contracts.py 依目前環境調整視窗（方案 A）；原檔未改動。\n")
            yaml.safe_dump({"groups": groups}, f, allow_unicode=True, sort_keys=False, width=1000)
    bad = sum(1 for r in rows if not r["ok"])
    env_bad = sum(1 for c in checks if not c[1])
    print(f"建議：環境 {env_bad} 項、rule 視窗 {bad} 條、`for` 與 scrape 失敗 {len(fors)} 條（符合契約的 rule 視窗 {len(rows) - bad} 條）"
          f" → {a.out}/contracts-fit.md 的「建議先做」")
    return 1 if bad or env_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
