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
d_p99 / d_max / failure runs (procedure in docs/tools/baseline.md); --rules is optional then. Floors: τ ≥ 2s,
X ≥ 1s, S not below 15s unless the input S is lower. With --stack vmalert it uses R_min = (n+k)·I + max(0, L − D),
I = max(S, dedup).

Other rules the report follows:
  - --config-kind vmagent (default with --stack vmalert): vmagent silently sets scrape_timeout > scrape_interval to
    scrape_interval; the effective τ = min(τ, S) is used everywhere and the clamp is reported.
  - d unknown: worst case d = τ (effective) when τ is known, else d = S (parse/write after the HTTP read is not
    limited by τ; it is small and ignored, inferred).
  - Job matchers are resolved against the config's job names (regex: fullmatch); several matches use the worst job.
    A selector whose job is in no config job (or that --sources marks job_mismatch / label_mismatch / dropped /
    live_missing) has no data: 「沒有資料，契約不適用」, never 符合.
  - Subqueries `(...)[R:res]` are ranges: n from the outer function, I = max(res, the inner data interval).
  - Exporter deadline X: --exporter-deadline, else the default of the exporter (--job-exporter, or inferred from the
    metric prefixes of that job's rules), see EXPORTER_DEADLINE.
  - --prom-version below 2.53: `query_offset` is shown as unavailable.
  - vmalert with --latency unknown: L = S of the job (conservative), stated as an assumption.
  - Section 5 skips rules that detect absence (absent(), unless, up == 0, absence_covered_by, *Missing names).
  - --inventory env/<name>.inventory.yaml (tools/inventory.py): stack (ruler → --stack, version, evaluation_interval,
    rule_query_offset, global scrape_interval / scrape_timeout), per job interval, effective timeout, exporter,
    exporter deadline (from: user), measured d_max / d_p99, rules' keep_range, and rules that never / always fire
    (no data). Precedence: flags > inventory from: user / confirmed > --config > other inventory values.
  - Neither flags, --config nor --inventory give S / E: Prometheus defaults scrape_interval 1m, scrape_timeout 10s,
    evaluation_interval 1m are used and listed as assumptions (no error).
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


def latency(a, S: float | None) -> float:
    """vmalert data latency L: --latency, else L = S of the job (conservative; 0 would be optimistic)."""
    if a.latency is not None:
        return a.latency
    return S if S is not None else (a.S or 0.0)


def need_window(a, I: float, d: float, offset: float, k: int | None = None, n: int = 2, S: float | None = None) -> float:
    """range-window / range-window-1sample: (n+k)·I + max(0, d − O); vmalert: (n+k)·I + max(0, L − D)."""
    k = a.k if k is None else k
    if a.stack == "vmalert":
        return (n + k) * I + max(0.0, latency(a, S) - a.eval_delay)
    return (n + k) * I + max(0.0, d - offset)


def load(files: list[str]) -> list[tuple[str, dict]]:
    return [(p, C._load(p) or {}) for p in files]


# ---------- exporter deadlines (docs/tools/baseline.md「Exporter 怎麼用這個期限」) ----------
# (kind, value, flag, text). kind "fixed": the exporter ignores the scrape-timeout header and uses its own
# deadline; kind "offset": it reads the header and stops at τ − offset. Refs (catalog/refs.d):
#   mysqld_exporter   bl-my-header, bl-my-offset-default, my-offset
#   blackbox_exporter bl-bb-header, bl-bb-offset-flag, bl-bb-default-120
#   mongodb_exporter  mx-flag-timeout-offset
#   postgres_exporter bl-pg-coll-timeout-flag, bl-pg-ctx-background, pg-timeout
#   redis_exporter    rx-flag-timeout
EXPORTER_DEADLINE = {
    "redis_exporter": ("fixed", 15.0, "--connection-timeout", "redis_exporter `--connection-timeout` 預設 15s，不讀 header"),
    "postgres_exporter": ("fixed", 60.0, "--collection-timeout", "postgres_exporter `--collection-timeout` 預設 1m，不讀 header"),
    "mysqld_exporter": ("offset", 0.25, "--timeout-offset", "mysqld_exporter 讀 header：τ − `--timeout-offset`（預設 0.25s）"),
    "mongodb_exporter": ("offset", 1.0, "--web.timeout-offset", "mongodb_exporter 讀 header：τ − `--web.timeout-offset`（預設 1s）"),
    "blackbox_exporter": ("offset", 0.5, "--timeout-offset",
                          "blackbox_exporter 讀 header：min(module `timeout`, τ − `--timeout-offset`（預設 0.5s））"),
}
PREFIX_EXPORTER = {"redis_": "redis_exporter", "pg_": "postgres_exporter", "mysql_": "mysqld_exporter",
                   "mongodb_": "mongodb_exporter", "probe_": "blackbox_exporter"}


def suggested_x(tau: float) -> float:
    """docs/tools/baseline.md step 6: X = τ − max(1s, 10%·τ), never below 1s."""
    return max(1.0, tau - max(1.0, 0.1 * tau))


def exporter_deadline(a, job, tau: float | None) -> tuple[float | None, str]:
    """(X, where it came from). --exporter-deadline wins; else the exporter's default."""
    if job in a.exporter_deadline:
        return a.exporter_deadline[job], "--exporter-deadline"
    exp = a.job_exporter.get(job)
    if exp not in EXPORTER_DEADLINE or tau is None:
        return None, ""
    kind, v, _, text = EXPORTER_DEADLINE[exp]
    how = (getattr(a, "job_exporter_src", None) or {}).get(job) or \
        ("--job-exporter" if job in a.job_exporter_flag else "由 rule 的 metric 前綴推斷")
    return (v if kind == "fixed" else tau - v), f"{text}；exporter 是{how}"


def infer_exporters(a, docs) -> None:
    """job -> exporter from the metric prefixes (redis_/pg_/mysql_/mongodb_/probe_) of the rules on that job."""
    seen: dict = {}
    for _, doc in docs:
        for g in doc.get("groups", []) or []:
            for r in g.get("rules", []) or []:
                for sel in MS.selectors(str(r.get("expr", ""))):
                    m = sel["metric"] or ""
                    exp = next((e for p, e in PREFIX_EXPORTER.items() if m.startswith(p)), None)
                    if not exp:
                        continue
                    job, _, _, nodata = resolve_job(a, sel["matchers"])
                    if job is not None and not nodata:
                        seen.setdefault(job, set()).add(exp)
    for job, exps in seen.items():
        if job in a.job_exporter:
            continue
        if len(exps) == 1:
            a.job_exporter[job] = next(iter(exps))
        else:
            a.assumptions.append(f"job {job} 的 rule 用到多種 exporter 的 metric（{', '.join(sorted(exps))}），"
                                 "沒有套 exporter 期限的預設值；用 `--job-exporter` 指定")


# ---------- jobs ----------

def job_params(a, job) -> dict:
    """S, d, τ (effective), p99 for a job, and which of them fell back to the defaults (reported per row).
    d unknown: worst case d = τ when τ is known (a scrape longer than τ times out), else d = S."""
    defaults = []
    if job in a.job_interval:
        S = a.job_interval[job]
    else:
        S = a.S
        defaults.append(f"S {fmt(a.S)}")
    tau = a.job_timeout.get(job, a.timeout)
    if tau is not None and S is not None:
        tau = min(tau, S)
    if tau is not None and job not in a.job_timeout:
        defaults.append(f"τ {fmt(tau)}")
    assumed = None
    if job in a.job_duration:
        d, measured = a.job_duration[job], True
    elif a.d is not None:
        d, measured = a.d, True
        defaults.append(f"d {fmt(a.d)}")
    elif tau is not None:
        d, measured, assumed = tau, False, "τ"
        defaults.append("d = τ")
    else:
        d, measured, assumed = S, False, "S"
        defaults.append("d = S")
    return {"S": S, "d": d, "measured": measured, "assumed": assumed, "tau": tau,
            "p99": a.job_p99.get(job, a.p99), "defaults": defaults}


def known_jobs(a) -> set | None:
    if getattr(a, "cfg_jobs", None) is not None:
        return set(a.cfg_jobs)
    return None


def resolve_job(a, matchers) -> tuple:
    """(job, source text, notes, no_data) for a selector's job matchers. Exact and regex matchers are resolved
    against the config's job names (fullmatch, as PromQL does); several matches use the worst job (largest I + d);
    none means the selector has no data."""
    jm = [(op, v) for (l, op, v) in matchers if l == "job"]
    if not jm:
        return None, "預設（沒有 job 條件）", [], False
    cond = ", ".join(f'job{op}"{v}"' for op, v in jm)
    eq = [v for op, v in jm if op == "="]
    known = known_jobs(a)
    if known is None:
        if eq:
            return eq[0], f"job {eq[0]}", [], False
        cands = set(a.job_interval) | set(a.job_duration) | set(a.job_timeout)
        matched = sorted(j for j in cands if all(MS.matches(op, v, j) for op, v in jm))
        if not matched:
            return None, f"預設（job 條件是正規式 `{cond}`）", [], False
    else:
        matched = sorted(j for j in known if all(MS.matches(op, v, j) for op, v in jm))
        if not matched:
            job = eq[0] if eq else None
            note = f"job {job} 不在 config 裡" if job else f"`{cond}` 選不到 config 裡任何 job"
            return job, f"`{cond}`", [note], True
    if len(matched) == 1:
        j = matched[0]
        return j, f"job {j}" + ("" if eq else f"（`{cond}` 對到）"), [], False

    def cost(j):
        P = job_params(a, j)
        return eff_interval(P["S"], P["d"]) + P["d"]
    worst = max(matched, key=cost)
    return worst, f"job {worst}（`{cond}` 對到 {', '.join(matched)}，取最壞的）", [], False


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
        out += f"，假設 d = {P.get('assumed') or 'S'}"
    return out


def range_contract(a, n: int) -> str:
    if a.stack == "vmalert":
        return "vm-range-window"
    return "range-window" if n >= 2 else "range-window-1sample"


# ---------- subqueries ----------

_SUBQ = re.compile(r"\[\s*([0-9][0-9smhdwy.]*)\s*:\s*([0-9smhdwy.]*)\s*\]")


def subqueries(expr: str) -> list[dict]:
    """`(inner)[R:res]` (and `f(...)[R:res]`): the inner expression is evaluated every res (default: the evaluation
    interval) over R. Returns [{"open", "close", "R", "res", "end", "func"}]; a subquery written directly on a
    selector (`x[5m:1m]`) is left to selectors()."""
    out = []
    for m in _SUBQ.finditer(expr):
        i = m.start() - 1
        while i >= 0 and expr[i].isspace():
            i -= 1
        if i < 0 or expr[i] != ")":
            continue
        depth, j = 0, i
        while j >= 0:
            if expr[j] == ")":
                depth += 1
            elif expr[j] == "(":
                depth -= 1
                if depth == 0:
                    break
            j -= 1
        if j < 0:
            continue
        # f(...)[R:res]: the subquery covers the call, so the outer function is the one around f
        w = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", expr[:j])
        start = w.start(1) if w and w.group(1).lower() not in MS.GROUPING else j
        funcs = [f for f in MS.enclosing_funcs(expr, start)]
        out.append({"open": start, "close": i, "R": m.group(1), "res": m.group(2) or None, "end": m.end(),
                    "func": funcs[0] if funcs else "", "range_start": m.start()})
    return out


def in_subquery(subs: list[dict], pos: int) -> bool:
    return any(s["open"] <= pos < s["close"] for s in subs)


def source_status(a, rule: str, metric: str) -> str | None:
    """--sources (check_metric_sources.py JSON): a status that means the selector returns nothing."""
    return (getattr(a, "sources_empty", None) or {}).get((rule, metric))


def analyze(a, docs):
    recorded = {}
    for _, doc in docs:
        for g in doc.get("groups", []) or []:
            for r in g.get("rules", []) or []:
                if "record" in r:
                    recorded[r["record"]] = dur(g.get("interval")) or a.E
    rows, fitted_docs = [], []

    def sel_params(sel, rule_name):
        """(P, S, d, I, k, src, job, defaults, notes, no_data) for one selector."""
        metric = sel["metric"] or ""
        if metric in recorded:
            S = recorded[metric]
            P = {"S": S, "d": 0.0, "measured": True, "assumed": None, "tau": None, "p99": None, "defaults": []}
            return P, S, 0.0, S, a.k, f"recording rule（group interval {fmt(S)}）", None, [], [], False
        job, src, notes, nodata = resolve_job(a, sel["matchers"])
        P = job_params(a, job)
        defaults = P["defaults"] if not nodata else []
        if defaults and job is not None:
            src += f"（用預設值：{'、'.join(defaults)}）"
        st = source_status(a, rule_name, metric)
        if st and not nodata:
            nodata = True
            notes = notes + [f"check_metric_sources：{st}"]
        inv_nd = (getattr(a, "nodata_rules", None) or {}).get(rule_name)
        if inv_nd and not nodata:
            nodata = True
            notes = notes + [inv_nd]
        I = eff_interval(P["S"], P["d"])
        if a.stack == "vmalert" and a.dedup:
            I = max(I, a.dedup)  # VictoriaMetrics keeps one sample per -dedup.minScrapeInterval
        return P, P["S"], P["d"], I, k_for(a, job), src, job, defaults, notes, nodata

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
                seen = set()
                sels = MS.selectors(expr)
                subs = subqueries(expr)
                cands = []  # (text, R text, range text, func, P, S, d, I, k, src, job, defaults, notes, nodata, offset, metric)
                sq_cands = []
                for sel in sels:
                    if not sel["range"]:
                        continue  # instant selector (inside a subquery it is handled with the subquery)
                    text = expr[sel["pos"]:sel["range_end"]]
                    if text in seen:
                        continue
                    seen.add(text)
                    rng, _, res = sel["range"].partition(":")
                    P, S, d, I, k, src, job, defaults, notes, nodata = sel_params(sel, name)
                    if ":" in sel["range"]:  # subquery directly on a selector
                        res_s = dur(res) if res else a.E
                        I = max(I, res_s)
                        src += f"；subquery 解析度 {fmt(res_s)}"
                    off = qoff + (dur(sel["offset"]) or 0)
                    cands.append((text, rng, f"[{sel['range']}]", enclosing_func(expr, sel["pos"]), P, S, d, I, k, src,
                                  job, defaults, notes, nodata, off, sel["metric"] or text))
                for sq in subs:
                    inner = [x for x in sels if sq["open"] <= x["pos"] < sq["close"] and x["range"] is None]
                    ranged = [x for x in sels if sq["open"] <= x["pos"] < sq["close"] and x["range"] is not None]
                    res_s = dur(sq["res"]) if sq["res"] else a.E
                    ps_inner = [sel_params(x, name) for x in inner]
                    ps = ps_inner + [sel_params(x, name) for x in ranged]
                    if ps:
                        worst = max(ps, key=lambda p: p[3] + p[2])
                    else:
                        worst = ({"S": res_s, "d": 0.0, "measured": True, "assumed": None, "tau": None, "p99": None,
                                  "defaults": []}, res_s, 0.0, res_s, a.k, "subquery", None, [], [], False)
                    P, S, d, I, k, src, job, defaults, notes, nodata = worst
                    # instant selectors inside change every I_sel; a range function inside gives a value every res
                    I = max([res_s] + [p[3] for p in ps_inner])
                    k = max([p[4] for p in ps] or [a.k])
                    nodata = bool(ps) and all(p[9] for p in ps)
                    d = max([p[2] for p in ps] or [0.0])
                    text = expr[sq["open"]:sq["end"]]
                    short = text if len(text) <= 70 else text[:40] + " … " + text[-25:]
                    src = f"subquery：內層每 {fmt(res_s)} 評估一次，I = max(解析度, 內層資料間隔) = {fmt(I)}；{src}"
                    sq_cands.append((text, sq["R"], expr[sq["range_start"]:sq["end"]], sq["func"], P, S, d, I, k, src,
                                     job, defaults, notes, nodata, qoff, short))
                for (text, rng, rtxt, func, P, S, d, I, k, src, job, defaults, notes, nodata, off, metric) in sq_cands + cands:
                    R = dur(rng)
                    n = samples_needed(a, func, I, k)
                    need = need_window(a, I, d, off, k, n, S)
                    row = {"file": path, "group": g.get("name"), "rule": name, "metric": metric, "func": func, "n": n,
                           "R": R, "S": S, "d": d, "I": I, "offset": off, "need": need, "src": src,
                           "ok": R >= need and not nodata, "nodata": nodata,
                           "job": job, "defaults": defaults, "notes": notes, "params": P, "k": k,
                           "contract": range_contract(a, n), "keep": keep}
                    if a.stack == "prometheus" and func in ANCHORED_OK:
                        row["need_anchored"] = need_window(a, I, d, off, k, 1)
                    if not row["ok"] and not nodata:
                        fit = nice_up(need)
                        row["fit_R"] = fit
                        if not keep:
                            new_rtxt = rtxt.replace(rng, fmt(fit), 1)
                            new_expr = new_expr.replace(text, text.replace(rtxt, new_rtxt, 1), 1)
                        if a.stack == "vmalert":
                            L = latency(a, S)
                            row["opt_b"] = {"eval_delay": max(0.0, (n + k) * I + L - R),
                                            "scrape_interval": (R - max(0.0, L - a.eval_delay)) / (n + k),
                                            "dedup_bound": bool(a.dedup and a.dedup > S)}
                        elif P["measured"] or P.get("assumed") == "τ":
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
_ABSENCE_FUNC = re.compile(r"\babsent(_over_time)?\s*\(|\bunless\b")
_UP_ZERO = re.compile(r"(?<![\w:])up\s*(\{[^}]*\})?\s*==\s*0(?![\d.])")
_ABSENCE_NAME = re.compile(r"(Missing|Absent|Disappear|Gone|NoData|Vanish)", re.I)


def detects_absence(rule: dict) -> str | None:
    """Why a rule is about missing data (so last_over_time would hide exactly what it looks for), or None."""
    expr = str(rule.get("expr", ""))
    m = _ABSENCE_FUNC.search(expr)
    if m:
        return f"用 `{m.group(0).rstrip('(').strip()}` 偵測消失"
    if _UP_ZERO.search(expr):
        return "用 `up == 0` 偵測 scrape 失敗"
    ann = rule.get("annotations") or {}
    if ann.get("absence_covered_by"):
        return f"有 `absence_covered_by`（{ann['absence_covered_by']}）：消失由別條 rule 偵測，這條是成對的一半"
    if _ABSENCE_NAME.search(str(rule.get("alert") or "")):
        return "名稱表示偵測資料消失"
    return None


def for_checks(a, docs, skipped: list | None = None) -> list[dict]:
    """Threshold rules with `for` that read an instant selector (no range) on a job that can miss scrapes.
    A failed scrape writes staleness markers, so the selector is empty at the next evaluation
    (docs/contracts.md#lookback); an empty result drops the pending alert and `for` starts over
    (docs/contracts.md#range-window). The combination is inferred, not tested on its own.
    Rules that detect absence are skipped (appended to `skipped` with the reason): last_over_time would make a
    series that really disappeared look present for R more."""
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
                subs = subqueries(expr)
                by_job: dict = {}
                for sel in MS.selectors(expr):
                    m = sel["metric"]
                    if sel["range"] is not None or m in recorded or (m or "").startswith(INSTANT_SKIP) \
                            or (m or "").startswith("scrape_") or in_subquery(subs, sel["pos"]):
                        continue  # up / scrape_* are written on a failed scrape too; ranges / subqueries are checked above
                    if MS.ABSENT_FUNCS & set(MS.enclosing_funcs(expr, sel["pos"])):
                        continue
                    job, _, _, nodata = resolve_job(a, sel["matchers"])
                    if nodata or source_status(a, r["alert"], m or "") or r["alert"] in (getattr(a, "nodata_rules", None) or {}):
                        continue  # no data at all: reported in the no-data section instead
                    by_job.setdefault(job, []).append(expr[sel["pos"]:sel["end"]])
                if not by_job:
                    continue
                why = detects_absence(r)
                if why:
                    if skipped is not None:
                        skipped.append({"file": path, "rule": r["alert"], "why": why})
                    continue
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


COST_TAU_DOWN = "τ 變小，偶爾慢的 scrape 更容易逾時；或 S 變大，偵測變慢"
COST_TAU_UP = "τ 變大，壞掉的 target 要等更久才判定失敗"


def env_checks(a) -> list[tuple]:
    """(text, ok, 建議, contract id or None, 代價) per check. The text always states the actual relation
    (e.g. 「X 15s ≥ τ 10s」), never the wanted one, so a failed row cannot read like a pass."""
    out = []
    jobs = {None} | set(a.job_interval) | set(a.job_duration) | set(a.job_timeout) | set(a.exporter_deadline) \
        | set(a.job_p99) | set(a.job_exporter) | set(getattr(a, "tau_clamped", {}) or {})
    for j in sorted(jobs, key=lambda x: (x is not None, str(x))):
        P = job_params(a, j)
        S, d, tau, measured = P["S"], P["d"], P["tau"], P["measured"]
        who = f"job {j}" if j else "預設"
        raw_tau = a.job_timeout.get(j, a.timeout)  # as configured (Prometheus keeps τ > S and refuses the config)
        if tau is not None:
            conf = (getattr(a, "tau_clamped", {}) or {}).get(j)
            if conf is not None:
                out.append((f"`scrape_timeout` 設定 {fmt(conf)} > S {fmt(S)}（{who}）：vmagent 不報錯、自動改成 S，"
                            f"有效 τ = {fmt(tau)}，以下的檢查都用有效 τ", False,
                            f"把 τ 明確設在 S 以下（例如 {fmt(max(1.0, math.floor(0.8 * S)))}），不要依賴截斷",
                            "exporter-deadline", COST_TAU_DOWN))
            elif raw_tau >= S:
                rel = "=" if raw_tau == S else ">"
                fix = ("Prometheus 會拒絕載入設定；" if a.config_kind == "prometheus" and raw_tau > S else "") + \
                    "契約要 τ < S：把 τ 設在 S 以下，或把 S 調大"
                out.append((f"`scrape_timeout` 與 `scrape_interval`（{who}）：τ {fmt(raw_tau)} {rel} S {fmt(S)}", False, fix,
                            "exporter-deadline", COST_TAU_DOWN))
            else:
                out.append((f"`scrape_timeout` 與 `scrape_interval`（{who}）：τ {fmt(tau)} < S {fmt(S)}", True, "",
                            "exporter-deadline", ""))
            if measured:
                cut = d >= 0.95 * tau
                out.append((f"耗時數據是否被逾時截斷（{who}）：d_max {fmt(d)} {'≥' if cut else '<'} 0.95 × τ（{fmt(0.95 * tau)}）",
                            not cut,
                            "" if not cut else "d_max 幾乎等於 τ，代表有 scrape 逾時，真正的耗時不知道；先看 exporter 自己的耗時 metric，"
                            "或暫時放寬 τ（仍 < S）再量（docs/tools/baseline.md 步驟 3）",
                            None, "" if not cut else "量測期間 τ 放寬，壞掉的 target 晚一點才判定失敗"))
            p99 = a.job_p99.get(j, a.p99)
            if p99 is not None:
                ok = p99 <= 0.8 * tau
                out.append((f"平常的 scrape 與 80% 預警線（{who}）：p99 {fmt(p99)} {'≤' if ok else '>'} 0.8 × τ（{fmt(0.8 * tau)}）", ok,
                            "" if ok else f"τ 調到 ≥ {fmt(math.ceil(p99 / 0.8))}（仍要 < S），或降低 exporter 耗時",
                            None, "" if ok else COST_TAU_UP))
            x, xsrc = exporter_deadline(a, j, tau)
            if x is not None:
                ok = x < tau
                exp = a.job_exporter.get(j)
                kind = EXPORTER_DEADLINE.get(exp, ("fixed",))[0]
                flag = EXPORTER_DEADLINE[exp][2] if exp in EXPORTER_DEADLINE else "exporter 的期限參數"
                if ok:
                    fix = ""
                elif kind == "offset":
                    fix = f"exporter 讀 header 卻沒有餘裕：把 `{flag}` 調大，或把 τ 調大（仍 < S）"
                else:
                    fix = f"把 `{flag}` 設成 τ − max(1s, 10%) = {fmt(suggested_x(tau))}；否則 Prometheus／vmagent 先放棄，" \
                          "`up` 變 0、所有 series 消失，看起來像 exporter 掛了"
                out.append((f"exporter 期限（{who}）：X {fmt(x)} {'<' if ok else '≥'} τ {fmt(tau)}"
                            + (f"（{xsrc}）" if xsrc and xsrc != "--exporter-deadline" else ""), ok, fix,
                            "exporter-deadline", "" if ok else "慢的查詢會先被 exporter 放棄，少一部分 metric（collector 失敗），`up` 仍是 1"))
        if a.stack == "vmalert" and a.dedup and S is not None and a.dedup > S:
            out.append((f"`-dedup.minScrapeInterval` 與 S（{who}）：dedup {fmt(a.dedup)} > S {fmt(S)}，實際資料間隔是 {fmt(a.dedup)}",
                        False, f"縮短 S 不會讓視窗變短；把 S 調成 {fmt(a.dedup)}（scrape 少一半，exporter 對 DB 的查詢也少），"
                        "或降低 dedup", None, "dedup 變小時儲存的 sample 變多；多台 vmagent 時 dedup 要等於 `scrape_interval` 才能去重"))
        if not measured:
            continue  # d unknown: d = τ / S is an assumption, checking d ≤ S against itself says nothing
        ok = d <= S
        out.append((f"scrape 在 interval 內完成（{who}）：d {fmt(d)} {'≤' if ok else '>'} S {fmt(S)}", ok,
                    "" if ok else f"把 `scrape_interval` 調到 ≥ {fmt(nice_up(d))}，或降低 scrape 耗時（exporter 查詢、`scrape_timeout`）",
                    "scrape-within-interval", "" if ok else "S 變大，偵測變慢、視窗要跟著拉長"))
        if a.stack == "prometheus":
            I = eff_interval(S, d)
            ok = I + d < a.lookback
            out.append((f"lookback（{who}）：I + d = {fmt(I + d)} {'<' if ok else '≥'} {fmt(a.lookback)}", ok,
                        "" if ok else "沒有 range 的 selector（例如 `up == 0`）會間歇回傳空；縮短 `scrape_interval`",
                        "lookback", "" if ok else "scrape 次數變多"))
    if a.E < a.S:
        out.append((f"`evaluation_interval` 與 `scrape_interval`：E {fmt(a.E)} < S {fmt(a.S)}", False,
                    "E < S 時同一批資料被重算，pending 依相位循環；一般取 E = S", None, "E 變大，偵測最多晚 E"))
    if a.T is not None:
        ok = a.T <= a.E
        out.append((f"rule group 耗時與 interval：T {fmt(a.T)} {'≤' if ok else '>'} E {fmt(a.E)}", ok,
                    "" if ok else f"把貴的 rule 拆到別的 group，或把 group `interval` 調到 ≥ {fmt(nice_up(a.T / 0.8))}",
                    "group-within-interval", "" if ok else "group 變多要管理；interval 變大偵測變慢"))
    return out


STD_INTERVALS = [15, 30, 60, 120, 300]


def x_cell(a, job, tau: float) -> str:
    """Exporter deadline column of the baseline: header-reading exporters get the offset, others a value."""
    exp = a.job_exporter.get(job)
    X = suggested_x(tau)
    if exp in EXPORTER_DEADLINE:
        kind, v, flag, _ = EXPORTER_DEADLINE[exp]
        if kind == "offset":
            return f"讀 header：τ − `{flag}` {fmt(v)} = {fmt(max(0.0, tau - v))}（預設即可）"
        return f"`{flag}` 設成 {fmt(X)}（預設 {fmt(v)}，不讀 header）"
    return f"{fmt(X)}（只有不讀 header 的 exporter 要設；讀 header 的用 offset 參數）"


def baseline(a) -> str:
    """Suggested S / τ / X / E / R_min per job from measured durations (docs/tools/baseline.md, procedure)."""
    jobs = sorted(set(a.job_duration) | set(a.job_p99) | set(a.job_interval), key=str) or [None]
    vm = a.stack == "vmalert"
    out = ["## 建議的基準值（由量到的耗時推導）\n",
           "步驟見 [scrape 與 evaluation 的基準值怎麼定](https://hahasula.github.io/alert-trust/tools/baseline/)。"
           "0.8 是 alert-trust 的預警線，不是官方數字。下限：τ ≥ 2s、X ≥ 1s，S 不低於 15s（除非現在就更低）。\n"]
    if vm:
        L_txt = fmt(a.latency) if a.latency is not None else "S（沒給，保守假設）"
        out.append(f"vmalert：R_min = (n+k)·I + max(0, L − D)，I = max(S, dedup)，L = {L_txt}、D = {fmt(a.eval_delay)}、"
                   f"dedup = {fmt(a.dedup) if a.dedup else '沒給（有設且 > S 時 I 會變大）'}；"
                   f"(1+k)·I < `-datasource.queryStep` {fmt(a.query_step)} 時 n = 1。\n")
        out += ["| job | 目前 S | d_p99／d_max | 建議 S | τ | exporter 期限 X | E | k | I | R_min |",
                "|---|---|---|---|---|---|---|---|---|---|"]
    else:
        out += ["| job | 目前 S | d_p99／d_max | 建議 S | τ | exporter 期限 X | E | k | R_min（無 offset） | R_min（`query_offset` = d_max） | lookback |",
                "|---|---|---|---|---|---|---|---|---|---|---|"]
    for j in jobs:
        S0 = a.job_interval.get(j, a.S)
        dmax = a.job_duration.get(j, a.d)
        if dmax is None:
            out.append(f"| {j or '預設'} | {fmt(S0)} | 沒給 | 要先量 d_max | | | | | | |" + ("" if vm else " |"))
            continue
        p99 = a.job_p99.get(j, a.p99) or dmax
        tau_now = a.job_timeout.get(j, a.timeout)
        if tau_now is not None and S0 is not None:
            tau_now = min(tau_now, S0)
        cut = tau_now is not None and dmax >= 0.95 * tau_now
        S = S0 if dmax <= 0.8 * S0 else next((x for x in STD_INTERVALS if dmax <= 0.8 * x), math.ceil(dmax / 0.8))
        S = max(S, min(15.0, S0))
        tau = max(math.ceil(dmax / 0.9), math.ceil(p99 / 0.8), 2)  # d_max stays below the 0.95τ cut-off check
        while tau >= S:
            S = next((x for x in STD_INTERVALS if x > S), S * 2)
        tau_cell = fmt(tau) if not cut else f"≥ {fmt(tau)}（d_max ≈ 目前 τ {fmt(tau_now)}，被截斷：先放寬 τ 再量）"
        ge = "≥ " if cut else ""
        k = k_for(a, j)
        note = "" if S == S0 else "（調大）"
        head = f"| {j or '預設'} | {fmt(S0)} | {fmt(p99)}／{fmt(dmax)} | {fmt(S)}{note} | {tau_cell} | {x_cell(a, j, tau)} | {fmt(S)} | {k} "
        if vm:
            I = max(S, a.dedup or 0.0)
            n = 1 if (1 + k) * I < a.query_step else 2
            L = a.latency if a.latency is not None else S
            r = (n + k) * I + max(0.0, L - a.eval_delay)
            dd = "" if not (a.dedup and a.dedup > S) else f"（dedup {fmt(a.dedup)} > S：S 調成 {fmt(a.dedup)} 或降低 dedup）"
            out.append(head + f"| {fmt(I)}{dd} | {ge}{fmt(r)}（n = {n}）→ `[{fmt(nice_up(r))}]` |")
        else:
            r1, r2 = (2 + k) * S + dmax, (2 + k) * S
            out.append(head + f"| {ge}{fmt(r1)} → `[{fmt(nice_up(r1))}]` | {ge}{fmt(r2)} → `[{fmt(nice_up(r2))}]` "
                       f"| {fmt(S + dmax)} {'< 5m' if S + dmax < a.lookback else '**≥ 5m**'} |")
    out.append("\n讀多個 job 的 rule group，E 取這些 S 的最大值；再量 T，確認 `T ≤ 0.8 E`。")
    if not vm and not query_offset_available(a):
        out.append(f"\nPrometheus {a.prom_version_text} 沒有 `query_offset`（要 2.53 以上），只看「無 offset」那欄。")
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


def parse_version(v: str | None) -> tuple | None:
    if not v:
        return None
    nums = re.findall(r"\d+", str(v))
    return tuple(int(x) for x in nums[:3]) if nums else None


def query_offset_available(a) -> bool:
    """`query_offset` / `rule_query_offset` exist from Prometheus 2.53 (docs/tools/baseline.md, p255-query-offset)."""
    v = getattr(a, "prom_version", None)
    return v is None or v >= (2, 53)


SITE = C.SITE_URL if hasattr(C, "SITE_URL") else "https://hahasula.github.io/alert-trust"
COST_A = ("視窗變長，`rate` 對短暫尖峰的反應變慢（突波被稀釋成約 t/R），也可能改變 rule 的語意")
COST_B = ("`scrape_interval` 變短，scrape 次數與 series 寫入量照比例增加；慢的 exporter 更容易逾時。"
          "`query_offset` 讓每條 rule 都晚這麼久才判斷")
COST_DEDUP = ("dedup 變小，儲存的 sample 變多；多台 vmagent（HA）時 `-dedup.minScrapeInterval` 要等於 `scrape_interval` 才能去重")


def advice(a, rows, checks, fors) -> list[str]:
    out = []
    for t, ok, fix, cid, cost in checks:
        if not ok:
            out.append(f"- {t}：不符合契約 `{cid}`。**建議**：{fix}。**代價**：{cost or '—'}。" if cid
                       else f"- {t}：不符合建議。**建議**：{fix}。**代價**：{cost or '—'}。")
    nd = [r for r in rows if r.get("nodata")]
    if nd:
        names = list(dict.fromkeys(r["rule"] for r in nd))
        out.append(f"- {len(names)} 條 rule 沒有資料，契約不適用（{'、'.join('`' + n + '`' for n in names[:6])}"
                   f"{' 等' if len(names) > 6 else ''}）：job 條件對不上 config 的 scrape job，或 `check_metric_sources.py` 判定選不到 series。"
                   "**建議**：先修 job 條件／label（見 metric-sources 報告），再重跑這個檢查。**代價**：改 rule 要一起改用到同一個 job 的 dashboard。")
    bad = [r for r in rows if not r["ok"] and not r.get("nodata")]
    if bad:
        names = list(dict.fromkeys(r["rule"] for r in bad))
        cids = sorted({r["contract"] for r in bad})
        out.append(f"- {len(names)} 條 rule 的視窗不符合契約 {'、'.join('`' + c + '`' for c in cids)}（{'、'.join('`' + n + '`' for n in names[:6])}）："
                   "視窗內可能少於需要的 sample 數，查詢回空、`for` 歸零。**建議**：見第 3 節，方案 A 改視窗、方案 B 改環境。"
                   f"**代價**：A {COST_A}；B {COST_B}。")
        if a.stack == "vmalert" and any(r["opt_b"].get("dedup_bound") for r in bad):
            out.append(f"- `-dedup.minScrapeInterval` {fmt(a.dedup)} 大於 S：方案 B 縮短 `scrape_interval` 沒有用，實際間隔由 dedup 決定。"
                       f"**建議**：降低 dedup（見第 3 節），或用方案 A。**代價**：{COST_DEDUP}。")
    if fors:
        names = list(dict.fromkeys(f["rule"] for f in fors))
        out.append(f"- {len(names)} 條門檻型 rule 有 `for`、selector 沒有 range（{'、'.join('`' + n + '`' for n in names[:6])}"
                   f"{' 等' if len(names) > 6 else ''}）：一次 scrape 失敗就讓 series 變 stale，pending 被刪、`for` 從頭算（推論）。"
                   "**建議**：見第 5 節，用 `last_over_time(x[R])` 撐過 k 次失敗。**代價**：真的消失的 series 要多 R 才不再成立。")
    dj = sorted({r["job"] for r in rows + fors if r.get("defaults") and r.get("job") and not r.get("nodata")})
    if dj:
        out.append(f"- job {', '.join(dj)} 沒有個別的數字，用了預設值。**建議**：補 `--job-duration`／`--job-interval`（或 `--config`）後重跑；"
                   "如果這些 job 根本不在 config 裡，先修 rule 的 job 條件（`check_metric_sources.py`）。")
    return out


def assumptions_for(a, rows, fors) -> list[str]:
    out = list(a.assumptions)
    used = [r for r in rows + fors if r.get("defaults") and not r.get("nodata")]
    by_job: dict = {}
    for r in used:
        by_job.setdefault(r.get("job") or "（沒有 job 條件）", set()).update(r["defaults"])
    if by_job:
        out.append("這些 job 沒有個別數字，用預設值：" + "；".join(f"{j}（{'、'.join(sorted(v))}）" for j, v in by_job.items()))
    if a.d is None and any("d = τ" in r.get("defaults", []) for r in used):
        out.append("尖峰 scrape 耗時 d 沒給的 job 用最壞情況 d = τ（有效 `scrape_timeout`）：超過 τ 的 scrape 會逾時失敗，"
                   "所以成功的 scrape 不會比 τ 久（τ 之後的 parse／寫入不計，通常很小，推論）")
    if a.d is None and any("d = S" in r.get("defaults", []) for r in used):
        out.append("尖峰 scrape 耗時 d 與 τ 都沒給的 job 用最壞情況 d = S")
    cfg = getattr(a, "cfg_jobs", None)
    if cfg is not None:
        miss = sorted({r["job"] for r in rows + fors if r.get("job") and r["job"] not in cfg})
        if miss:
            out.append(f"job {', '.join(miss)} 不在 config 的 scrape job 裡（有：{', '.join(sorted(cfg))}）：這些 rule 沒有資料，"
                       "契約不適用；先跑 `check_metric_sources.py`")
    return out


def report(a, rows, checks, assumptions, fors=None, skipped=None) -> str:
    fors = fors or []
    skipped = skipped or []
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
    out.append("| 檢查（實際的關係） | 結果 | 建議 | 代價 |\n|---|---|---|---|")
    out += [f"| {t} | {verdict(ok, cid)} | {fix or '—'} | {cost or '—'} |" for t, ok, fix, cid, cost in checks]
    ok_rows = [r for r in rows if r["ok"]]
    nd_rows = [r for r in rows if r.get("nodata")]
    bad = [r for r in rows if not r["ok"] and not r.get("nodata")]
    out.append("\n## 2. 在目前規格下符合契約的 rule\n")
    if ok_rows:
        out.append("| rule | 函式（需要幾個 sample） | metric | 來源 | 視窗 R | 需要 ≥ | 邊際 |\n|---|---|---|---|---|---|---|")
        out += [f"| `{r['rule']}` | `{r['func'] or '—'}`（{r['n']}） | `{r['metric']}` | {r['src']} | {fmt(r['R'])} | {fmt(r['need'])} | +{fmt(r['R'] - r['need'])} |"
                for r in ok_rows]
    else:
        out.append("沒有。")
    if nd_rows:
        out.append("\n### 沒有資料，契約不適用\n")
        out.append("這些 selector 選不到任何 series，視窗夠不夠長都一樣回傳空，所以不列為符合。先修 job 條件或 label。\n")
        out.append("| rule | metric | job 條件 | 原因 |\n|---|---|---|---|")
        out += [f"| `{r['rule']}` | `{r['metric']}` | {r['src']} | 沒有資料，契約不適用：{'；'.join(r['notes']) or '—'} |" for r in nd_rows]
    out.append("\n## 3. 不符合契約的 rule：兩個方向\n")
    qo_ok = query_offset_available(a)
    qo_na = f"不可用：Prometheus {getattr(a, 'prom_version_text', '')} 沒有 `query_offset`（要 2.53 以上）"
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
                if b.get("dedup_bound"):
                    s = (f"縮短 S 沒有用：dedup {fmt(a.dedup)} > S {fmt(r['S'])}，實際間隔是 dedup；改成 `-dedup.minScrapeInterval` "
                         f"至多 {fmt_max(b['scrape_interval'])}（S 也要 ≤ dedup）。代價：{COST_DEDUP}")
                else:
                    s = b_text(b["scrape_interval"], r["params"])
                out.append(f"| `{r['rule']}` | `{r['metric']}` | `{r['contract']}` | {fmt(r['R'])} | {fmt(r['need'])} | {A(r)} "
                           f"| {fmt(b['eval_delay'])} | {s} |")
        else:
            out.append("| rule | metric | 來源 | 契約 | 視窗 R | 需要 ≥ | A：改成 | B：`scrape_interval` 至多 | B：或設 `query_offset` ≥ d，`scrape_interval` 至多 |\n|---|---|---|---|---|---|---|---|---|")
            for r in bad:
                b = r["opt_b"]
                s1 = b_text(b["scrape_interval"], r["params"])
                qo = fmt(b["query_offset"]) if b["query_offset"] is not None else "S"
                s2 = b_text(b["scrape_interval_with_offset"], r["params"], f"`query_offset` {qo}，") if qo_ok else qo_na
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
            if a.stack == "vmalert":
                out.append("| rule | 函式 | R | 實際約幾個 sample | B：該 job `scrape_interval` 至多 |\n|---|---|---|---|---|")
            else:
                out.append("| rule | 函式 | R | 實際約幾個 sample | B：該 job `scrape_interval` 至多 | C：`anchored`（Prometheus 3.x 實驗功能） |\n|---|---|---|---|---|---|")
            for r in keep:
                bs = b_text(r["opt_b"]["scrape_interval"], r["params"])
                if r["opt_b"].get("dedup_bound"):
                    bs = (f"縮短 S 沒有用（dedup {fmt(a.dedup)} > S）；`-dedup.minScrapeInterval` 至多 "
                          f"{fmt_max(r['opt_b']['scrape_interval'])}")
                if a.stack == "vmalert":
                    out.append(f"| `{r['rule']}` | `{r['func'] or '—'}` | {fmt(r['R'])} | {r['R'] / r['I']:.1f} | {bs} |")
                    continue
                v = getattr(a, "prom_version", None)
                if v is not None and v < (3,):
                    c = f"不可用：Prometheus {a.prom_version_text} 沒有 `anchored`（3.x 實驗功能）"
                elif "need_anchored" in r:
                    c = ("可行：`" + r["func"] + "(…[" + fmt(r["R"]) + "] anchored)`，需要 ≥ " + fmt(r["need_anchored"])
                         + "；要開 `--enable-feature=promql-extended-range-selectors`，不外推")
                    if r["R"] < r["need_anchored"]:
                        no_d = r["need_anchored"] - max(0.0, r["d"] - r["offset"])
                        c = (f"單獨不夠（需要 ≥ {fmt(r['need_anchored'])}）；"
                             + (f"再加 `query_offset` ≥ {fmt(r['d'])} 後需要 ≥ {fmt(no_d)}，"
                                + ("可行" if r["R"] >= no_d else "仍不夠，要縮短 `scrape_interval`") if qo_ok
                                else "要縮短 `scrape_interval`（這個版本沒有 `query_offset`）"))
                else:
                    c = "不適用這個函式"
                out.append(f"| `{r['rule']}` | `{r['func'] or '—'}` | {fmt(r['R'])} | {r['R'] / r['I']:.1f} | {bs} | {c} |")
            if a.stack != "vmalert":
                out.append("\n`anchored` 用視窗前 lookback 內的最後一個 sample 當起點，視窗內只要 1 個 sample；它不外推，"
                           "值是視窗兩端的實際差，比 `rate` 更接近「最近 R 內增加多少」的語意。實驗功能，見"
                           "[各 range 函式需要的視窗](https://hahasula.github.io/alert-trust/contracts/#range-functions)。")
            else:
                out.append("\nMetricsQL 已經用視窗前的 sample（n = 1），沒有 `anchored` 這一項。")
        raw = [r for r in bad if not r["src"].startswith("recording")]
        out.append("\n## 4. 一次讓所有 rule 符合契約的環境設定（方案 B）\n")
        if a.stack == "vmalert":
            dmin = max(r["opt_b"]["eval_delay"] for r in bad)
            out.append(f"- `-rule.evalDelay`（或只對一個 group 設 group `eval_delay`）至少 {fmt(math.ceil(dmin))}；"
                       "代價是這些 rule 都晚這麼久才看到資料。")
            if any(r["opt_b"].get("dedup_bound") for r in bad):
                dm = min(r["opt_b"]["scrape_interval"] for r in bad if r["opt_b"].get("dedup_bound"))
                out.append(f"- 或把 `-dedup.minScrapeInterval` 降到至多 {fmt_max(dm)}（S 也要 ≤ dedup）；縮短 `scrape_interval` 本身沒有用。"
                           f"代價：{COST_DEDUP}。")
        elif raw:
            by_src: dict[str, list] = {}
            for r in raw:
                by_src.setdefault(r["job"] or r["src"], []).append(r)
            out.append("| 來源 | 不加 offset：`scrape_interval` 至多 | 或 `query_offset` ≥ | 加 offset 後 `scrape_interval` 至多 |\n|---|---|---|---|")
            for src, rs in by_src.items():
                P = rs[0]["params"]
                smax = min(r["opt_b"]["scrape_interval"] for r in rs)
                so = min(r["opt_b"]["scrape_interval_with_offset"] for r in rs)
                qo = fmt(max(r["d"] for r in rs)) if (P["measured"] or P.get("assumed") == "τ") else "S"
                if qo_ok:
                    out.append(f"| {('job ' + src) if rs[0]['job'] else src} | {b_text(smax, P)} | {qo} | {b_text(so, P)} |")
                else:
                    out.append(f"| {('job ' + src) if rs[0]['job'] else src} | {b_text(smax, P)} | {qo_na} | — |")
            out.append("\n`query_offset` 要 Prometheus 2.53 以上（全域 `rule_query_offset` 或 group `query_offset`）；代價是每條 rule 都晚這麼久才判斷。")
        if any(r["src"].startswith("recording") for r in bad):
            out.append("- 讀 recording rule 的 rule：調整的是上游 recording rule 所在 group 的 `interval`，不是 `scrape_interval`。")
    if fors or skipped:
        out.append("\n## 5. 門檻型 rule 的 `for` 與 scrape 失敗（建議）\n")
        out.append("Scrape 失敗時 Prometheus 寫 staleness marker，沒有 range 的 selector 在下一次評估立刻變空"
                   "（[lookback](https://hahasula.github.io/alert-trust/contracts/#lookback)，有實驗）；查詢回空時 pending 被刪除、"
                   "`for` 歸零（[range-window](https://hahasula.github.io/alert-trust/contracts/#range-window)，有實驗）。"
                   "兩者合起來：`for` 期間只要失敗一次，告警就從頭等——**這是推論，沒有單獨實驗**。"
                   "`up` 與 `scrape_*` 在失敗時照樣寫入，不受影響，所以不列。\n")
    if fors:
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
                   "幫不了還在 pending 的告警。`--write-fitted` 只寫方案 A 的視窗，不寫這一節的改法。")
    if skipped:
        out.append("\n**不適用**（偵測資料消失的 rule，不建議 `last_over_time`：它會讓真的消失的 series 多 R 才被發現）：\n")
        out.append("| rule | 為什麼不適用 |\n|---|---|")
        out += [f"| `{s['rule']}` | {s['why']} |" for s in skipped]
    return escape_table_pipes("\n".join(out)) + "\n"


