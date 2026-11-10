#!/usr/bin/env python3
"""Check that every metric a rule reads has a known source: a scrape job, a recording rule, or a person's answer.

  python3 tools/check_metric_sources.py --rules my-rules/*.yml --config prometheus.yml \
      [--answers env/<name>.sources.yaml] [--live snapshot/<id>/metric_jobs.json] --out report
  python3 tools/check_metric_sources.py --inventory env/<name>.inventory.yaml --out report
      (jobs, target labels, dropped metrics and the answers come from the inventory; --rules / --config /
       --answers given on the command line still win)

Never guesses. For each (rule, metric selector) it reports one status:

  recording     produced by a recording rule in the given files
  builtin       up, scrape_*, ALERTS*: made by the ruler or the scrape itself (job matcher still checked)
  live          seen in the live snapshot (collect.py metric_jobs.json), in a job the selector matches
  confirmed     the answers file names the job(s) that produce it, and the selector matches them
  always_fires  the selector sits inside absent()/absent_over_time() and matches nothing (job, label, dropped,
                live): absent() returns 1 all the time, so the alert fires constantly
  job_mismatch  the selector's job matcher matches no scrape job in the config (rule returns nothing);
                also for {__name__=~"..."} selectors
  label_mismatch the selector requires a label (role="primary", cluster=...) that cannot exist: up/scrape_*
                only carry target labels and the config adds none, or the answers file says `labels.<l>: none`
  dropped       metric_relabel_configs in the matching job(s) drop this metric name
  live_missing  the live snapshot queried this metric and no matching job has it
                (metrics the snapshot did not query fall back to the answers file)
  needs_input   cannot be decided from files: ask the person which job produces it, or where a required
                non-job label comes from (static_configs labels / relabel target_label do not add it; the
                exporter may export it, which cannot be known offline; answer with `labels.<l>: exporter|none`)

The catalog (catalog/components, catalog/targets) only suggests which exporter usually produces a
metric; that is shown as a hint and never counts as a source. The report starts with advice
(建議 / 代價), not a verdict. Exit code (for CI only): 1 when any row is always_fires, job_mismatch,
label_mismatch, dropped or live_missing; 2 when only needs_input remains.
"""
from __future__ import annotations

import argparse
import difflib
import functools
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


def _after(expr: str, sel: dict, end: int) -> dict:
    """Fill pos/end/range/offset of a selector that ends at index `end` (exclusive)."""
    n = len(expr)
    k = end
    while k < n and expr[k].isspace():
        k += 1
    sel["range"], sel["range_end"], sel["offset"] = None, end, None
    if k < n and expr[k] == "[":
        close = expr.find("]", k)
        close = n - 1 if close < 0 else close
        sel["range"], sel["range_end"] = expr[k + 1:close].strip(), close + 1
        k = close + 1
    m = re.match(r"\s*offset\s+(-?[0-9smhdwy.]+)", expr[k:], re.I)
    if m:
        sel["offset"] = m.group(1)
    sel["end"] = end
    return sel


def selectors(expr: str) -> list[dict]:
    """Return [{"metric", "matchers", "name_matchers", "pos", "end", "range", "range_end", "offset"}] for each
    vector selector in expr. `range` is the text inside [...] right after the selector (None for an instant
    selector; "5m:1m" for a subquery directly on the selector)."""
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
            out.append(_after(expr, {"metric": names[0] if names else None, "matchers": [m for m in ms if m[0] != "__name__"],
                                     "name_matchers": [m for m in ms if m[0] == "__name__"], "pos": i}, j + 1))
            i = j + 1
            continue
        if c.isdigit() or (c == "." and i + 1 < n and expr[i + 1].isdigit()):
            m = re.match(r"[0-9.]+(e[+-]?\d+)?([smhdwy]|ms)*", expr[i:], re.I)
            i += len(m.group(0)) if m else 1
            continue
        if c.isalpha() or c in "_:":
            m = re.match(r"[A-Za-z_:][A-Za-z0-9_:]*", expr[i:])
            if m is None:  # a non-ASCII letter (prose in a card's signal text)
                i += 1
                continue
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
                out.append(_after(expr, {"metric": word, "matchers": parse_matchers(expr[k + 1:e]), "name_matchers": [],
                                         "pos": i}, e + 1))
                i = e + 1
            else:
                out.append(_after(expr, {"metric": word, "matchers": [], "name_matchers": [], "pos": i}, j))
                i = j
            continue
        i += 1
    return out


