#!/usr/bin/env python3
"""Build an environment model (env/<name>.inventory.yaml) from the user's files, before any advice.

  python3 tools/inventory.py --rules user/*.yml [--config prometheus.yml] [--config-kind prometheus|vmagent] \
      [--alertmanager alertmanager.yml] [--answers env/<name>.sources.yaml] \
      [--merge env/<name>.inventory.yaml] --out env/<name>.inventory.yaml [--md report/inventory.md]

Every value carries `from`: file (read from a file), default (official default), hint (catalog guess),
user (the answers file or an edit in the inventory), unknown. `--merge` keeps values the user edited
(marked `confirmed: true`, `from: user`, or changed while the source files stayed the same) and refreshes
the rest. Sections:

  stack                 ruler kind (rule syntax + config kind), version, rule_files, alertmanagers, observer
  jobs                  per scrape job: interval / timeout (+ effective), targets, target labels, dropped
                        metrics, exporter hint (catalog), exporter deadline default (docs/tools/baseline.md),
                        measured d_p99 / d_max / up0 (answers)
  unscraped_components  Alertmanager targets and the ruler itself when no scrape job covers them
  rules                 jobs used / matched, labels required, ranges, absence detection, for, keep_range,
                        labels_out, Alertmanager route
  alertmanager          timings per route (with inheritance), receivers (empty ones), inhibit rules,
                        rules routed to an empty receiver, rules an inhibit rule always suppresses
  findings_from_files   facts only, no advice
  questions             ranked by how many findings / rules the answer changes; at most 3 ask_now

Answers file (env/<name>.sources.yaml) keys read here: job_names, producers, metrics, labels (as in
check_metric_sources.py), plus version, ruler, external_observer, measured.<job>.{d_p99,d_max,up0_count},
keep_range.<rule>. The inventory is facts and questions; advice comes from the later tools.
"""
from __future__ import annotations

import argparse
import difflib
import fnmatch
import glob
import hashlib
import os
import re
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog as C  # noqa: E402
import check_metric_sources as MS  # noqa: E402
import fit_contracts as FC  # noqa: E402

# ---------- small helpers ----------

IDENTITY_LABELS = ("cluster", "role", "env", "region", "service", "namespace", "pod", "team")
VMALERT_GROUP_FIELDS = ("type", "concurrency", "eval_offset", "eval_delay", "eval_alignment", "headers",
                        "notifier_headers", "params")
VMALERT_RULE_FIELDS = ("debug", "update_entries_limit")
PROMETHEUS_GROUP_FIELDS = ("query_offset",)
# Exporter's own deadline with default flags (docs/tools/baseline.md#defaults). Value: seconds, or a function of τ.
EXPORTER_DEADLINE = {
    "redis_exporter": (lambda t: 15.0, "--connection-timeout 預設 15s，不讀 scrape timeout header"),
    "postgres_exporter": (lambda t: 60.0, "--collection-timeout 預設 1m，不讀 scrape timeout header"),
    "mysqld_exporter": (lambda t: t - 0.25, "τ − --timeout-offset（預設 0.25s）"),
    "mongodb_exporter": (lambda t: t - 1.0, "τ − --web.timeout-offset（預設 1s）"),
    "blackbox_exporter": (lambda t: t - 0.5, "min(module timeout, τ − --timeout-offset（預設 0.5s））"),
}
AM_DEFAULTS = {"group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h"}
# Default ports (hint only: a target on this port is probably this exporter / component)
DEFAULT_PORTS = {"9100": "node_exporter", "9104": "mysqld_exporter", "9187": "postgres_exporter", "9121": "redis_exporter",
                 "9216": "mongodb_exporter", "9115": "blackbox_exporter", "9090": "prometheus", "9093": "alertmanager",
                 "8429": "vmagent", "8428": "victoriametrics", "8880": "vmalert", "2379": "etcd"}
RULER_SELF = {"prometheus": (r".*prometheus.*", ":9090"), "vmalert": (r".*vmalert.*", ":8880")}


def V(value, frm: str, **extra) -> dict:
    d = {"value": value, "from": frm}
    d.update(extra)
    return d


def fmt(sec):
    return None if sec is None else FC.fmt(sec)


def dur(v):
    try:
        return FC.dur(v)
    except ValueError:
        return None