def apply_config(a, paths: list[str]) -> None:
    """Fill S / τ / E / O and per-job scrape_interval / scrape_timeout from prometheus.yml (or a vmagent
    -promscrape.config); explicit flags win. An unset scrape_timeout is the global one capped at the job's S
    (Prometheus config loading). An explicit scrape_timeout > S: Prometheus refuses the config (kept, flagged);
    vmagent sets it to S silently (recorded in a.tau_clamped, docs/tools/baseline.md#defaults)."""
    g, jobs = {}, []
    for p in paths:
        g.update(MS.load_global(p))
        jobs += MS.load_config(p)
    gS = dur(g.get("scrape_interval")) or 60.0      # Prometheus / vmagent defaults: 1m / 10s / 1m
    gT_raw = dur(g.get("scrape_timeout"))
    gT = gT_raw or 10.0
    if a.S is None:
        a.S = gS
    if a.timeout is None:
        if gT_raw is not None and gT_raw > a.S and a.config_kind == "vmagent":
            a.tau_clamped[None] = gT_raw
        a.timeout = min(gT, a.S) if (gT_raw is None or a.config_kind == "vmagent") else gT
    if a.E is None:
        a.E = dur(g.get("evaluation_interval")) or 60.0
    if a.O is None:
        a.O = dur(g.get("rule_query_offset")) or 0.0
    for j in jobs:
        S = dur(j.get("scrape_interval")) or gS
        a.job_interval.setdefault(j["job"], S)
        if j["job"] in a.job_timeout:
            continue
        raw = dur(j.get("scrape_timeout"))
        if raw is None:
            a.job_timeout[j["job"]] = min(gT, a.job_interval[j["job"]])
        elif raw > a.job_interval[j["job"]] and a.config_kind == "vmagent":
            a.tau_clamped[j["job"]] = raw
            a.job_timeout[j["job"]] = a.job_interval[j["job"]]
        else:
            a.job_timeout[j["job"]] = raw
    a.cfg_jobs = {j["job"] for j in jobs}
    clamp = "；vmagent 把 > S 的 τ 改成 S" if a.tau_clamped else ""
    a.assumptions.append(f"S、τ、E 由 config 讀入（{a.config_kind}；global：S {fmt(gS)}、τ {fmt(gT)}、E {fmt(a.E)}；"
                         f"job：{', '.join(f'{j} S {fmt(a.job_interval[j])}／有效 τ {fmt(a.job_timeout[j])}' for j in sorted(a.cfg_jobs))}{clamp}）")