def enclosing_funcs(expr: str, pos: int) -> list[str]:
    """Names of the functions whose parentheses enclose position pos, innermost first ('' for a bare paren)."""
    out, depth = [], 0
    for i in range(pos - 1, -1, -1):
        c = expr[i]
        if c == ")":
            depth += 1
        elif c == "(":
            if depth == 0:
                m = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", expr[:i])
                name = m.group(1) if m else ""
                if name.lower() in GROUPING:  # sum by (x) (...): the "(" after by belongs to the aggregation
                    name = ""
                out.append(name)
            else:
                depth -= 1
    return out


def enclosing_func(expr: str, pos: int) -> str:
    """Innermost function around position pos ('' if none)."""
    fs = [f for f in enclosing_funcs(expr, pos)]
    return fs[0] if fs else ""


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
        labels = {"job", "instance"}
        for st in sc.get("static_configs") or []:
            labels |= set((st or {}).get("labels") or {})
        for r in (sc.get("relabel_configs") or []) + (sc.get("metric_relabel_configs") or []):
            if r.get("target_label") and r.get("action", "replace") in ("replace", "lowercase", "uppercase", "hashmod"):
                labels.add(str(r["target_label"]))
        # labels this config cannot enumerate: file/http SD carry their own, labelmap copies __meta_*, honor_labels keeps the exporter's
        open_labels = [w for w, hit in (("honor_labels", bool(sc.get("honor_labels"))),
                                        ("labelmap", any(r.get("action") == "labelmap" for r in sc.get("relabel_configs") or [])),
                                        ("file_sd_configs", "file_sd_configs" in sc), ("http_sd_configs", "http_sd_configs" in sc)) if hit]
        jobs.append({"scrape_interval": sc.get("scrape_interval"), "scrape_timeout": sc.get("scrape_timeout"),
                     "target_labels": sorted(l for l in labels if not l.startswith("__")), "open_labels": open_labels,
                     "job": sc.get("job_name"), "metrics_path": sc.get("metrics_path", "/metrics"),
                     "module": (sc.get("params") or {}).get("module"), "sd": sds or ["static_configs"],
                     "honor_labels": bool(sc.get("honor_labels")), "federate_match": (sc.get("params") or {}).get("match[]"),
                     "metric_relabel": sc.get("metric_relabel_configs") or [], "notes": notes,
                     "rewrites_job": "relabel_configs 會改寫 job label" in notes or bool(sc.get("honor_labels"))})
    return jobs


def load_global(path: str) -> dict:
    """global: block of prometheus.yml (scrape_interval, scrape_timeout, evaluation_interval, rule_query_offset)."""
    return dict((C._load(path) or {}).get("global") or {})


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

def producer_of(card: dict) -> str:
    """What produces a card's metrics: its `exporter` when several cards share one (mysql and mariadb both read
    mysqld_exporter), else the card id. Answers (`producers.<name>`) and hints use this name."""
    return card.get("exporter") or card["id"]


def producer_cards(root: str) -> dict[str, list[str]]:
    """producer name -> card ids, only for producers that more than one card (or a differently named card) uses."""
    out: dict[str, set] = {}
    for card in C.load_components(root):
        out.setdefault(producer_of(card), set()).add(card["id"])
    return {k: sorted(v) for k, v in out.items() if v != {k}}


def catalog_hints(root: str) -> dict[str, list[str]]:
    """metric name -> producers (exporter or card id) whose cards list it (hint only)."""
    out: dict[str, set] = {}
    for card in C.load_components(root):
        for s in card.get("signals", []):
            for m in s.get("metrics") or []:
                if isinstance(m, str) and re.fullmatch(r"[A-Za-z_:][A-Za-z0-9_:]*", m) and m not in BUILTIN_EXACT \
                        and m != "probe_success" and not m.startswith(BUILTIN_PREFIX):
                    out.setdefault(m, set()).add(producer_of(card))
    return {k: sorted(v) for k, v in out.items()}


