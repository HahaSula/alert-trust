#!/usr/bin/env python3
"""Check that every metric a rule reads has a known source: a scrape job, a recording rule, or a person's answer.

  python3 tools/check_metric_sources.py --rules my-rules/*.yml --config prometheus.yml \
      [--answers env/<name>.sources.yaml] [--live snapshot/<id>/metric_jobs.json] --out report

Never guesses. For each (rule, metric selector) it reports one status:

  recording     produced by a recording rule in the given files
  builtin       up, scrape_*, ALERTS*: made by the ruler or the scrape itself (job matcher still checked)
  live          seen in the live snapshot (collect.py metric_jobs.json), in a job the selector matches
  confirmed     the answers file names the job(s) that produce it, and the selector matches them
  job_mismatch  the selector's job matcher matches no scrape job in the config (rule returns nothing)
  dropped       metric_relabel_configs in the matching job(s) drop this metric name
  live_missing  the live snapshot queried this metric and no matching job has it
                (metrics the snapshot did not query fall back to the answers file)
  needs_input   cannot be decided from files: ask the person which job produces it

The catalog (catalog/components, catalog/targets) only suggests which exporter usually produces a
metric; that is shown as a hint and never counts as a source. Exit code 1 when any row is
job_mismatch, dropped or live_missing; 2 when only needs_input remains.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog as C  # noqa: E402
from collect import PROMQL_KEYWORDS  # noqa: E402

BUILTIN_EXACT = {"up", "ALERTS", "ALERTS_FOR_STATE"}
BUILTIN_PREFIX = ("scrape_",)
GROUPING = {"by", "without", "on", "ignoring", "group_left", "group_right"}
AGGREGATIONS = {"sum", "min", "max", "avg", "group", "stddev", "stdvar", "count", "count_values", "bottomk",
                "topk", "quantile", "limitk", "limit_ratio"}
_MATCHER_RE = re.compile(r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`[^`]*`)\s*,?')


# ---------- PromQL: metric selectors with their label matchers ----------

def _unquote(s: str) -> str:
    if s[0] == "`":
        return s[1:-1]
    return bytes(s[1:-1], "utf-8").decode("unicode_escape")


def parse_matchers(body: str) -> list[tuple[str, str, str]]:
    out, pos = [], 0
    while pos < len(body):
        m = _MATCHER_RE.match(body, pos)
        if not m:
            break
        out.append((m.group(1), m.group(2), _unquote(m.group(3))))
        pos = m.end()
    return out


def selectors(expr: str) -> list[dict]:
    """Return [{"metric": name, "matchers": [(label, op, value)]}] for each vector selector in expr."""
    out, i, n = [], 0, len(expr)
    while i < n:
        c = expr[i]
        if c in "\"'`":  # string literal outside a selector (label_replace args etc.)
            j = i + 1
            while j < n and expr[j] != c:
                j += 2 if expr[j] == "\\" and c != "`" else 1
            i = j + 1
            continue
        if c == "[":  # range / subquery duration
            i = expr.find("]", i) + 1 or n
            continue
        if c == "{":  # bare selector {__name__="x", ...}
            j = _close_brace(expr, i)
            ms = parse_matchers(expr[i + 1:j])
            names = [v for (l, op, v) in ms if l == "__name__" and op == "="]
            out.append({"metric": names[0] if names else None, "matchers": [m for m in ms if m[0] != "__name__"],
                        "name_matchers": [m for m in ms if m[0] == "__name__"]})
            i = j + 1
            continue
        if c.isdigit() or (c == "." and i + 1 < n and expr[i + 1].isdigit()):
            m = re.match(r"[0-9.]+(e[+-]?\d+)?([smhdwy]|ms)*", expr[i:], re.I)
            i += len(m.group(0)) if m else 1
            continue
        if c.isalpha() or c in "_:":
            m = re.match(r"[A-Za-z_:][A-Za-z0-9_:]*", expr[i:])
            word = m.group(0)
            j = i + len(word)
            k = j
            while k < n and expr[k].isspace():
                k += 1
            nxt = expr[k] if k < n else ""
            low = word.lower()
            if low in GROUPING and nxt == "(":
                i = expr.find(")", k) + 1 or n
                continue
            if low == "offset":
                m2 = re.match(r"\s*-?[0-9smhdwy]+", expr[j:])
                i = j + (len(m2.group(0)) if m2 else 0)
                continue
            if nxt == "(" or low in PROMQL_KEYWORDS or low in AGGREGATIONS:
                i = j
                continue
            if nxt == "{":
                e = _close_brace(expr, k)
                out.append({"metric": word, "matchers": parse_matchers(expr[k + 1:e]), "name_matchers": []})
                i = e + 1
            else:
                out.append({"metric": word, "matchers": [], "name_matchers": []})
                i = j
            continue
        i += 1
    return out


def _close_brace(expr: str, start: int) -> int:
    i, q = start + 1, None
    while i < len(expr):
        ch = expr[i]
        if q:
            if ch == "\\" and q != "`":
                i += 2
                continue
            if ch == q:
                q = None
        elif ch in "\"'`":
            q = ch
        elif ch == "}":
            return i
        i += 1
    return len(expr) - 1


def matches(op: str, pattern: str, value: str) -> bool:
    if op == "=":
        return value == pattern
    if op == "!=":
        return value != pattern
    try:
        hit = re.fullmatch(pattern, value) is not None
    except re.error:
        return True  # cannot evaluate: do not claim a mismatch
    return hit if op == "=~" else not hit


# ---------- Prometheus config ----------

def load_config(path: str) -> list[dict]:
    """Scrape jobs from prometheus.yml (+ scrape_config_files)."""
    doc = C._load(path) or {}
    base = os.path.dirname(os.path.abspath(path))
    scs = list(doc.get("scrape_configs") or [])
    for pat in doc.get("scrape_config_files") or []:
        for f in sorted(glob.glob(os.path.join(base, pat))):
            scs += (C._load(f) or {}).get("scrape_configs") or []
    jobs = []
    for sc in scs:
        notes = []
        if any(r.get("target_label") == "job" for r in sc.get("relabel_configs") or []):
            notes.append("relabel_configs 會改寫 job label")
        if (sc.get("metrics_path") or "/metrics") == "/federate":
            notes.append("federate：series 保留來源的 job label" if sc.get("honor_labels") else "federate")
        sds = [k for k in sc if k.endswith("_sd_configs")]
        jobs.append({"job": sc.get("job_name"), "metrics_path": sc.get("metrics_path", "/metrics"),
                     "module": (sc.get("params") or {}).get("module"), "sd": sds or ["static_configs"],
                     "honor_labels": bool(sc.get("honor_labels")), "federate_match": (sc.get("params") or {}).get("match[]"),
                     "metric_relabel": sc.get("metric_relabel_configs") or [], "notes": notes,
                     "rewrites_job": "relabel_configs 會改寫 job label" in notes or bool(sc.get("honor_labels"))})
    return jobs


def dropped_by(job: dict, metric: str) -> bool:
    for r in job["metric_relabel"]:
        if r.get("source_labels") != ["__name__"]:
            continue
        rx = r.get("regex", "(.*)")
        try:
            hit = re.fullmatch(str(rx), metric) is not None
        except re.error:
            continue
        act = r.get("action", "replace")
        if (act == "drop" and hit) or (act == "keep" and not hit):
            return True
    return False


# ---------- catalog hints ----------

def catalog_hints(root: str) -> dict[str, list[str]]:
    """metric name -> card ids that list it (hint only)."""
    out: dict[str, set] = {}
    for card in C.load_components(root):
        for s in card.get("signals", []):
            for m in s.get("metrics") or []:
                if isinstance(m, str) and re.fullmatch(r"[A-Za-z_:][A-Za-z0-9_:]*", m) and m not in BUILTIN_EXACT \
                        and m != "probe_success" and not m.startswith(BUILTIN_PREFIX):
                    out.setdefault(m, set()).add(card["id"])
    return {k: sorted(v) for k, v in out.items()}


def hint_for(metric: str, exact: dict[str, list[str]]) -> list[str]:
    if metric in exact:
        return exact[metric]
    base = re.sub(r"_(bucket|sum|count|total|created)$", "", metric)
    if base in exact:
        return exact[base]
    pre = metric.split("_", 1)[0] + "_"
    cards = sorted({c for k, v in exact.items() if k.startswith(pre) for c in v})
    return cards[:3]


# ---------- main check ----------

def load_rules(files: list[str]) -> list[dict]:
    out = []
    for path in files:
        for g in (C._load(path) or {}).get("groups", []) or []:
            if g.get("type") not in (None, "", "prometheus"):
                continue
            for r in g.get("rules", []) or []:
                out.append({"file": path, "group": g.get("name"), "name": r.get("alert") or r.get("record"),
                            "kind": "alert" if "alert" in r else "record", "expr": str(r.get("expr", ""))})
    return out


def check(rules: list[dict], jobs: list[dict], answers: dict, live: dict | None, root: str) -> list[dict]:
    recorded = {r["name"] for r in rules if r["kind"] == "record"}
    exact = catalog_hints(root)
    ans_metrics = answers.get("metrics") or {}
    ans_producers = answers.get("producers") or {}
    # job label values that exist. relabel_configs / honor_labels can produce values not in job_name;
    # until the person lists them (answers.job_names) the set is open and a non-matching selector is a question.
    known = {j["job"] for j in jobs} | set(answers.get("job_names") or [])
    rewriters = [j["job"] for j in jobs if j["rewrites_job"]]
    open_set = bool(rewriters) and not answers.get("job_names")
    rows = []
    for r in rules:
        seen = set()
        for sel in selectors(r["expr"]):
            metric = sel["metric"]
            jm = [m for m in sel["matchers"] if m[0] == "job"]
            key = (metric, tuple(jm))
            if key in seen:
                continue
            seen.add(key)
            ok_job = lambda j: all(matches(op, v, j) for _, op, v in jm)  # noqa: E731
            matched = sorted(j for j in known if ok_job(j))
            row = {"rule": r["name"], "file": r["file"], "metric": metric or "{__name__=~…}", "ask": "",
                   "job_matcher": ", ".join(f'job{op}"{v}"' for _, op, v in jm), "jobs": matched, "hint": [], "detail": ""}
            no_job = bool(jm) and bool(jobs) and not matched and not (live is not None and metric in live)
            if metric is None:
                row.update(status="needs_input", ask="metric", detail="selector 用 __name__ 正規式，無法離線判斷")
            elif metric in recorded:
                row.update(status="recording", detail="由給定檔案中的 recording rule 產生")
            elif metric.startswith("ALERTS"):
                row.update(status="builtin", detail="ruler 自己產生")
            elif no_job and open_set:
                row["hint"] = hint_for(metric, exact) if metric not in BUILTIN_EXACT else []
                row.update(status="needs_input", ask="job_names",
                           detail=f"沒有 job 符合；{', '.join(rewriters)} 的 relabel／honor_labels 可能產生其他 job 名稱")
            elif no_job:
                row.update(status="job_mismatch", detail=f"存在的 job：{', '.join(sorted(known))}")
            elif metric in BUILTIN_EXACT or metric.startswith(BUILTIN_PREFIX):
                row.update(status="builtin", detail="每個 scrape job 自動產生")
            else:
                row["hint"] = hint_for(metric, exact)
                cfg = [j for j in jobs if j["job"] in matched]
                drop = [j["job"] for j in cfg if dropped_by(j, metric)]
                a = ans_metrics.get(metric)
                a_jobs = a.get("jobs") if isinstance(a, dict) else None
                if a_jobs is None:
                    a_jobs = next((ans_producers[c] for c in row["hint"] if c in ans_producers), None)
                if cfg and len(drop) == len(cfg) and not (open_set and len(matched) > len(cfg)):
                    row.update(status="dropped", detail=f"metric_relabel_configs 丟掉：{', '.join(drop)}")
                elif live is not None and metric in live:  # collect.py queried it (a key with no jobs = no series)
                    lj = [j for j in live[metric] if ok_job(j)]
                    if lj:
                        row.update(status="live", jobs=lj, detail="live 快照看得到")
                    else:
                        where = ", ".join(live[metric])
                        row.update(status="live_missing",
                                   detail=f"live 快照裡只出現在：{where}" if where else "live 快照查不到這個 metric")
                elif a_jobs is not None:
                    ok = [j for j in a_jobs if ok_job(j)]
                    if ok:
                        row.update(status="confirmed", jobs=ok, detail="使用者確認")
                    else:
                        row.update(status="job_mismatch", detail=f"使用者說來源是 {', '.join(a_jobs)}，selector 對不上")
                else:
                    why = "selector 沒有 job 條件，任何 job 都可能" if not jm else "有符合的 job，但不知道它是否輸出這個 metric"
                    row.update(status="needs_input", ask="producer", detail=why)
            rows.append(row)
    return rows


ORDER = ["job_mismatch", "dropped", "live_missing", "needs_input", "confirmed", "live", "recording", "builtin"]
LABEL = {"job_mismatch": "job 對不上", "dropped": "被 relabel 丟掉", "live_missing": "live 查不到",
         "needs_input": "要問人", "confirmed": "使用者確認", "live": "live 看得到", "recording": "recording rule",
         "builtin": "內建"}


def report(rows: list[dict], jobs: list[dict]) -> str:
    out = ["# Metric 來源檢查\n", "每條 rule 讀到的 metric，都要找得到來源：scrape job、recording rule，或使用者確認。"
           "目錄裡的卡片只提示「通常由哪個 exporter 產生」，不算來源。\n"]
    cnt = {s: sum(1 for r in rows if r["status"] == s) for s in ORDER}
    out.append("| 狀態 | 數量 |\n|---|---|")
    out += [f"| {LABEL[s]}（`{s}`） | {cnt[s]} |" for s in ORDER if cnt[s]]
    out.append("\n## Config 裡的 scrape job\n")
    out.append("| job | metrics_path | 服務發現 | 備註 |\n|---|---|---|---|")
    for j in jobs:
        extra = j["notes"] + ([f"module {j['module']}"] if j["module"] else [])
        out.append(f"| `{j['job']}` | `{j['metrics_path']}` | {', '.join(j['sd'])} | {'；'.join(extra)} |")
    out.append("\n## 每條 rule 的 metric\n")
    out.append("| 狀態 | rule | metric | job 條件 | 符合的 job | 說明 | 目錄提示 |\n|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda x: (ORDER.index(x["status"]), x["rule"] or "", x["metric"])):
        out.append(f"| {LABEL[r['status']]} | `{r['rule']}` | `{r['metric']}` | {('`' + r['job_matcher'] + '`') if r['job_matcher'] else '—'} "
                   f"| {', '.join(r['jobs']) or '—'} | {r['detail']} | {', '.join(r['hint']) or '—'} |")
    ask = [r for r in rows if r["status"] == "needs_input"]
    if ask:
        out.append("\n## 要問使用者的問題\n")
        out.append("回答寫進 answers 檔（格式見 `env/sources.example.yaml`）再跑一次；不知道就標「未知」，報告會列在沒有確認的部分。\n")
        jn = sorted({r["job_matcher"] for r in ask if r["ask"] == "job_names"})
        if jn:
            out.append(f"- relabel 或 honor_labels 之後，實際有哪些 job 名稱？（寫在 `job_names`）這些條件在 job_name 裡找不到：{', '.join('`' + x + '`' for x in jn)}")
        groups: dict[str, set] = {}
        for r in ask:
            if r["ask"] == "producer":
                groups.setdefault(r["hint"][0] if len(r["hint"]) == 1 else "", set()).add(r["metric"])
        for card in sorted(k for k in groups if k):
            ms = sorted(groups[card])
            out.append(f"- 目錄提示這 {len(ms)} 個 metric 通常來自 `{card}`：{', '.join('`' + m + '`' for m in ms[:6])}"
                       f"{' 等' if len(ms) > 6 else ''}。實際是哪個 scrape job？（寫在 `producers.{card}`）")
        for m in sorted(groups.get("", ())):
            out.append(f"- `{m}` 是哪個 scrape job 產生的？目錄裡沒有唯一的提示，可能是應用程式自己的 metric（寫在 `metrics`）")
        for r in ask:
            if r["ask"] == "metric":
                out.append(f"- `{r['rule']}` 用 `__name__` 正規式選 metric，實際會選到哪些 metric、來自哪個 job？")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rules", nargs="+", required=True, help="rule 檔（Prometheus 或 vmalert 格式）")
    ap.add_argument("--config", action="append", default=[], help="prometheus.yml（可重複）")
    ap.add_argument("--answers", help="使用者確認的來源（env/<name>.sources.yaml）")
    ap.add_argument("--live", help="collect.py 產生的 metric_jobs.json")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rules = load_rules(a.rules)
    jobs = [j for c in a.config for j in load_config(c)]
    answers = C._load(a.answers) or {} if a.answers else {}
    live = None
    if a.live:
        with open(a.live, encoding="utf-8") as f:
            live = {m: sorted(v) for m, v in json.load(f).items()}
    rows = check(rules, jobs, answers, live, C.ROOT)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "metric-sources.md"), "w", encoding="utf-8") as f:
        f.write(report(rows, jobs))
    with open(os.path.join(a.out, "metric-sources.json"), "w", encoding="utf-8") as f:
        json.dump({"jobs": [{k: v for k, v in j.items() if k != "metric_relabel"} for j in jobs], "rows": rows},
                  f, ensure_ascii=False, indent=2)
    cnt = {s: sum(1 for r in rows if r["status"] == s) for s in ORDER}
    print("  ".join(f"{LABEL[s]} {cnt[s]}" for s in ORDER if cnt[s]) or "沒有 rule")
    if not jobs:
        print("沒有給 --config：job 條件無法檢查，所有 metric 都要問人或用 --live")
    if cnt["job_mismatch"] or cnt["dropped"] or cnt["live_missing"]:
        return 1
    return 2 if cnt["needs_input"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