def apply_inventory(a, inv: dict, user_only: bool) -> None:
    """Fill S / τ / E / O, per-job interval / effective timeout / exporter / measured d and p99, keep_range and
    the never/always-firing rules from env/<name>.inventory.yaml (tools/inventory.py). Flags already set win.
    user_only=True takes only values marked from: user / confirmed (they also beat --config); the second pass
    takes the rest (from: file / default / hint)."""
    import inventory_use as IU
    take = IU.is_user if user_only else IU.known
    st = inv.get("stack") or {}
    if a.E is None and take(st.get("evaluation_interval")):
        a.E = dur(IU.val(st["evaluation_interval"]))
    if a.O is None and take(st.get("rule_query_offset")):
        a.O = dur(IU.val(st["rule_query_offset"]))
    if a.S is None and take(st.get("scrape_interval")):
        a.S = dur(IU.val(st["scrape_interval"]))
    if a.timeout is None and take(st.get("scrape_timeout")):
        a.timeout = dur(IU.val(st["scrape_timeout"]))
    used = []
    for j in IU.config_jobs(inv) if IU.has_config(inv) else []:
        name = j["name"]
        iv, to = j.get("interval") or {}, j.get("timeout") or {}
        if take(iv) and name not in a.job_interval:
            a.job_interval[name] = dur(IU.val(iv))
        S = a.job_interval.get(name, a.S)
        if take(to) and name not in a.job_timeout:
            raw = dur(IU.val(to))
            eff = dur(to.get("effective")) if to.get("effective") and not IU.is_user(to) else raw
            if a.config_kind == "vmagent" and S is not None and raw is not None and raw > S:
                a.tau_clamped[name] = raw
                eff = S
            a.job_timeout[name] = eff
        ex = j.get("exporter") or {}
        if take(ex) and IU.val(ex) in EXPORTER_DEADLINE and name not in a.job_exporter:
            a.job_exporter[name] = IU.val(ex)
            a.job_exporter_src[name] = "inventory 使用者確認" if IU.is_user(ex) else \
                f"inventory 推測（{ex.get('basis') or '目錄卡片'}，要確認）"
        xd = j.get("exporter_deadline") or {}
        if IU.is_user(xd) and IU.val(xd) and name not in a.exporter_deadline:
            a.exporter_deadline[name] = dur(IU.val(xd))
        m = j.get("measured") or {}
        if m.get("from") == "user":
            if m.get("d_max") and name not in a.job_duration:
                a.job_duration[name] = dur(m["d_max"])
            if m.get("d_p99") and name not in a.job_p99:
                a.job_p99[name] = dur(m["d_p99"])
        if name in a.job_interval:
            used.append(name)
    if user_only:
        return
    if IU.has_config(inv):
        names = IU.job_names(inv)
        a.cfg_jobs = names if a.cfg_jobs is None else set(a.cfg_jobs) | names
    a.keep_range = list(dict.fromkeys(list(a.keep_range) + IU.keep_range(inv)))
    a.nodata_rules = dict(IU.nodata_rules(inv), **(getattr(a, "nodata_rules", None) or {}))
    ev = st.get("evaluation_interval") or {}
    if IU.has_config(inv) or IU.known(ev):
        a.assumptions.append(f"S、τ、E、job 由 inventory `{inv.get('_path')}` 讀入（E {fmt(a.E) if a.E else '—'}"
                         f"（{ev.get('from', 'unknown')}）；job：" + (", ".join(
                             f"{j} S {fmt(a.job_interval[j])}／有效 τ {fmt(a.job_timeout[j]) if j in a.job_timeout else '—'}"
                             for j in sorted(used)) or "—") + "）；inventory 標 from: user／confirmed 的值優先於 --config，"
                             "命令列參數優先於 inventory")
    if a.nodata_rules:
        a.assumptions.append(f"inventory 判定永遠不會響或一直在響的 {len(a.nodata_rules)} 條 rule 當成「沒有資料」，契約不適用")