def cards_note(producer: str, root: str) -> str:
    """'（目錄卡片：mysql 或 mariadb，要確認是哪一種）' when one exporter serves several cards."""
    cards = producer_cards(root).get(producer)
    return f"（目錄卡片：{' 或 '.join(cards)}，要確認是哪一種）" if cards else ""


# labels the exporter itself puts on its metrics: never a question about the scrape config
EXPORTER_LABELS = {"le", "quantile", "code", "method", "handler"}
EXPORTER_LABEL_PREFIX = ("grpc_",)
# labels that describe the target (who it is, where it runs): only the config or the person can say
IDENTITY_LABELS = {"cluster", "role", "env", "region", "service", "namespace", "pod", "team"}


def family(metric: str) -> str:
    return re.sub(r"_(bucket|sum|count|total|created|info)$", "", metric or "")


@functools.lru_cache(maxsize=4)
def catalog_label_hints(root: str) -> dict[str, set]:
    """metric family -> labels the catalog shows on it: selectors in the cards' signal text and in the repo's
    rule templates (rules/**/*.yml, which follow the upstream rules). A label seen there is the exporter's own."""
    out: dict[str, set] = {}

    def add(expr: str) -> None:
        for sel in selectors(str(expr)):
            if sel["metric"]:
                for (l, _, _) in sel["matchers"]:
                    if l not in ("job", "instance"):
                        out.setdefault(family(sel["metric"]), set()).add(l)
    for card in C.load_components(root):
        for sg in card.get("signals", []):
            add(sg.get("signal") or "")
    for path in sorted(glob.glob(os.path.join(root, "rules", "**", "*.yml"), recursive=True)):
        for g in (C._load(path) or {}).get("groups", []) or []:
            for r in g.get("rules", []) or []:
                add(r.get("expr", ""))
    return out


def exporter_label(label: str, metric: str | None, hints: dict[str, set], has_hint: bool) -> bool:
    """True when the label is the exporter's own (do not ask where it comes from): le / quantile / grpc_* / code /
    method / handler, a label the catalog shows on this metric family, or a non-identity label on a metric the
    catalog knows. Identity labels (cluster, role, env, ...) and labels on unknown metrics stay questions, even when
    a template uses them (rules/targets/mysql.yml writes role="primary" as a placeholder to add by relabel)."""
    if label in IDENTITY_LABELS:
        return False
    if label in EXPORTER_LABELS or label.startswith(EXPORTER_LABEL_PREFIX):
        return True
    if label in hints.get(family(metric or ""), set()):
        return True
    return has_hint and label not in IDENTITY_LABELS


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


ABSENT_FUNCS = {"absent", "absent_over_time"}
EMPTY_STATUSES = {"job_mismatch", "dropped", "live_missing", "label_mismatch"}  # selector returns nothing
NO_LABEL = {"none", "no", "沒有", "不存在", "false"}


def required_labels(matchers: list[tuple[str, str, str]]) -> list[str]:
    """Labels a selector needs to exist (job/instance are checked elsewhere / always present)."""
    out = []
    for label, op, val in matchers:
        if label in ("job", "instance", "__name__"):
            continue
        if op == "=" and val != "":
            out.append(label)
        elif op == "=~":
            try:
                if re.fullmatch(val, "") is None:
                    out.append(label)
            except re.error:
                pass
    return out