def sha(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def fullmatch(op: str, pattern: str, value: str) -> bool:
    return MS.matches(op, pattern, value)


# ---------- rules ----------

def load_rule_docs(files: list[str]) -> list[dict]:
    """[{file, group, interval, type, fields, rule}] for every rule (all group types)."""
    out = []
    for path in files:
        doc = C._load(path) or {}
        for g in doc.get("groups") or []:
            for r in g.get("rules") or []:
                out.append({"file": path, "group": g, "rule": r})
    return out


def infer_ruler(docs: list[dict], config_kind: str | None, answers: dict) -> dict:
    if answers.get("ruler"):
        return V(answers["ruler"], "user")
    vm, prom = set(), set()
    for d in docs:
        g, r = d["group"], d["rule"]
        vm |= {f"group.{k}" for k in VMALERT_GROUP_FIELDS if k in g}
        vm |= {f"rule.{k}" for k in VMALERT_RULE_FIELDS if k in r}
        prom |= {f"group.{k}" for k in PROMETHEUS_GROUP_FIELDS if k in g}
    if vm and not prom:
        return V("vmalert", "file", evidence=sorted(vm))
    if prom and not vm:
        return V("prometheus", "file", evidence=sorted(prom))
    if vm and prom:
        return V(None, "unknown", evidence=sorted(vm | prom), note="rule 檔同時有 vmalert 與 Prometheus 專用欄位")
    if config_kind == "vmagent":
        return V("vmalert", "hint", evidence=["config 是 vmagent 的 scrape 設定"])
    if config_kind == "prometheus":
        return V("prometheus", "hint", evidence=["config 是 prometheus.yml；rule 檔沒有任何一方專用的欄位"])
    return V(None, "unknown", note="rule 檔兩種 ruler 都能讀，沒有 config 可判斷")


def subquery_ranges(expr: str) -> list[dict]:
    """`(...)[15m:1m]` or `f(...)[1h:]` subqueries (selector-level ranges come from MS.selectors)."""
    out = []
    for m in re.finditer(r"\)\s*\[([^\]]*:[^\]]*)\]", expr):
        depth, i = 0, m.start()
        while i >= 0:
            if expr[i] == ")":
                depth += 1
            elif expr[i] == "(":
                depth -= 1
                if depth == 0:
                    break
            i -= 1
        out.append({"range": m.group(1).strip(), "pos": max(i, 0)})
    return out


def range_entries(expr: str, sels: list[dict]) -> list[dict]:
    out = []
    items = [(s["range"], s["pos"], s.get("metric")) for s in sels if s.get("range")]
    items += [(q["range"], q["pos"], None) for q in subquery_ranges(expr)]
    for rng, pos, metric in sorted(items, key=lambda x: x[1]):
        func = MS.enclosing_func(expr, pos)
        if func in FC.ONE_SAMPLE:
            n = 1
        else:
            n = 2  # TWO_SAMPLES, or an unknown wrapper: conservative (fit_contracts.samples_needed)
        e = {"func": func or None, "range": rng.split(":")[0], "samples_needed": n}
        if ":" in rng:
            e["subquery_step"] = rng.split(":", 1)[1] or None
        if metric:
            e["metric"] = metric
        out.append(e)
    return out


def detects_absence(expr: str, rule: dict) -> dict:
    via = []
    if re.search(r"\babsent(_over_time)?\s*\(", expr):
        via.append("absent")
    if re.search(r"\bunless\b", expr):
        via.append("unless")
    if re.search(r"\bup\s*(\{[^}]*\})?\s*==\s*0\b", expr):
        via.append("up==0")
    covered = (rule.get("annotations") or {}).get("absence_covered_by")
    if covered:
        via.append("absence_covered_by")
    d = {"value": bool(via), "via": via}
    if covered:
        d["covered_by"] = [x.strip() for x in str(covered).split(",") if x.strip()]
    return d


def required_matchers(matchers) -> list[tuple[str, str, str]]:
    """Matchers that need the label to exist with a value (job and __name__ excluded)."""
    out = []
    for label, op, val in matchers:
        if label in ("job", "__name__"):
            continue
        if op == "=" and val != "":
            out.append((label, op, val))
        elif op == "=~":
            try:
                if re.fullmatch(val, "") is None:
                    out.append((label, op, val))
            except re.error:
                pass
        elif op in ("!=", "!~") and val == "":
            out.append((label, op, val))
    return out


# ---------- scrape config ----------

def load_scrape(path: str, kind: str) -> dict:
    doc = C._load(path) or {}
    base = os.path.dirname(os.path.abspath(path))
    scs = list(doc.get("scrape_configs") or [])
    for pat in doc.get("scrape_config_files") or []:
        for f in sorted(glob.glob(os.path.join(base, pat))):
            scs += (C._load(f) or {}).get("scrape_configs") or []
    g = doc.get("global") or {}
    gS_raw, gT_raw = g.get("scrape_interval"), g.get("scrape_timeout")
    gS = dur(gS_raw) or 60.0
    gT = dur(gT_raw) or 10.0
    findings = []
    if gS_raw and gT_raw and gT > gS:
        findings.append({"kind": "timeout_gt_interval", "job": "(global)",
                         "fact": f"global scrape_timeout {fmt(gT)} > scrape_interval {fmt(gS)}"
                                 + ("（Prometheus 載入設定會失敗）" if kind == "prometheus" else "（vmagent 截成 scrape_interval）")})
    jobs = []
    for sc in scs:
        name = sc.get("job_name")
        S_raw, T_raw = sc.get("scrape_interval"), sc.get("scrape_timeout")
        S = dur(S_raw) or gS
        interval = V(fmt(S), "file" if (S_raw or gS_raw) else "default")
        if T_raw:
            T, tfrom = dur(T_raw), "file"
        elif gT_raw:
            T, tfrom = (min(gT, S) if kind == "prometheus" else gT), "file"
        else:
            T, tfrom = min(10.0, S), "default"
        eff = min(T, S)
        timeout = V(fmt(T), tfrom, effective=fmt(eff))
        if T > S:
            timeout["note"] = ("Prometheus 載入設定會失敗（scrape_timeout 不能大於 scrape_interval）" if kind == "prometheus"
                               else "vmagent 自動截成 scrape_interval，不報錯")
            findings.append({"kind": "timeout_gt_interval", "job": name,
                             "fact": f"job {name} 的 scrape_timeout {fmt(T)} > scrape_interval {fmt(S)}；" + timeout["note"]})
        static_targets, static_labels = [], {}
        for st in sc.get("static_configs") or []:
            st = st or {}
            static_targets += [str(t) for t in st.get("targets") or []]
            for k, v in (st.get("labels") or {}).items():
                static_labels.setdefault(str(k), set()).add(str(v))
        relabel = sc.get("relabel_configs") or []
        relabel_targets = sorted({str(r["target_label"]) for r in relabel + (sc.get("metric_relabel_configs") or [])
                                  if r.get("target_label") and r.get("action", "replace") in
                                  ("replace", "lowercase", "uppercase", "hashmod")} - {"__address__"})
        sds = sorted(k for k in sc if k.endswith("_sd_configs"))
        open_labels = [w for w, hit in (("honor_labels", bool(sc.get("honor_labels"))),
                                        ("labelmap", any(r.get("action") == "labelmap" for r in relabel)),
                                        ("file_sd_configs", "file_sd_configs" in sc),
                                        ("http_sd_configs", "http_sd_configs" in sc)) if hit]
        dropped, kept = [], []
        for r in sc.get("metric_relabel_configs") or []:
            if r.get("source_labels") == ["__name__"]:
                if r.get("action") == "drop":
                    dropped.append(str(r.get("regex", "(.*)")))
                elif r.get("action") == "keep":
                    kept.append(str(r.get("regex", "(.*)")))
        jobs.append({
            "name": name,
            "interval": interval,
            "timeout": timeout,
            "targets": len(static_targets) if not sds else None,
            "static_targets": static_targets,
            "sd": sds or ["static_configs"],
            "metrics_path": sc.get("metrics_path", "/metrics"),
            "target_labels": {"from_static": {k: sorted(v) for k, v in sorted(static_labels.items())},
                              "from_relabel": relabel_targets, "honor_labels": bool(sc.get("honor_labels")),
                              "open": open_labels},
            "rewrites_job": "job" in relabel_targets or bool(sc.get("honor_labels")),
            "dropped_metrics": dropped,
            "kept_only_metrics": kept,
            "_metric_relabel": sc.get("metric_relabel_configs") or [],
        })
    alerting = doc.get("alerting")
    am_targets = []
    for amc in (alerting or {}).get("alertmanagers") or []:
        for st in amc.get("static_configs") or []:
            am_targets += [str(t) for t in (st or {}).get("targets") or []]
        if any(k.endswith("_sd_configs") for k in amc):
            am_targets.append("(service discovery)")
    return {"global": g, "gS": gS, "gT": gT, "jobs": jobs, "rule_files": doc.get("rule_files"),
            "has_rule_files": "rule_files" in doc, "alerting": alerting, "am_targets": am_targets,
            "external_labels": {str(k): str(v) for k, v in (g.get("external_labels") or {}).items()},
            "alert_relabel": bool((alerting or {}).get("alert_relabel_configs")), "findings": findings}


def guess_config_kind(path: str | None, explicit: str | None) -> dict:
    if explicit:
        return V(explicit, "user")
    if not path:
        return V(None, "unknown")
    if "vmagent" in os.path.basename(path).lower():
        return V("vmagent", "hint", note="檔名含 vmagent")
    return V("prometheus", "default", note="沒有 --config-kind；當成 prometheus.yml")


# ---------- Alertmanager ----------

_AM_MATCHER = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*(.*?)\s*$')


def _split_matchers(s: str) -> list[str]:
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
    parts, cur, q = [], "", None
    for ch in s:
        if q:
            cur += ch
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            cur += ch
        elif ch == ",":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return [p for p in parts if p.strip()]


def am_matchers(node: dict, prefix: str = "") -> list[tuple[str, str, str]]:
    """matchers / match / match_re (or source_* / target_* with prefix) as (label, op, value)."""
    out = []
    for m in node.get(prefix + "matchers") or []:
        for part in _split_matchers(str(m)):
            mm = _AM_MATCHER.match(part)
            if mm:
                v = mm.group(3)
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out.append((mm.group(1), mm.group(2), v))
    for k, v in (node.get(prefix + "match") or {}).items():
        out.append((str(k), "=", str(v)))
    for k, v in (node.get(prefix + "match_re") or {}).items():
        out.append((str(k), "=~", str(v)))
    return out


def mtext(ms) -> str:
    return ", ".join(f'{l}{op}"{v}"' for l, op, v in ms) or "（全部）"


def eval_matchers(ms, labels: dict) -> tuple[str, list[str]]:
    """'yes' / 'no' / 'unknown' and the runtime labels the answer depends on.
    labels: name -> value, or None when the value is only known at runtime (templated or from the series)."""
    unknown = []
    for l, op, v in ms:
        val = labels.get(l, labels.get("__other__"))
        if val is None:
            unknown.append(l)
            continue
        vals = [val] if isinstance(val, str) else list(val)
        hits = [fullmatch(op, v, x) for x in vals]
        if not any(hits):
            return "no", []
        if not all(hits):
            unknown.append(l)
    return ("unknown", sorted(set(unknown))) if unknown else ("yes", [])


def route_tree(am: dict) -> dict:
    """Flatten the routing tree with inherited timings: {path: node}."""
    root = am.get("route") or {}
    nodes = []

    def walk(n, parent, path):
        eff = {}
        for k in ("receiver", "group_by", "group_wait", "group_interval", "repeat_interval"):
            if k in n:
                eff[k] = V(n[k], "file")
            elif parent:
                eff[k] = dict(parent["eff"][k], inherited=True)
            elif k in AM_DEFAULTS:
                eff[k] = V(AM_DEFAULTS[k], "default")
            else:
                eff[k] = V(None, "unknown")
        me = {"path": path, "matchers": am_matchers(n), "continue": bool(n.get("continue")), "eff": eff, "node": n,
              "children": []}
        nodes.append(me)
        for i, c in enumerate(n.get("routes") or []):
            me["children"].append(walk(c, me, f"{path}.routes[{i}]"))
        return me

    top = walk(root, None, "route")
    return {"root": top, "nodes": nodes}


def route_for(tree: dict, labels: dict) -> dict:
    """Alertmanager routing (first match unless continue) with three-valued matching."""
    runtime = set()

    def match(node, certain):
        res = []
        for c in node["children"]:
            m, unk = eval_matchers(c["matchers"], labels)
            if m == "no":
                continue
            runtime.update(unk)
            sub = match(c, certain and m == "yes")
            res += sub
            if m == "yes" and not c["continue"] and any(r[1] for r in sub):
                break
        if not any(r[1] for r in res):
            res.append((node, certain and not res))
        return res

    res = match(tree["root"], True)
    seen, outs = set(), []
    for node, certain in res:
        key = (node["path"], certain)
        if key in seen:
            continue
        seen.add(key)
        outs.append({"receiver": node["eff"]["receiver"]["value"], "path": node["path"], "certain": certain,
                     "group_wait": node["eff"]["group_wait"]["value"],
                     "group_interval": node["eff"]["group_interval"]["value"],
                     "repeat_interval": node["eff"]["repeat_interval"]["value"]})
    certain_recv = [o["receiver"] for o in outs if o["certain"]]
    r = {"receiver": certain_recv[0] if len(certain_recv) == 1 and not runtime else None,
         "receivers": sorted({o["receiver"] for o in outs if o["receiver"]}), "matches": outs, "from": "file"}
    if len(certain_recv) > 1:
        r["receiver"] = None
        r["note"] = "continue：同時送到多個 receiver"
    if runtime:
        r["depends_on_runtime_labels"] = sorted(runtime)
    return r


def rule_labels(rule: dict, external: dict, series: dict | None = None) -> dict:
    """Alert labels as far as files tell: series labels (value, set of possible values, or None = runtime),
    overridden by rule labels (templated -> None) and alertname; external_labels only where absent.
    series["__other__"] = "" means every other label is known to be absent."""
    out = dict(series or {})
    for k, v in (rule.get("labels") or {}).items():
        v = str(v)
        out[str(k)] = None if "{{" in v else v
    out["alertname"] = rule.get("alert")
    for k, v in external.items():
        if k not in out or out[k] == "":
            out[k] = v
    return out


_AGG = re.compile(r"\b(sum|min|max|avg|count|group|topk|bottomk|quantile|stddev|stdvar|count_values|limitk)\b\s*(by|without|\()", re.I)


def series_labels(expr: str, sels: list[dict], jobs: list[dict], matched: set) -> dict:
    """Target labels the alert series carry, from the config. Only for expressions without aggregation
    (aggregation keeps an unknown subset); labels from relabel / SD / honor_labels stay runtime."""
    if not matched or _AGG.search(expr) or not all(j in {x["name"] for x in jobs} for j in matched):
        return {}
    js = [x for x in jobs if x["name"] in matched]
    if any(j["target_labels"]["open"] or j["sd"] != ["static_configs"] for j in js):
        return {}
    out = {"job": set(matched)}
    if not any("instance" in j["target_labels"]["from_relabel"] for j in js):
        out["instance"] = {t for j in js for t in j["static_targets"]}
    names = {l for j in js for l in j["target_labels"]["from_static"]}
    for l in names:
        vals = set()
        for j in js:
            vals |= set(j["target_labels"]["from_static"].get(l) or [""])
        out[l] = vals
    for j in js:
        for l in j["target_labels"]["from_relabel"]:
            out[l] = None
    if all(s.get("metric") == "up" or (s.get("metric") or "").startswith("scrape_") for s in sels):
        out["__other__"] = ""  # up / scrape_* carry only target labels
    return {k: (next(iter(v)) if isinstance(v, set) and len(v) == 1 else v) for k, v in out.items()}


def receiver_empty(r: dict) -> bool:
    return not any(k.endswith("_configs") and v for k, v in r.items())


# ---------- main model ----------

def build(a) -> dict:
    answers = (C._load(a.answers) or {}) if a.answers else {}
    docs = load_rule_docs(a.rules)
    kind = guess_config_kind(a.config, a.config_kind)
    cfg = load_scrape(a.config, kind["value"]) if a.config else None
    am = (C._load(a.alertmanager) or {}) if a.alertmanager else None
    findings: list[dict] = []
    inv: dict = {}

    srcs = {"config": a.config, "config_kind": kind, "alertmanager": a.alertmanager, "rules": list(a.rules),
            "answers": a.answers}
    srcs["sha256"] = {p: sha(p) for p in [a.config, a.alertmanager, a.answers, *a.rules] if p}
    inv["sources"] = srcs

    # ---- stack ----
    ruler = infer_ruler(docs, kind["value"], answers)
    stack = {"ruler": ruler,
             "version": V(answers.get("version"), "user") if answers.get("version") else V(None, "unknown")}
    if cfg:
        if kind["value"] == "vmagent":
            stack["evaluation_interval"] = V(None, "unknown", note="vmalert -evaluationInterval（預設 1m），不在 scrape 設定裡")
            stack["rule_files_loaded"] = V(None, "unknown", note="vmalert 用 -rule 參數載入")
            stack["rules_given_are_loaded"] = V("unknown", "unknown")
            stack["alertmanagers"] = V(None, "unknown", note="vmalert 用 -notifier.url")
        else:
            ei = cfg["global"].get("evaluation_interval")
            stack["evaluation_interval"] = V(ei, "file") if ei else V("1m", "default")
            if cfg["global"].get("rule_query_offset"):
                stack["rule_query_offset"] = V(cfg["global"]["rule_query_offset"], "file")
            rf = cfg["rule_files"] or []
            stack["rule_files_loaded"] = V(rf, "file")
            per = {}
            for p in a.rules:
                ab, bn = os.path.abspath(p), os.path.basename(p)
                cdir = os.path.dirname(os.path.abspath(a.config))
                if any(fnmatch.fnmatch(ab, os.path.normpath(os.path.join(cdir, g))) for g in rf):
                    per[p] = True
                elif any(fnmatch.fnmatch(bn, os.path.basename(g)) for g in rf):
                    per[p] = "unknown"
                else:
                    per[p] = False
            vals = set(per.values())
            overall = True if vals == {True} else False if vals == {False} else "unknown"
            stack["rules_given_are_loaded"] = V(overall, "file", per_file=per,
                                                note="依 rule_files 的 glob 比對；檔名符合但路徑不同時是 unknown")
            if not cfg["has_rule_files"]:
                findings.append({"kind": "config_missing", "fact": "config 沒有 rule_files：這份 config 不會載入任何 rule"})
            elif overall is False:
                findings.append({"kind": "rules_not_loaded", "fact": "給的 rule 檔檔名都不符合 rule_files 的 glob（" + ", ".join(rf) + "）"})
            stack["alertmanagers"] = V(cfg["am_targets"], "file")
            if not cfg["alerting"]:
                findings.append({"kind": "config_missing", "fact": "config 沒有 alerting：這台 Prometheus 不會把 alert 送到任何 Alertmanager"})
            if cfg["external_labels"]:
                stack["external_labels"] = V(cfg["external_labels"], "file")
            if cfg["alert_relabel"]:
                stack["alert_relabel_configs"] = V(True, "file", note="送出前會改寫 alert label；路由結果可能不同")
    else:
        for k in ("evaluation_interval", "rule_files_loaded", "alertmanagers"):
            stack[k] = V(None, "unknown")
        stack["rules_given_are_loaded"] = V("unknown", "unknown")
    eo = answers.get("external_observer")
    stack["external_observer"] = V(eo, "user") if eo is not None else V("unknown", "unknown")
    inv["stack"] = stack

    # ---- jobs ----
    cfg_jobs = cfg["jobs"] if cfg else []
    job_names_ans = [str(x) for x in answers.get("job_names") or []]
    known_jobs = sorted({j["name"] for j in cfg_jobs} | set(job_names_ans))
    rewrites = [j["name"] for j in cfg_jobs if j["rewrites_job"]]
    job_values_complete = bool(cfg) and (not rewrites or bool(job_names_ans)) or bool(job_names_ans)
    by_job = {j["name"]: j for j in cfg_jobs}
    recorded = {d["rule"].get("record") for d in docs if d["rule"].get("record")}
    exact = MS.catalog_hints(C.ROOT)
    producers = {str(k): [str(x) for x in v or []] for k, v in (answers.get("producers") or {}).items()}
    ans_labels = {str(k): str(v) for k, v in (answers.get("labels") or {}).items()}
    measured = answers.get("measured") or {}

    # ---- rules (first pass: selectors per rule) ----
    rules_out, job_metrics, rule_jobs_used = [], {}, {}
    for d in docs:
        r, g = d["rule"], d["group"]
        name = r.get("alert") or r.get("record")
        expr = str(r.get("expr", ""))
        gtype = g.get("type")
        entry = {"name": name, "kind": "alert" if "alert" in r else "record", "file": d["file"],
                 "group": {"name": g.get("name"), "interval": V(g["interval"], "file") if g.get("interval")
                           else dict(stack.get("evaluation_interval") or V(None, "unknown"), inherited=True)}}
        if gtype not in (None, "", "prometheus"):
            entry["group"]["type"] = gtype
            entry["note"] = f"type {gtype}：不是 PromQL／MetricsQL，沒有分析 selector"
            rules_out.append(entry)
            continue
        sels = MS.selectors(expr)
        used, matched, sel_rows, jms = [], set(), [], set()
        for s in sels:
            jm = [(l, op, v) for (l, op, v) in s["matchers"] if l == "job"]
            used += [f'job{op}"{v}"' for (_, op, v) in jm]
            jms |= {(op, v) for (_, op, v) in jm}
            hits = None
            if known_jobs and jm:
                hits = [j for j in known_jobs if all(fullmatch(op, v, j) for (_, op, v) in jm)]
            elif known_jobs and not jm and (s.get("metric") == "up" or (s.get("metric") or "").startswith("scrape_")):
                hits = list(known_jobs)
            if hits:
                matched |= set(hits)
            in_absent = any(f in MS.ABSENT_FUNCS for f in MS.enclosing_funcs(expr, s["pos"]))
            row = {"metric": s.get("metric"), "jobs": hits, "in_absent": in_absent, "empty": None, "why": None,
                   "required": required_matchers(s["matchers"])}
            m = s.get("metric")
            if m in recorded:
                row["recording"] = True
            elif hits is not None and job_values_complete:
                if not hits and jm:
                    row["empty"], row["why"] = True, "job"
                elif hits and m and all(j in by_job and MS.dropped_by({"metric_relabel": by_job[j]["_metric_relabel"]}, m)
                                        for j in hits):
                    row["empty"], row["why"] = True, "dropped"
                else:
                    row["empty"] = False
            if m and hits:
                for j in hits:
                    job_metrics.setdefault(j, set()).add(m)
            sel_rows.append(row)
        entry["jobs_used"] = sorted(set(used))
        if known_jobs:
            entry["jobs_matched"] = V(sorted(matched), "file" if not job_names_ans else "user" if not cfg else "file")
            if not job_values_complete and any(not any(fullmatch(op, v, j) for j in known_jobs) for op, v in jms):
                entry["jobs_matched"]["note"] = f"relabel／honor_labels 會改寫 job（{', '.join(rewrites)}）；實際 job 名稱要確認"
        else:
            entry["jobs_matched"] = V(None, "unknown", note="沒有 config，也沒有 answers.job_names")
        entry["_sels"] = sel_rows
        entry["_selraw"] = sels
        entry["_jm"] = sorted(jms)
        entry["_expr"] = expr
        entry["_rule"] = r
        entry["ranges"] = range_entries(expr, sels)
        entry["detects_absence"] = detects_absence(expr, r)
        if r.get("for"):
            entry["for"] = r.get("for")
        if r.get("keep_firing_for"):
            entry["keep_firing_for"] = r.get("keep_firing_for")
        rule_jobs_used[name] = matched
        rules_out.append(entry)

    # ---- jobs (with exporter hints from the metrics rules read from them) ----
    jobs_out = []
    if cfg:
        job_list = cfg_jobs
    else:
        seen_j = {}
        for e in rules_out:
            for op, v in e.get("_jm", []):
                if op in ("=", "=~"):
                    seen_j.setdefault(v, op)
        for v in job_names_ans:
            seen_j[v] = "answers"
        job_list = [{"name": v, "_from_rules": True, "_op": op} for v, op in sorted(seen_j.items())]
    for j in job_list:
        name = j["name"]
        out = {"name": name}
        if j.get("_from_rules"):
            out["from"] = "user" if name in job_names_ans else "unknown"
            out["note"] = "沒有 config：job 名稱取自 rule 的 job 條件" if name not in job_names_ans else "answers.job_names"
            if j["_op"] == "=~":
                out["pattern"] = True
                out["note"] = "沒有 config：這是 rule 的 job 正規表示式，不是 job 名稱"
            for k in ("interval", "timeout"):
                out[k] = V(None, "unknown")
            out["targets"] = None
        else:
            out.update({k: j[k] for k in ("interval", "timeout", "targets", "sd", "target_labels", "dropped_metrics")})
            if j["kept_only_metrics"]:
                out["kept_only_metrics"] = j["kept_only_metrics"]
            if j["rewrites_job"]:
                out["rewrites_job"] = True
        prod = [p for p, js in producers.items() if name in js]
        if prod:
            out["exporter"] = V(prod[0], "user")
        else:
            votes: dict[str, int] = {}
            for m in sorted(job_metrics.get(name, ())):
                for c in exporter_hint(m, exact):
                    votes[c] = votes.get(c, 0) + 1
            ports = {t.rsplit(":", 1)[-1] for t in j.get("static_targets") or [] if ":" in t}
            by_port = sorted({DEFAULT_PORTS[p] for p in ports if p in DEFAULT_PORTS})
            if len(by_port) == 1 and by_port[0] in votes:
                out["exporter"] = V(by_port[0], "hint", basis="預設 port，且 rule 讀的 metric 在這張目錄卡片")
            elif votes:
                best = max(sorted(votes), key=lambda c: votes[c])
                out["exporter"] = V(best, "hint", basis=f"rule 讀這個 job 的 {votes[best]} 個 metric 在目錄卡片 {best}")
            elif len(by_port) == 1:
                out["exporter"] = V(by_port[0], "hint", basis="target 用的是它的預設 port")
            else:
                out["exporter"] = V(None, "unknown")
        ex = out["exporter"]["value"]
        tau = dur(out["timeout"].get("effective") or out["timeout"]["value"]) if out["timeout"]["value"] else None
        if ex in EXPORTER_DEADLINE:
            f, why = EXPORTER_DEADLINE[ex]
            x = f(tau) if tau is not None else None
            if x is None and ex in ("redis_exporter", "postgres_exporter"):
                x = f(0)
            out["exporter_deadline"] = V(fmt(x), "default", basis=why, ref="docs/tools/baseline.md#defaults")
            if x is not None and tau is not None and x >= tau:
                findings.append({"kind": "exporter_deadline_ge_timeout", "job": name,
                                 "fact": f"job {name}：{ex} 預設期限 {fmt(x)} ≥ scrape_timeout {fmt(tau)}（預設值，未確認 exporter 參數）"})
        else:
            out["exporter_deadline"] = V(None, "unknown")
        ms = measured.get(name) or {}
        out["measured"] = ({"d_p99": ms.get("d_p99"), "d_max": ms.get("d_max"), "up0_count": ms.get("up0_count"), "from": "user"}
                           if ms else {"d_p99": None, "d_max": None, "up0_count": None, "from": "unknown"})
        jobs_out.append(out)
    inv["jobs"] = jobs_out

    # ---- unscraped components ----
    unscraped = []
    if cfg:
        all_targets = {t: j["name"] for j in cfg_jobs for t in j["static_targets"]}
        for t in cfg["am_targets"]:
            if t == "(service discovery)":
                continue
            if t in all_targets or any(re.search("alertmanager", j["name"] or "") for j in cfg_jobs):
                continue
            unscraped.append({"component": "alertmanager", "target": t, "from": "file",
                              "fact": f"alerting 的 {t} 沒有 scrape job 在抓"})
        rk = ruler["value"]
        if rk in RULER_SELF:
            pat, port = RULER_SELF[rk]
            hit = [j["name"] for j in cfg_jobs if re.fullmatch(pat, j["name"] or "")
                   or any(t.endswith(port) for t in j["static_targets"])]
            if not hit:
                unscraped.append({"component": rk, "target": None, "from": "file" if ruler["from"] == "file" else "hint",
                                  "fact": f"沒有 scrape job 在抓 ruler（{rk}）本身"})
        for u in unscraped:
            findings.append({"kind": "component_unscraped", "fact": u["fact"]})
    inv["unscraped_components"] = unscraped if cfg else V(None, "unknown", note="沒有 config")

    # ---- rules (second pass: labels, emptiness, keep_range, route) ----
    tree = route_tree(am) if am else None
    receivers = {r.get("name"): r for r in (am or {}).get("receivers") or []}
    keep_ans = answers.get("keep_range") or {}
    ext = cfg["external_labels"] if cfg else {}
    fires_by: dict[tuple, list] = {}
    for e in rules_out:
        if "_sels" not in e:
            continue
        r, name, sels = e.pop("_rule"), e["name"], e.pop("_sels")
        r_expr, r_sels = e.pop("_expr"), e.pop("_selraw")
        # labels required
        req = {}
        for s in sels:
            for (l, op, v) in s["required"]:
                if l not in IDENTITY_LABELS and not (l == "instance" and op == "="):
                    continue
                key = (l, op, v)
                st = req.setdefault(key, {"label": l, "match": f'{op}"{v}"', "status": None, "from": None, "metrics": set(),
                                          "on_up": False})
                st["metrics"].add(s["metric"] or "?")
                st["on_up"] |= (s["metric"] == "up" or (s["metric"] or "").startswith("scrape_"))
                st.setdefault("_jobs", set()).update(s["jobs"] or [])
        lr = []
        for (l, op, v), st in req.items():
            jobs_hit = [by_job[j] for j in st.pop("_jobs") if j in by_job]
            st["metrics"] = sorted(st["metrics"])
            if l in ans_labels:
                none = ans_labels[l].lower() in MS.NO_LABEL
                st["status"], st["from"] = ("missing" if none else "answered"), "user"
                st["answer"] = ans_labels[l]
            elif not jobs_hit:
                st["status"], st["from"] = "unknown", "unknown"
            elif l == "instance":
                vals = [t for j in jobs_hit for t in j["static_targets"]]
                if any("instance" in j["target_labels"]["from_relabel"] or j["target_labels"]["open"] or j["sd"] != ["static_configs"]
                       for j in jobs_hit):
                    st["status"], st["from"] = "unknown", "unknown"
                elif any(fullmatch(op, v, t) for t in vals):
                    st["status"], st["from"] = "static_target", "file"
                else:
                    st["status"], st["from"] = "missing", "file"
                    st["static_values"] = vals
            else:
                static_vals = sorted({x for j in jobs_hit for x in j["target_labels"]["from_static"].get(l, [])})
                relabeled = any(l in j["target_labels"]["from_relabel"] for j in jobs_hit)
                opened = any(j["target_labels"]["open"] for j in jobs_hit)
                if static_vals and not relabeled and not opened:
                    ok = any(fullmatch(op, v, x) for x in static_vals) if v != "" else True
                    st["status"], st["from"] = ("static_label" if ok else "missing"), "file"
                    if not ok:
                        st["static_values"] = static_vals
                elif relabeled:
                    st["status"], st["from"] = "relabel", "file"
                elif opened:
                    st["status"], st["from"] = "unknown", "unknown"
                elif st["on_up"]:
                    st["status"], st["from"] = "missing", "file"
                    st["note"] = "up／scrape_* 只帶 target label，config 沒有加這個 label"
                else:
                    st["status"], st["from"] = "exporter_or_unknown", "unknown"
            st.pop("on_up")
            lr.append(st)
            if st["status"] == "missing":
                for s in sels:
                    if (l, op, v) in s["required"] and s["empty"] is not True:
                        s["empty"], s["why"] = True, "label"
                findings.append({"kind": "label_not_produced", "rule": name,
                                 "fact": f"{name} 要 {l}{st['match']}，" + (
                                     f"answers 說沒有這個 label" if st["from"] == "user" else
                                     f"static 值只有 {st.get('static_values')}" if st.get("static_values") is not None else
                                     "matched job 的 config 沒有產生這個 label")})
        e["labels_required"] = sorted(lr, key=lambda x: x["label"])
        # never / always fires
        absent_empty = [s for s in sels if s["in_absent"] and s["empty"]]
        other = [s for s in sels if not s["in_absent"]]
        if absent_empty:
            e["fires"] = V("always", "file", why=sorted({s["why"] for s in absent_empty}))
            fires_by.setdefault(("always_fires", ", ".join(e["fires"]["why"])), []).append(name)
        elif other and all(s["empty"] for s in other):
            e["fires"] = V("never", "file", why=sorted({s["why"] for s in other}))
            fires_by.setdefault(("never_fires", ", ".join(e["fires"]["why"])), []).append(name)
        elif any(s["empty"] for s in other):
            e["fires"] = V("partly_empty", "file", note="部分 selector 一定是空的；結果依運算子而定")
        for s in sels:
            if s["why"] == "dropped":
                findings.append({"kind": "dropped_metric_used", "rule": name,
                                 "fact": f"{name} 讀的 {s['metric']} 被 metric_relabel_configs 丟掉（job {', '.join(s['jobs'])}）"})
        # ranges: short windows (k=1, d=τ, conservative) and job interval
        for rg in e["ranges"]:
            js = [by_job[j] for j in rule_jobs_used.get(name, ()) if j in by_job]
            I = max((dur(j["interval"]["value"]) for j in js), default=cfg["gS"] if cfg else None)
            T = max((dur(j["timeout"]["effective"]) for j in js), default=cfg["gT"] if cfg else None)
            R = dur(rg["range"])
            if I and R is not None:
                need = (rg["samples_needed"] + 1) * I + (T or 0)
                rg["data_interval"] = fmt(I)
                rg["short"] = R < need
                rg["short_basis"] = f"(n+1)·I + τ = {fmt(need)}（k=1、d=τ 的保守值）"
            else:
                rg["short"] = None
        if e["ranges"]:
            ka = keep_ans.get(name)
            ann = (r.get("annotations") or {}).get("keep_range")
            e["keep_range"] = V(ka, "user") if ka is not None else V(ann, "file") if ann is not None else V("unknown", "unknown")
        if e["kind"] == "alert":
            e["_lab"] = rule_labels(r, ext, series_labels(r_expr, r_sels, cfg_jobs, rule_jobs_used.get(name, set())))
            lo = {str(k): str(v) for k, v in (r.get("labels") or {}).items()}
            e["labels_out"] = lo
            if tree:
                e["route"] = route_for(tree, e["_lab"])
            else:
                e["route"] = V(None, "unknown", note="沒有 alertmanager.yml")
    inv["rules"] = rules_out
    WHY = {"job": "job 對不上", "dropped": "metric 被丟掉", "label": "label 不存在"}
    for (kind, why), names in fires_by.items():
        reason = "、".join(WHY.get(w.strip(), w) for w in why.split(","))
        verb = "absent() 的 selector 一定是空的，一直在響" if kind == "always_fires" else "selector 全部回傳空，永遠不會響"
        findings.append({"kind": kind, "rules": names, "reason": why, "fact": f"{len(names)} 條 rule {verb}（{reason}）：{', '.join(names)}"})

    # findings from config / jobs
    if cfg:
        findings[:0] = cfg["findings"]
        missing: dict[str, list] = {}
        for e in rules_out:
            for op, val in e.pop("_jm", []):
                if op in ("=", "=~") and not any(fullmatch(op, val, j) for j in known_jobs):
                    missing.setdefault(f'job{op}"{val}"', []).append(e["name"])
        for ju, names in missing.items():
            val = ju.split('"', 1)[1][:-1]
            close = difflib.get_close_matches(val, known_jobs, n=1, cutoff=0.6)
            f = {"kind": "job_not_in_config", "job": ju, "rules": names, "closest": close[0] if close else None,
                 "fact": f"{len(names)} 條 rule 用的 {ju} 對不到 config 的任何 job（config 有 {', '.join(known_jobs)}"
                         + (f"；名稱最接近的是 {close[0]}" if close else "") + "）"}
            if not job_values_complete:
                f["fact"] += "；relabel 會改寫 job，未確認"
            findings.append(f)

    for e in rules_out:
        e.pop("_jm", None)

    # ---- alertmanager ----
    if am is not None:
        nodes = tree["nodes"]
        recv_out = [{"name": n, "empty": receiver_empty(r), "kinds": sorted(k for k in r if k.endswith("_configs") and r[k])}
                    for n, r in receivers.items()]
        empty = {x["name"] for x in recv_out if x["empty"]}
        routes = []
        for n in nodes:
            routes.append({"path": n["path"], "matchers": mtext(n["matchers"]) if n["path"] != "route" else "（root）",
                           "continue": n["continue"], **{k: n["eff"][k] for k in
                                                         ("receiver", "group_by", "group_wait", "group_interval", "repeat_interval")}})
        summ = [f"{'  ' * n['path'].count('routes[')}{mtext(n['matchers'])} → {n['eff']['receiver']['value']}"
                + ("（continue）" if n["continue"] else "") for n in nodes if n["path"] != "route"]
        summ.append(f"其他 → {nodes[0]['eff']['receiver']['value']}")
        g = am.get("global") or {}
        amo = {"global": {"resolve_timeout": V(g["resolve_timeout"], "file") if g.get("resolve_timeout") else V("5m", "default")},
               "routes_summary": "；".join(x.strip() for x in summ), "routes": routes, "receivers": recv_out}
        for k in ("time_intervals", "mute_time_intervals"):
            if am.get(k):
                amo[k] = [t.get("name") for t in am[k]]
        alerts = [e for e in rules_out if e["kind"] == "alert" and isinstance(e.get("route"), dict) and "receivers" in e["route"]]
        unr = []
        for e in alerts:
            rt = e["route"]
            hit = [o for o in rt["matches"] if o["receiver"] in empty]
            if hit:
                certain = any(o["certain"] for o in hit) and not rt.get("depends_on_runtime_labels")
                unr.append({"rule": e["name"], "receiver": hit[0]["receiver"], "certain": certain,
                            "severity": e["labels_out"].get("severity")})
                if certain or len(rt["receivers"]) == 1:
                    findings.append({"kind": "routed_to_empty_receiver", "rule": e["name"],
                                     "fact": f"{e['name']}（severity {e['labels_out'].get('severity', '—')}）會送到空的 receiver {hit[0]['receiver']}"
                                             + ("" if certain else "（依 runtime label 而定）")})
            if rt["receivers"] and any(x not in receivers for x in rt["receivers"]):
                findings.append({"kind": "receiver_undefined", "rule": e["name"],
                                 "fact": f"{e['name']} 路由到的 receiver {rt['receivers']} 不在 receivers 清單"})
        amo["unrouted_or_blackholed_rules"] = unr
        inh_out = []
        for ih in am.get("inhibit_rules") or []:
            src, tgt = am_matchers(ih, "source_"), am_matchers(ih, "target_")
            equal = [str(x) for x in ih.get("equal") or []]
            srules = [e["name"] for e in alerts if eval_matchers(src, e["_lab"])[0] == "yes"]
            pairs = []
            for e in alerts:
                lt = single(e["_lab"])
                if eval_matchers(tgt, lt)[0] != "yes":
                    continue
                cond_always, cond_same = [], []
                for sname in srules:
                    if sname == e["name"]:
                        continue
                    ls = single(next(x for x in alerts if x["name"] == sname)["_lab"])
                    if all(l in ls and l in lt and ls[l] is not None and ls[l] == lt[l] for l in equal):
                        cond_always.append(sname)
                    elif not any(l in ls and l in lt and ls[l] is not None and lt[l] is not None and ls[l] != lt[l] for l in equal):
                        cond_same.append(sname)
                if cond_always or cond_same:
                    p = {"target": e["name"]}
                    if cond_always:
                        p["always_when_firing"] = cond_always
                    if cond_same:
                        p["when_same_labels"] = {"sources": cond_same, "labels": equal}
                    pairs.append(p)
                    if cond_always:
                        findings.append({"kind": "always_inhibited", "rule": e["name"],
                                         "fact": f"{', '.join(cond_always)} 在響時 {e['name']} 一定被抑制（equal: {equal or '無'}）"})
            inh_out.append({"source": mtext(src), "target": mtext(tgt), "equal": equal, "source_rules": srules,
                            "suppressed": pairs})
        amo["inhibit"] = inh_out
        amo["ends_at_inputs"] = {"evaluation_interval": (stack.get("evaluation_interval") or {}).get("value"),
                                 "resend_delay": V("1m", "default") if ruler["value"] == "prometheus" else V(None, "unknown"),
                                 "contract": "resend-endsat",
                                 "note": "只記錄數值；EndsAt 與 repeat_interval 的關係見 docs/contracts.md#resend-endsat"}
        inv["alertmanager"] = amo
    else:
        inv["alertmanager"] = V(None, "unknown", note="沒有 alertmanager.yml")

    for e in rules_out:
        e.pop("_lab", None)

    # dedupe findings, drop internal keys
    seen, fl = set(), []
    for f in findings:
        if f["fact"] in seen:
            continue
        seen.add(f["fact"])
        fl.append(f)
    inv["findings_from_files"] = fl
    inv["questions"] = questions(inv, cfg, am, a, fl)
    return inv


GENERIC_PREFIX = {"http", "go", "process", "grpc", "promhttp", "net", "rpc", "scrape"}


def exporter_hint(metric: str, exact: dict[str, list[str]]) -> list[str]:
    """Catalog cards that list this metric (or its base name); prefix guesses only for distinctive prefixes."""
    if metric.split("_", 1)[0] in GENERIC_PREFIX:
        return []  # process_*, go_*, http_*: every component exports them
    return MS.hint_for(metric, exact)


def single(labels: dict) -> dict:
    return {k: v for k, v in labels.items() if isinstance(v, str) and k != "__other__"}


# ---------- questions ----------

def questions(inv, cfg, am, a, findings) -> list[dict]:
    rules = [r for r in inv["rules"] if "jobs_used" in r]
    ranged = [r for r in rules if r.get("ranges")]
    qs = []

    def cnt(*kinds, reason=None):
        return sum(1 for f in findings if f["kind"] in kinds and (reason is None or reason in str(f.get("reason", ""))))
    jnf = [f for f in findings if f["kind"] == "job_not_in_config"]
    if jnf:
        jobs = sorted(f["job"] + (f"（是 {f['closest']} 嗎？）" if f.get("closest") else "") for f in jnf)
        qs.append({"q": f"rule 用的 {', '.join(jobs)} 在 config 裡對不到；實際的 job 名稱是什麼（relabel 之後）？",
                   "changes": ["rule 會不會響", "absent() 是否一直在響", "metric 來源"], "affects": len({n for f in jnf for n in f["rules"]}),
                   "findings": len(jnf) + cnt("never_fires", "always_fires", reason="job"),
                   "answer_key": "job_names"})
    if not cfg:
        qs.append({"q": "可以給 prometheus.yml（或 vmagent 的 scrape 設定）嗎？", "changes": ["job 名稱", "scrape_interval／timeout",
                   "丟掉的 metric", "monitor of monitor"], "affects": len(rules), "answer_key": "--config"})
    if not am:
        alerts = [r for r in rules if r["kind"] == "alert"]
        qs.append({"q": "可以給 alertmanager.yml 嗎？", "changes": ["每條 rule 送到哪個 receiver", "inhibit", "空 receiver"],
                   "affects": len(alerts), "answer_key": "--alertmanager"})
    if inv["stack"]["version"]["from"] == "unknown":
        rk = inv["stack"]["ruler"]["value"] or "Prometheus／vmalert"
        qs.append({"q": f"評估這些 rule 的 {rk} 是哪個版本？",
                   "changes": ["query_offset／eval_delay 可用與否", "anchored", "samples_needed（vmalert）"],
                   "affects": len(ranged) + 1, "findings": 0, "answer_key": "version"})
    if inv["stack"]["external_observer"]["from"] == "unknown":
        un = inv["unscraped_components"] if isinstance(inv["unscraped_components"], list) else []
        qs.append({"q": "有沒有另一台（外部觀察者）在監控這台 ruler 和 Alertmanager？",
                   "changes": ["monitor of monitor", "Watchdog", *sorted({f"{u['component']} 沒被抓" for u in un})],
                   "affects": 1 + len(un), "findings": cnt("component_unscraped"),
                   "answer_key": "external_observer"})
    jobs_ranged = sorted({j for r in ranged for j in (r["jobs_matched"].get("value") or [])})
    unmeasured = [j["name"] for j in inv["jobs"] if j["name"] in jobs_ranged and j["measured"]["from"] == "unknown"]
    if unmeasured:
        qs.append({"q": f"{', '.join(unmeasured)} 的 scrape 耗時 p99／最大值與 up==0 次數（7 天）",
                   "changes": ["scrape_timeout", "range 視窗", "k"], "affects": len(ranged),
                   "findings": cnt("timeout_gt_interval", "exporter_deadline_ge_timeout"), "answer_key": "measured"})
    short = [r["name"] for r in ranged if any(g.get("short") for g in r["ranges"]) and r["keep_range"]["from"] == "unknown"]
    if short:
        qs.append({"q": f"{', '.join(short[:5])}{' 等' if len(short) > 5 else ''} 的短視窗是刻意的嗎？",
                   "changes": ["方案 A（改 rule）能不能用"], "affects": len(short), "answer_key": "keep_range"})
    hinted = [j for j in inv["jobs"] if j["exporter"]["from"] == "hint"]
    if hinted:
        n = sum(1 for r in rules if set(r["jobs_matched"].get("value") or []) & {j["name"] for j in hinted})
        qs.append({"q": "確認 exporter：" + "、".join(f"{j['name']} = {j['exporter']['value']}？" for j in hinted) + "（版本與參數）",
                   "changes": ["exporter 期限", "metric 來源", "目錄卡片"], "affects": n,
                   "findings": cnt("exporter_deadline_ge_timeout"), "answer_key": "producers"})
    lbl = {}
    for r in rules:
        for l in r.get("labels_required", []):
            if l["status"] in ("exporter_or_unknown", "unknown"):
                lbl.setdefault(l["label"], set()).add(r["name"])
    if lbl:
        qs.append({"q": "這些 label 從哪裡來（exporter 自己輸出、relabel、或根本沒有）：" + "、".join(sorted(lbl)) + "？",
                   "changes": ["rule 會不會響", "拓撲（primary／cluster）"], "affects": len(set().union(*lbl.values())), "findings": 0,
                   "answer_key": "labels"})
    if cfg and inv["sources"]["config_kind"]["value"] == "prometheus" and (not cfg["has_rule_files"] or not cfg["alerting"]):
        qs.append({"q": "config 沒有 rule_files 或 alerting：給的是完整的 config 嗎？", "changes": ["rule 有沒有被載入", "alert 送到哪裡"],
                   "affects": len(rules), "findings": cnt("config_missing"), "answer_key": "--config"})
    for q in qs:
        q.setdefault("findings", 0)
    # answers that can overturn a finding first, then by the number of rules they change
    qs.sort(key=lambda q: (-q["findings"], -q["affects"]))
    for i, q in enumerate(qs):
        q["ask_now"] = i < 3
    return qs


# ---------- merge ----------

def _is_user(node) -> bool:
    return isinstance(node, dict) and (node.get("confirmed") is True or node.get("from") == "user")


def merge(new, old, same_sources: bool):
    """Keep user-confirmed nodes from old; when the source files did not change, a value that differs from
    what the files give now is a user edit and is kept (marked confirmed)."""
    if isinstance(new, dict) and isinstance(old, dict):
        if "from" in old and "from" in new and "value" in old:
            if _is_user(old):
                return old
            if same_sources and old.get("value") != new.get("value"):
                return dict(old, confirmed=True, **{"from": "user"})
            return new
        out = dict(new)
        for k, v in new.items():
            if k in old and k != "sources":
                out[k] = merge(v, old[k], same_sources)
        return out
    if isinstance(new, list) and isinstance(old, list) and all(isinstance(x, dict) and "name" in x for x in new + old):
        om = {x["name"]: x for x in old}
        return [merge(x, om[x["name"]], same_sources) if x["name"] in om else x for x in new]
    return new


# ---------- markdown ----------

def _v(node) -> str:
    if not isinstance(node, dict) or "value" not in node:
        return str(node)
    v = node["value"]
    s = "**不知道**" if v in (None, "unknown") else (", ".join(map(str, v)) if isinstance(v, list) else str(v))
    if v == []:
        s = "（空）"
    tag = {"file": "檔案", "default": "預設", "hint": "**推測**", "user": "使用者", "unknown": "**未知**"}.get(node["from"], node["from"])
    extra = f"，實際 {node['effective']}" if node.get("effective") and node["effective"] != v else ""
    return f"{s}{extra}（{tag}{'，已確認' if node.get('confirmed') else ''}）"


def markdown(inv) -> str:
    L = ["# 環境清單（從檔案整理）", "",
         "這是我從檔案理解的環境，哪裡不對？標 **未知**／**推測** 的是還沒確認的部分；直接改 inventory.yaml 或回答下面的問題。", ""]
    s = inv["sources"]
    L += [f"來源：config `{s['config'] or '（沒給）'}`（{s['config_kind']['value'] or '—'}）、alertmanager `{s['alertmanager'] or '（沒給）'}`、"
          f"rule 檔 {len(s['rules'])} 個、answers `{s['answers'] or '（沒給）'}`", ""]
    L += ["## 先問這幾題", ""]
    for q in [q for q in inv["questions"] if q["ask_now"]]:
        L.append(f"1. {q['q']}（會改變 {q['findings']} 項事實，影響 {q['affects']} 項：{'、'.join(q['changes'])}）")
    L += ["", "## 只看檔案就知道的事實", ""]
    L += [f"- {f['fact']}" for f in inv["findings_from_files"]] or ["- （沒有）"]
    st = inv["stack"]
    L += ["", "## 監控體系", "", "| 項目 | 值 |", "|---|---|"]
    names = {"ruler": "ruler", "version": "版本", "evaluation_interval": "`evaluation_interval`", "rule_files_loaded": "`rule_files`",
             "rules_given_are_loaded": "給的 rule 檔有被載入", "alertmanagers": "Alertmanager", "external_observer": "外部觀察者",
             "external_labels": "`external_labels`", "rule_query_offset": "`rule_query_offset`"}
    for k, v in st.items():
        L.append(f"| {names.get(k, k)} | {_v(v)} |")
    L += ["", "## Scrape job", "", "| job | `scrape_interval` | `scrape_timeout` | target | target label | 丟掉的 metric | exporter | exporter 期限 | 實測 d_p99／d_max／up0 |",
          "|---|---|---|---|---|---|---|---|---|"]
    for j in inv["jobs"]:
        tl = j.get("target_labels") or {}
        lab = ", ".join([*(f"{k}={'/'.join(v)}" for k, v in (tl.get("from_static") or {}).items()), *(f"{x}（relabel）" for x in tl.get("from_relabel") or []),
                         *(["honor_labels"] if tl.get("honor_labels") else [])]) or "—"
        m = j["measured"]
        meas = "**未知**" if m["from"] == "unknown" else f"{m['d_p99']}／{m['d_max']}／{m['up0_count']}"
        L.append(f"| {j['name']} | {_v(j['interval'])} | {_v(j['timeout'])} | {j.get('targets') if j.get('targets') is not None else '—'} | {lab} | "
                 f"{', '.join(j.get('dropped_metrics') or []) or '—'} | {_v(j['exporter'])} | {_v(j['exporter_deadline'])} | {meas} |")
    un = inv["unscraped_components"]
    L += ["", "## 沒有被抓的元件", ""]
    if isinstance(un, list):
        L += [f"- {u['fact']}" for u in un] or ["- （沒有）"]
    else:
        L.append(f"- {_v(un)}")
    L += ["", "## Rule", "", "| rule | group（interval） | 對到的 job | 要的 label | 視窗 | 偵測消失 | `for` | 刻意的視窗 | receiver |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in inv["rules"]:
        if "jobs_used" not in r:
            continue
        jm = r["jobs_matched"]
        jtxt = ("**不知道**" if jm["value"] is None else ", ".join(jm["value"]) or "**（沒有）**") + (f"（用 {', '.join(r['jobs_used'])}）" if r["jobs_used"] else "")
        if r.get("fires"):
            jtxt += f"；**{ {'never': '永遠不會響', 'always': '一直在響', 'partly_empty': '部分 selector 是空的'}[r['fires']['value']] }**"
        lbl = ", ".join(f"{l['label']}{l['match']}（{l['status']}）" for l in r["labels_required"]) or "—"
        rng = ", ".join(f"{g['func']}[{g['range']}]" + ("（短）" if g.get("short") else "") for g in r["ranges"]) or "—"
        ab = "是（" + ", ".join(r["detects_absence"]["via"]) + "）" if r["detects_absence"]["value"] else "否"
        kr = _v(r["keep_range"]) if r.get("keep_range") else "—"
        rt = r.get("route")
        if not rt:
            recv = "—"
        elif "receivers" in rt:
            recv = rt["receiver"] or ("／".join(rt["receivers"]) + "（依 runtime label：" + ", ".join(rt.get("depends_on_runtime_labels", [])) + "）"
                                      if rt.get("depends_on_runtime_labels") else "、".join(rt["receivers"]))
        else:
            recv = "**未知**"
        gi = r["group"]["interval"]
        L.append(f"| {r['name']} | {r['group']['name']}（{gi.get('value') or '—'}） | {jtxt} | {lbl} | {rng} | {ab} | {r.get('for', '—')} | {kr} | {recv} |")
    amo = inv["alertmanager"]
    L += ["", "## Alertmanager", ""]
    if "routes" in amo:
        L += ["| route | matchers | continue | receiver | `group_wait` | `group_interval` | `repeat_interval` |", "|---|---|---|---|---|---|---|"]
        for rt in amo["routes"]:
            L.append(f"| {rt['path']} | {rt['matchers']} | {'是' if rt['continue'] else ''} | {rt['receiver']['value']} | {_v(rt['group_wait'])} | "
                     f"{_v(rt['group_interval'])} | {_v(rt['repeat_interval'])} |")
        L += ["", "Receiver：" + "、".join(f"{x['name']}{'（**空**）' if x['empty'] else ''}" for x in amo["receivers"]), ""]
        for ih in amo["inhibit"]:
            L.append(f"- inhibit：{ih['source']} 抑制 {ih['target']}，equal {ih['equal'] or '（無）'}；來源 rule：{', '.join(ih['source_rules']) or '—'}")
        if amo["unrouted_or_blackholed_rules"]:
            L.append("- 送到空 receiver 的 rule：" + ", ".join(f"{x['rule']}→{x['receiver']}" + ("" if x["certain"] else "（不確定）")
                                                           for x in amo["unrouted_or_blackholed_rules"]))
    else:
        L.append(f"- {_v(amo)}")
    L += ["", "## 其他問題（之後再問）", ""]
    L += [f"- {q['q']}（影響 {q['affects']} 項）" for q in inv["questions"] if not q["ask_now"]] or ["- （沒有）"]
    return "\n".join(L) + "\n"


# ---------- main ----------

HEADER = ("# env/<名稱>.inventory.yaml：由 tools/inventory.py 從檔案產生。可以直接改；改過的值會在下次 --merge 時保留\n"
          "# （加 confirmed: true 最保險）。from: file 檔案／default 官方預設／hint 目錄推測／user 使用者／unknown 未知。\n"
          "# 這份含環境資訊，env/*.yaml 不要 commit。\n")


class _NoAlias(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def dump(inv) -> str:
    return HEADER + yaml.dump(inv, Dumper=_NoAlias, allow_unicode=True, sort_keys=False, width=140)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rules", nargs="+", required=True)
    ap.add_argument("--config")
    ap.add_argument("--config-kind", choices=["prometheus", "vmagent"])
    ap.add_argument("--alertmanager")
    ap.add_argument("--answers")
    ap.add_argument("--merge", help="既有的 inventory：保留使用者改過或確認的值")
    ap.add_argument("--out", required=True)
    ap.add_argument("--md")
    a = ap.parse_args(argv)
    inv = build(a)
    if a.merge and os.path.exists(a.merge):
        old = C._load(a.merge) or {}
        same = (old.get("sources") or {}).get("sha256") == inv["sources"]["sha256"]
        inv = merge(inv, old, same)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(dump(inv))
    if a.md:
        os.makedirs(os.path.dirname(os.path.abspath(a.md)), exist_ok=True)
        with open(a.md, "w", encoding="utf-8") as f:
            f.write(markdown(inv))
    print(f"jobs {len(inv['jobs'])}  rules {len(inv['rules'])}  findings {len(inv['findings_from_files'])}  "
          f"questions {len(inv['questions'])}（先問 {sum(q['ask_now'] for q in inv['questions'])}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