def apply_defaults(a) -> None:
    """No S / E from flags, config or inventory: Prometheus / vmagent defaults (scrape_interval 1m,
    scrape_timeout 10s, evaluation_interval 1m), stated as assumptions instead of an error."""
    who = "vmagent／vmalert" if a.stack == "vmalert" else "Prometheus"
    if a.S is None:
        a.S = 60.0
        if a.timeout is None:
            a.timeout = 10.0
            a.assumptions.append(f"沒有 `scrape_interval`／`scrape_timeout`（沒給 --config、--inventory 或參數）：用 {who} 預設 "
                                 "`scrape_interval` 1m、`scrape_timeout` 10s；給 config 後結論可能改變")
        else:
            a.assumptions.append(f"沒有 `scrape_interval`：用 {who} 預設 1m；給 config 後結論可能改變")
    if a.E is None:
        a.E = 60.0
        a.assumptions.append(f"沒有 `evaluation_interval`：用 {who} 預設 1m"
                             + ("（vmalert `-evaluationInterval`）" if a.stack == "vmalert" else "")
                             + "；group 有寫 interval 的照 group")


def clamp_timeouts(a) -> None:
    """vmagent: any remaining τ > S (from --scrape-timeout / --job-timeout) becomes S, recorded as a finding."""
    if a.config_kind != "vmagent":
        return
    if a.timeout is not None and a.S is not None and a.timeout > a.S:
        a.tau_clamped.setdefault(None, a.timeout)
        a.timeout = a.S
    for j, t in list(a.job_timeout.items()):
        S = a.job_interval.get(j, a.S)
        if S is not None and t > S:
            a.tau_clamped.setdefault(j, t)
            a.job_timeout[j] = S