def check(rules: list[dict], jobs: list[dict], answers: dict, live: dict | None, root: str) -> list[dict]:
    recorded = {r["name"] for r in rules if r["kind"] == "record"}
    exact = catalog_hints(root)
    lhints = catalog_label_hints(root)
    ans_metrics = answers.get("metrics") or {}
    ans_producers = answers.get("producers") or {}
    ans_labels = {str(k): str(v) for k, v in (answers.get("labels") or {}).items()}
    # job label values that exist. relabel_configs / honor_labels can produce values not in job_name;
    # until the person lists them (answers.job_names) the set is open and a non-matching selector is a question.
    known = {j["job"] for j in jobs} | set(answers.get("job_names") or [])
    rewriters = [j["job"] for j in jobs if j["rewrites_job"]]
    open_set = bool(rewriters) and not answers.get("job_names")
    by_job = {j["job"]: j for j in jobs}
    rows = []
    for r in rules:
        seen = set()
        for sel in selectors(r["expr"]):
            metric = sel["metric"]
            jm = [m for m in sel["matchers"] if m[0] == "job"]
            key = (metric, tuple(sel["matchers"]), tuple(sel["name_matchers"]))
            if key in seen:
                continue
            seen.add(key)
            funcs = enclosing_funcs(r["expr"], sel["pos"])
            in_absent = bool(ABSENT_FUNCS & set(funcs))
            ok_job = lambda j: all(matches(op, v, j) for _, op, v in jm)  # noqa: E731
            matched = sorted(j for j in known if ok_job(j))
            name_txt = ", ".join(f'__name__{op}"{v}"' for _, op, v in sel["name_matchers"])
            row = {"rule": r["name"], "file": r["file"], "metric": metric or "{" + name_txt + "}", "ask": "",
                   "job_matcher": ", ".join(f'job{op}"{v}"' for _, op, v in jm), "jobs": matched, "hint": [], "detail": "",
                   "absent": in_absent, "labels_missing": [], "labels_exporter": []}
            no_job = bool(jm) and bool(jobs) and not matched and not (live is not None and metric in live)
            if metric in recorded:
                row.update(status="recording", detail="由給定檔案中的 recording rule 產生")
            elif metric is not None and metric.startswith("ALERTS"):
                row.update(status="builtin", detail="ruler 自己產生")
            elif no_job and open_set:
                row["hint"] = hint_for(metric, exact) if metric and metric not in BUILTIN_EXACT else []
                row.update(status="needs_input", ask="job_names",
                           detail=f"沒有 job 符合；{', '.join(rewriters)} 的 relabel／honor_labels 可能產生其他 job 名稱")
            elif no_job:
                close = difflib.get_close_matches(jm[0][2], sorted(known), n=1, cutoff=0.5) if jm[0][1] == "=" else []
                row.update(status="job_mismatch", suggest=close[0] if close else "",
                           detail=f"存在的 job：{', '.join(sorted(known))}")
            elif metric is None:
                # {__name__=~"mysql_slave_status_(slave|replica)_..."}: the literal prefix still hints the exporter
                pat = next((v for (_, op, v) in sel["name_matchers"] if op == "=~"), "")
                prefix = re.match(r"[A-Za-z0-9_:]*", pat).group(0)
                row["hint"] = hint_for(prefix, exact) if len(prefix) >= 4 else []
                card = row["hint"][0] if len(row["hint"]) == 1 else None
                if card and card in ans_producers:
                    ok = [j for j in ans_producers[card] if ok_job(j)]
                    row.update(status="confirmed" if ok else "job_mismatch", jobs=ok or matched,
                               detail=f"使用者確認 {card} 的 job" if ok else f"使用者說 {card} 來自 {', '.join(ans_producers[card])}，selector 對不上")
                elif card:
                    row.update(status="needs_input", ask="producer", detail="selector 用 __name__ 正規式；目錄提示來自 " + card)
                else:
                    row.update(status="needs_input", ask="metric", detail="selector 用 __name__ 正規式，無法離線判斷")
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
            # non-job labels the selector requires: the config must add them, or the exporter must export them
            if jobs and row["status"] in ("builtin", "confirmed", "live", "needs_input", "job_mismatch") \
                    and not (metric or "").startswith("ALERTS"):
                if row["status"] == "job_mismatch":  # still worth knowing: after the job is fixed, is the label there?
                    cfg = [by_job[row["suggest"]]] if row.get("suggest") in by_job else jobs
                else:
                    cfg = [by_job[j] for j in row["jobs"] if j in by_job] or ([] if jm else jobs)
                openish = [j["job"] for j in cfg if j["open_labels"]] or (rewriters if open_set else [])
                builtin = metric is not None and (metric in BUILTIN_EXACT or metric.startswith(BUILTIN_PREFIX))
                for label in required_labels(sel["matchers"]):
                    if any(label in j["target_labels"] for j in cfg):
                        continue
                    ans = ans_labels.get(label)
                    if ans is not None:
                        if ans.strip().lower() in NO_LABEL:
                            row.update(status="label_mismatch", detail=f"使用者說 `{label}` label 不存在")
                        continue
                    if builtin and not openish and row["status"] != "job_mismatch":
                        row.update(status="label_mismatch",
                                   detail=f"`{metric}` 只帶 target label；config 沒有加 `{label}`")
                        continue
                    if not builtin and exporter_label(label, metric, lhints, bool(row["hint"])):
                        row["labels_exporter"].append(label)  # the exporter's own label: not a question
                        continue
                    row["labels_missing"].append(label)
                if row["labels_exporter"]:
                    row["detail"] = (row["detail"] + "；" if row["detail"] else "") + \
                        f"{', '.join('`' + l + '`' for l in row['labels_exporter'])} 是 exporter 自己輸出的 label（目錄提示，未用 live 確認）"
                if row["labels_missing"] and row["status"] != "label_mismatch":
                    if row["status"] not in ("needs_input", "job_mismatch"):
                        row.update(status="needs_input", ask="label")
                    row["detail"] = (row["detail"] + "；" if row["detail"] else "") + \
                        f"要 {', '.join('`' + l + '`' for l in row['labels_missing'])} label，config 沒有加"
            if in_absent and row["status"] in EMPTY_STATUSES:
                row.update(status="always_fires", cause=row["status"],
                           detail=f"absent() 裡的 selector 選不到任何 series（{LABEL[row['status']]}：{row['detail']}），"
                                  "absent 一直回傳 1")
            rows.append(row)
    return rows


ORDER = ["always_fires", "job_mismatch", "label_mismatch", "dropped", "live_missing", "needs_input", "confirmed", "live",
         "recording", "builtin"]
LABEL = {"always_fires": "一直在響", "job_mismatch": "job 對不上", "label_mismatch": "label 不存在",
         "dropped": "被 relabel 丟掉", "live_missing": "live 查不到", "needs_input": "要問人", "confirmed": "使用者確認",
         "live": "live 看得到", "recording": "recording rule", "builtin": "內建"}
ACTION = {"always_fires", "job_mismatch", "label_mismatch", "dropped", "live_missing"}


def _rules(rows: list[dict]) -> str:
    names = list(dict.fromkeys(r["rule"] for r in rows))
    return "、".join(f"`{n}`" for n in names[:8]) + (f" 等 {len(names)} 條" if len(names) > 8 else "")


def advice(rows: list[dict]) -> list[str]:
    """Advice-first summary: what to do, why, and what it costs."""
    out = []
    af = [r for r in rows if r["status"] == "always_fires"]
    if af:
        out.append(f"- **{_rules(af)} 現在一直在響**（或已經被 silence）：`absent()` 裡的 selector 選不到任何 series，"
                   "所以 absent 永遠回傳 1。**建議**：先修正 selector（job、label 或被丟掉的 metric），它就只在 target 真的消失時響。"
                   "**代價**：無；如果已經 silence，修正後記得解除。")
    jm = [r for r in rows if r["status"] == "job_mismatch"]
    if jm:
        conds = sorted({r["job_matcher"] for r in jm if r["job_matcher"]})
        sugg = sorted({f'`{r["job_matcher"]}` → `job="{r["suggest"]}"`' for r in jm if r.get("suggest")})
        out.append(f"- **{_rules(jm)} 不會響**：job 條件 {', '.join('`' + c + '`' for c in conds)} 在 config 裡找不到任何 job，"
                   "selector 回傳空。**建議**：把 job 條件改成實際的 job 名稱"
                   + (f"（最接近的：{'；'.join(sugg)}）" if sugg else "")
                   + "，或在 scrape config 用 relabel 把 job 改名。**代價**：改 rule 要一起改用到同一個 job 的 dashboard；"
                   "改 job 名稱會讓既有 series 換名、歷史查詢斷開。")
    lm = [r for r in rows if r["status"] == "label_mismatch"]
    if lm:
        out.append(f"- **{_rules(lm)} 不會響**：selector 要的 label 不存在（{'；'.join(sorted({r['detail'] for r in lm}))}）。"
                   "**建議**：在 `static_configs` 的 `labels` 或 `relabel_configs` 加上這個 label，或拿掉這個條件。"
                   "**代價**：靜態 label（例如 role）在 failover 後要跟著改，否則規則看錯節點。")
    dr = [r for r in rows if r["status"] == "dropped"]
    if dr:
        ms = sorted({r["metric"] for r in dr})
        out.append(f"- **{_rules(dr)} 不會響**：{', '.join('`' + m + '`' for m in ms)} 被 `metric_relabel_configs` 丟掉。"
                   "**建議**：調整 drop 規則讓它保留，或改用沒被丟掉的 metric。**代價**：保留會增加 series 數。")
    lv = [r for r in rows if r["status"] == "live_missing"]
    if lv:
        out.append(f"- **{_rules(lv)} 目前查不到資料**（live 快照）。**建議**：確認 exporter 有開對應 collector、job 名稱正確。")
    ni = [r for r in rows if r["status"] == "needs_input"]
    if ni:
        out.append(f"- {_rules(ni)} 的來源還沒確認：回答最後一節的問題後重跑。")
    return out