def load_sources(path: str) -> dict:
    """check_metric_sources.py JSON -> {(rule, metric): status} for selectors that return nothing."""
    import json
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {(r["rule"], r["metric"]): r["status"] for r in data.get("rows", [])
            if r.get("status") in MS.EMPTY_STATUSES}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rules", nargs="+", default=[], help="rule 檔；只要 --baseline 時可省略")
    ap.add_argument("--stack", choices=["prometheus", "vmalert"],
                    help="誰評估 rule；沒給時用 --inventory 的 stack.ruler，再沒有就當成 prometheus（寫進假設）")
    ap.add_argument("--inventory", help="tools/inventory.py 的 env/<name>.inventory.yaml：S、τ、E、O、job、exporter、"
                                        "實測值、keep_range、永遠不會響的 rule 從這裡讀；命令列參數優先")
    ap.add_argument("--config", action="append", default=[],
                    help="prometheus.yml（或 vmagent 的 -promscrape.config）：讀 global 與每個 job 的 scrape_interval／scrape_timeout、"
                         "evaluation_interval、rule_query_offset")
    ap.add_argument("--config-kind", choices=["prometheus", "vmagent"],
                    help="誰讀這份 scrape config；預設 --stack vmalert 時是 vmagent（τ > S 會被自動改成 S），否則 prometheus")
    ap.add_argument("--prom-version", help="Prometheus 版本（例如 2.48.1）；2.53 以前沒有 query_offset，3.x 以前沒有 anchored")
    ap.add_argument("--scrape-interval", help="S：預設的 scrape_interval（有 --config 時可省略）")
    ap.add_argument("--scrape-duration", help="d：尖峰 scrape 耗時（scrape_duration_seconds）；沒給就假設 d = τ（τ 也沒給時 d = S）")
    ap.add_argument("--job-interval", action="append", default=[], help="job=S，個別 job 的 scrape_interval")
    ap.add_argument("--job-duration", action="append", default=[], help="job=d，個別 job 的尖峰 scrape 耗時")
    ap.add_argument("--eval-interval", help="E：evaluation_interval（group 沒寫 interval 時；有 --config 時可省略）")
    ap.add_argument("--group-duration", help="T：尖峰 rule group 耗時")
    ap.add_argument("--scrape-timeout", help="τ：預設的 scrape_timeout")
    ap.add_argument("--job-timeout", action="append", default=[], help="job=τ")
    ap.add_argument("--scrape-p99", help="平常的 scrape 耗時（p99）")
    ap.add_argument("--job-p99", action="append", default=[], help="job=p99")
    ap.add_argument("--exporter-deadline", action="append", default=[], help="job=X，exporter 自己的查詢期限（優先於 exporter 預設值）")
    ap.add_argument("--job-exporter", action="append", default=[],
                    help=f"job=exporter（{'|'.join(EXPORTER_DEADLINE)}）；沒給時由 rule 的 metric 前綴推斷，用該 exporter 的預設期限")
    ap.add_argument("--job-failrun", action="append", default=[], help="job=N，觀察到的最長連續失敗次數（k 至少取這個）")
    ap.add_argument("--sources", help="check_metric_sources.py 的 metric-sources.json：選不到 series 的 selector 標「沒有資料」")
    ap.add_argument("--baseline", action="store_true", help="依量到的耗時輸出建議的 S、τ、X、E、R_min")
    ap.add_argument("--query-offset", help="O：全域 rule_query_offset（Prometheus；預設 0，或 --config 的值）")
    ap.add_argument("--k", type=int, default=1, help="容忍漏掉幾次 scrape（建議至少 1）")
    ap.add_argument("--lookback", default="5m")
    ap.add_argument("--latency", help="L：資料延遲（vmalert）；沒給時保守假設 L = 該 job 的 S")
    ap.add_argument("--eval-delay", default="30s", help="D：-rule.evalDelay（vmalert，預設 30s）")
    ap.add_argument("--query-step", default="5m", help="vmalert -datasource.queryStep（預設 5m）")
    ap.add_argument("--dedup-interval", help="VictoriaMetrics 的 -dedup.minScrapeInterval；大於 S 時實際間隔用它")
    ap.add_argument("--keep-range", action="append", default=[], help="這條 rule 的視窗是刻意選的，不要建議拉長（也可在 rule 加 annotation keep_range: \"true\"）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--write-fitted", help="把方案 A 改好的 rule 寫到這個檔（不動原檔；註解不會保留；第 5 節的改法不寫）")
    a = ap.parse_args(argv)
    a.assumptions = []
    inv = None
    if a.inventory:
        import inventory_use as IU
        inv = IU.load(a.inventory)
        if not a.rules and not a.baseline:
            a.rules = list((inv.get("sources") or {}).get("rules") or [])
        st = inv.get("stack") or {}
        if a.stack is None and IU.val(st.get("ruler")) in ("prometheus", "vmalert"):
            a.stack = IU.val(st["ruler"])
            if not IU.is_user(st["ruler"]) and st["ruler"].get("from") != "file":
                a.assumptions.append(f"ruler 是 {a.stack}：inventory 的推測（{'；'.join(st['ruler'].get('evidence') or [])}），未確認")
        ck = (inv.get("sources") or {}).get("config_kind") or {}
        if a.config_kind is None and IU.val(ck) in ("prometheus", "vmagent") and ck.get("from") != "default":
            a.config_kind = IU.val(ck)
        if a.prom_version is None and IU.val(st.get("version")) and (a.stack or "prometheus") == "prometheus":
            a.prom_version = str(IU.val(st["version"]))
    if a.stack is None:
        a.stack = "prometheus"
        a.assumptions.append("沒有 --stack，inventory 也不確定 ruler：當成 Prometheus；是 vmalert 時用 `--stack vmalert` 重跑")
    a.config_kind = a.config_kind or ("vmagent" if a.stack == "vmalert" else "prometheus")
    a.prom_version_text = a.prom_version or ""
    a.prom_version = parse_version(a.prom_version)
    a.tau_clamped = {}
    a.S, a.d, a.E, a.O = dur(a.scrape_interval), dur(a.scrape_duration), dur(a.eval_interval), dur(a.query_offset)
    a.T, a.lookback, a.latency, a.eval_delay = dur(a.group_duration), dur(a.lookback), dur(a.latency), dur(a.eval_delay)
    a.job_interval, a.job_duration = kv(a.job_interval), kv(a.job_duration)
    a.timeout, a.p99, a.dedup = dur(a.scrape_timeout), dur(a.scrape_p99), dur(a.dedup_interval)
    a.query_step = dur(a.query_step)
    a.job_timeout, a.job_p99, a.exporter_deadline = kv(a.job_timeout), kv(a.job_p99), kv(a.exporter_deadline)
    a.job_failrun = {k: int(v) for k, _, v in (x.partition("=") for x in a.job_failrun)}
    a.job_exporter = {k: v for k, _, v in (x.partition("=") for x in a.job_exporter)}
    a.job_exporter_flag = set(a.job_exporter)
    a.job_exporter_src = {}
    a.nodata_rules = {}
    for j, e in a.job_exporter.items():
        if e not in EXPORTER_DEADLINE:
            ap.error(f"--job-exporter {j}={e}：不認得的 exporter（可用：{', '.join(EXPORTER_DEADLINE)}）")
    a.cfg_jobs = None
    a.sources_empty = load_sources(a.sources) if a.sources else {}
    if inv is not None:
        apply_inventory(a, inv, user_only=True)
    if a.config:
        apply_config(a, a.config)
    if inv is not None:
        apply_inventory(a, inv, user_only=False)
    apply_defaults(a)
    clamp_timeouts(a)
    if a.O is None:
        a.O = 0.0
    if not query_offset_available(a) and a.O:
        a.assumptions.append(f"Prometheus {a.prom_version_text} 沒有 `rule_query_offset`，O 當成 0")
        a.O = 0.0
    if not a.rules and not a.baseline:
        ap.error("要給 --rules，或用 --baseline")
    if a.T is None:
        a.assumptions.append("rule group 耗時 T 沒給，沒檢查 T ≤ E")
    if a.stack == "vmalert":
        if a.latency is None:
            a.assumptions.append("資料延遲 L 沒給，用保守假設 L = 該 job 的 S（不是 0）；量 vmagent 到 VM 的延遲後用 `--latency` 重跑")
        if a.dedup is None:
            a.assumptions.append("`-dedup.minScrapeInterval` 沒給，當成沒有 dedup；有設且大於 S 時實際間隔是 dedup，視窗要更長")
    docs = load(a.rules)
    infer_exporters(a, docs)
    rows, fitted = analyze(a, docs)
    skipped: list = []
    fors = for_checks(a, docs, skipped)
    checks = env_checks(a)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "contracts-fit.md"), "w", encoding="utf-8") as f:
        f.write(report(a, rows, checks, assumptions_for(a, rows, fors), fors, skipped) if a.rules else "# 基準值\n")
        if a.baseline:
            f.write("\n" + baseline(a))
    with open(os.path.join(a.out, "contracts-fit.json"), "w", encoding="utf-8") as f:
        import json
        json.dump({"stack": a.stack, "S": a.S, "E": a.E, "assumptions": assumptions_for(a, rows, fors),
                   "env_checks": [{"text": c[0], "ok": c[1], "contract": c[3]} for c in checks],
                   "rows": [{k: r.get(k) for k in ("file", "group", "rule", "metric", "func", "R", "need", "ok", "nodata",
                                                    "job", "contract", "fit_R", "keep")} for r in rows],
                   "for_checks": [{"rule": x["rule"], "job": x["job"], "R": x["R"]} for x in fors]},
                  f, ensure_ascii=False, indent=1)
    if a.write_fitted:
        groups = [g for _, doc in fitted for g in doc.get("groups", []) or []]
        with open(a.write_fitted, "w", encoding="utf-8") as f:
            f.write("# 由 tools/fit_contracts.py 依目前環境調整視窗（方案 A）；原檔未改動；第 5 節（for 與 scrape 失敗）的改法沒有寫入。\n")
            yaml.safe_dump({"groups": groups}, f, allow_unicode=True, sort_keys=False, width=1000)
    bad = sum(1 for r in rows if not r["ok"] and not r.get("nodata"))
    nd = sum(1 for r in rows if r.get("nodata"))
    env_bad = sum(1 for c in checks if not c[1])
    print(f"建議：環境 {env_bad} 項、rule 視窗 {bad} 條、沒有資料 {nd} 條、`for` 與 scrape 失敗 {len(fors)} 條"
          f"（符合契約的 rule 視窗 {len(rows) - bad - nd} 條）→ {a.out}/contracts-fit.md 的「建議先做」")
    return 1 if bad or env_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