def answer_versions(answers: dict) -> dict:
    """Versions the answers give, passed through to the output (answers.version = the ruler's,
    answers.target_versions.<job> = the database behind that job). The metric checks do not use them."""
    out = {}
    if answers.get("version") is not None:
        out["ruler"] = str(answers["version"])
    tv = answers.get("target_versions")
    if isinstance(tv, dict) and tv:
        out["targets"] = {str(k): str(v) for k, v in tv.items() if v is not None}
    return out


def report(rows: list[dict], jobs: list[dict], versions: dict | None = None) -> str:
    out = ["# Metric 來源檢查\n", "每條 rule 讀到的 metric，都要找得到來源：scrape job、recording rule，或使用者確認。"
           "目錄裡的卡片只提示「通常由哪個 exporter 產生」，不算來源。\n"]
    adv = advice(rows)
    out.append("## 建議先做\n")
    out += adv or ["- 每個 metric 都找得到來源，沒有要先處理的項目。"]
    out.append("")
    cnt = {s: sum(1 for r in rows if r["status"] == s) for s in ORDER}
    out.append("| 狀態 | 數量 |\n|---|---|")
    out += [f"| {LABEL[s]}（`{s}`） | {cnt[s]} |" for s in ORDER if cnt[s]]
    out.append("\n## Config 裡的 scrape job\n")
    tv = (versions or {}).get("targets") or {}
    if versions:
        out.append("版本（answers）：" + "、".join(([f"ruler {versions['ruler']}"] if versions.get("ruler") else [])
                                             + [f"job `{k}` {v}" for k, v in sorted(tv.items())]) + "\n")
    out.append("| job | metrics_path | 服務發現 | target label | 備註 |\n|---|---|---|---|---|")
    for j in jobs:
        extra = j["notes"] + ([f"module {j['module']}"] if j["module"] else []) + \
            ([f"label 由 {', '.join(j['open_labels'])} 決定，無法離線列出"] if j.get("open_labels") else [])
        out.append(f"| `{j['job']}` | `{j['metrics_path']}` | {', '.join(j['sd'])} | {', '.join(j.get('target_labels', []))} "
                   f"| {'；'.join(extra)} |")
    out.append("\n## 每條 rule 的 metric\n")
    out.append("| 狀態 | rule | metric | job 條件 | 符合的 job | 說明 | 目錄提示 |\n|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda x: (ORDER.index(x["status"]), x["rule"] or "", x["metric"])):
        cell = lambda x: str(x).replace("|", "\\|")  # noqa: E731 - a | inside a table cell splits the column
        out.append(f"| {LABEL[r['status']]} | `{r['rule']}` | `{cell(r['metric'])}` | {('`' + cell(r['job_matcher']) + '`') if r['job_matcher'] else '—'} "
                   f"| {', '.join(r['jobs']) or '—'} | {cell(r['detail'])} | {', '.join(r['hint']) or '—'} |")
    ask = [r for r in rows if r["status"] == "needs_input"]
    if ask or any(r["labels_missing"] for r in rows):
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
            out.append(f"- 目錄提示這 {len(ms)} 個 metric 通常來自 `{card}`{cards_note(card, C.ROOT)}：{', '.join('`' + m + '`' for m in ms[:6])}"
                       f"{' 等' if len(ms) > 6 else ''}。實際是哪個 scrape job？（寫在 `producers.{card}`）")
        for m in sorted(groups.get("", ())):
            out.append(f"- `{m}` 是哪個 scrape job 產生的？目錄裡沒有唯一的提示，可能是應用程式自己的 metric（寫在 `metrics`）")
        pats: dict[str, list] = {}
        for r in ask:
            if r["ask"] == "metric":
                pats.setdefault(r["metric"], []).append(r)
        for pat, rs in pats.items():
            out.append(f"- {_rules(rs)} 用 `{pat}` 選 metric：實際會選到哪些 metric、來自哪個 job？（逐一寫在 `metrics`）")
        labels: dict[str, list] = {}
        for r in rows:
            for l in r["labels_missing"]:
                labels.setdefault(l, []).append(r)
        for l, rs in labels.items():
            out.append(f"- {_rules(rs)} 要 `{l}` label，config 沒有加（`static_configs` labels、`relabel_configs` 都沒有）。"
                       f"這個 label 從哪裡來？exporter 自己輸出就寫 `labels.{l}: exporter`；根本沒有就寫 `labels.{l}: none`。")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rules", nargs="+", help="rule 檔（Prometheus 或 vmalert 格式）；有 --inventory 時可省略")
    ap.add_argument("--config", action="append", default=[], help="prometheus.yml（可重複）；給了就不用 inventory 的 job")
    ap.add_argument("--inventory", help="tools/inventory.py 的 env/<name>.inventory.yaml：job、label、answers 從這裡讀")
    ap.add_argument("--answers", help="使用者確認的來源（env/<name>.sources.yaml）；優先於 inventory 記的 answers")
    ap.add_argument("--live", help="collect.py 產生的 metric_jobs.json")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    inv = None
    if a.inventory:
        import inventory_use as IU
        inv = IU.load(a.inventory)
    if not a.rules:
        a.rules = list(((inv or {}).get("sources") or {}).get("rules") or [])
        if not a.rules:
            ap.error("要給 --rules（或 --inventory，裡面記了 rule 檔）")
    rules = load_rules(a.rules)
    jobs = [j for c in a.config for j in load_config(c)]
    answers = C._load(a.answers) or {} if a.answers else {}
    if inv is not None:
        if not a.config:
            jobs = IU.ms_jobs(inv)
        answers = IU.answers(inv, answers)
    live = None
    if a.live:
        with open(a.live, encoding="utf-8") as f:
            live = {m: sorted(v) for m, v in json.load(f).items()}
    rows = check(rules, jobs, answers, live, C.ROOT)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "metric-sources.md"), "w", encoding="utf-8") as f:
        f.write(report(rows, jobs, answer_versions(answers)))
    with open(os.path.join(a.out, "metric-sources.json"), "w", encoding="utf-8") as f:
        json.dump({"jobs": [{k: v for k, v in j.items() if k != "metric_relabel"} for j in jobs], "rows": rows,
                   "versions": answer_versions(answers)},
                  f, ensure_ascii=False, indent=2)
    cnt = {s: sum(1 for r in rows if r["status"] == s) for s in ORDER}
    print("  ".join(f"{LABEL[s]} {cnt[s]}" for s in ORDER if cnt[s]) or "沒有 rule")
    act = sum(cnt[s] for s in ACTION)
    if act:
        print(f"建議先處理 {act} 個 selector，見 {a.out}/metric-sources.md 的「建議先做」")
    if not jobs:
        print("沒有給 --config：job 條件無法檢查，所有 metric 都要問人或用 --live")
    if act:
        return 1
    return 2 if cnt["needs_input"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
