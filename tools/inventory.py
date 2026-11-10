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
  findings_from_files   facts only, no advice. Routing ones: route_label_missing (a route matcher needs a label the
                        alert cannot carry: not in rule labels / external_labels / static or relabel target labels,
                        and the expression drops it (certain) or only the exporter could add it (asked; the route
                        then keeps depends_on_runtime_labels)), route_label_value_mismatch (close names, e.g.
                        job="etcd" vs kube-etcd), critical_not_paging (critical rules whose only receivers are
                        email / empty), routed_to_empty_receiver (grouped), inhibit_no_equal, dms_route_without_rule
                        (an alertname="Watchdog" route but no such rule), no_watchdog_rule,
                        watchdog_no_dedicated_route (the Watchdog rule, or the one we propose, falls into a route without
                        an alertname matcher and inherits its timings; alertmanager.watchdog_route has the timings, the
                        DMS lower bound of contract deadman-wait and a route template with a valid placeholder URL),
                        rule_not_applicable (「這條 rule 不適用這個部署」: the rule assumes a feature — multi-target
                        /probe, heartbeat, semi-sync, Galera, Sentinel, Patroni ... (DEPLOY_NEEDS) — that the config
                        or answers.features / jobs[].features rule out, or groups by cluster / role that no target has)
  questions             ranked by how many findings / rules the answer changes; at most 3 ask_now

Answers file (env/<name>.sources.yaml) keys read here: job_names, producers, metrics, labels (as in
check_metric_sources.py), plus version (the ruler's), target_versions.<job> (the database / server version behind a
scrape job, e.g. mysql: "8.4.13"; check_coverage.py uses it for the cards' `versions`), ruler, external_observer,
measured.<job>.{d_p99,d_max,up0_count}, keep_range.<rule>, features.<job> (list of check_coverage.FEATURES that exist;
an edited jobs[].features in the --merge inventory counts too). The inventory is facts and questions; advice comes from the later tools.
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


# ---------- deployment mode: does a rule fit how this environment is deployed? ----------

# (feature in check_coverage.FEATURES, regex on metric names, regex on rule name + summary/description)
DEPLOY_NEEDS = [
    ("multi-target-probe", None, r"probe.?exporter|multi.?target|多 ?target|代理的所有"),
    ("heartbeat", r"heartbeat", r"heartbeat"),
    ("semi-sync", r"semi_?sync|rpl_semi", r"semi.?sync"),
    ("galera", r"wsrep_", r"galera"),
    ("group-replication", r"group_replication", r"group.?replication"),
    ("sentinel", r"^redis_sentinel_|^sentinel_", r"sentinel"),
    ("patroni", r"^patroni_", r"patroni"),
]
FEATURE_TEXT = {"multi-target-probe": "一個 exporter 代理多個 target（/probe、/scrape、__param_target）",
                "heartbeat": "heartbeat 表（pt-heartbeat 之類）", "semi-sync": "semi-sync replication",
                "galera": "Galera", "group-replication": "Group Replication", "sentinel": "Redis Sentinel",
                "patroni": "Patroni"}
DEPLOY_LABELS = ("cluster", "role")
_BY = re.compile(r"\b(?:by|on)\s*\(([^)]*)\)")


def deployment_needs(name: str, rule: dict, metrics: list[str], expr: str) -> list[dict]:
    """What the rule assumes about the deployment: features (from DEPLOY_NEEDS) and grouping labels
    (by / on (cluster|role))."""
    ann = rule.get("annotations") or {}
    text = " ".join([name or "", str(ann.get("summary", "")), str(ann.get("description", ""))])
    out = []
    for feat, mrx, nrx in DEPLOY_NEEDS:
        hit_m = [m for m in metrics if mrx and re.search(mrx, m or "", re.I)]
        if hit_m or (nrx and re.search(nrx, text, re.I)):
            out.append({"feature": feat, "basis": f"metric {', '.join(sorted(set(hit_m)))}" if hit_m else "rule 名稱／annotation"})
    grouped = {x.strip() for g in _BY.findall(expr) for x in g.split(",")}
    for l in DEPLOY_LABELS:
        if l in grouped:
            out.append({"label": l, "basis": f"依 `{l}` 分組（by／on）"})
    return out


def deployment_fit(needs: list[dict], jobs: list[dict], features_by_job: dict, ans_labels: dict | None = None,
                   labels_required: list | None = None) -> dict:
    """{applies: True|False|"unknown", reasons: [...], ask: [features]} for one rule.
    jobs: the config jobs the rule reads (load_scrape shape, or inventory jobs). features_by_job: job -> list of
    features the user says exist (missing job = not answered)."""
    reasons, ask = [], []
    ans_labels = ans_labels or {}
    for n in needs:
        if "feature" in n:
            f = n["feature"]
            names = [j["name"] for j in jobs]
            listed = [features_by_job[x] for x in names if isinstance(features_by_job.get(x), list)]
            if names and len(listed) == len(names):
                import check_coverage as CC
                if not any(f in CC.expand(fl) for fl in listed):
                    reasons.append({"basis": "feature", "feature": f, "from": "user",
                                    "why": f"answers 的 features 沒有 {f}（{FEATURE_TEXT.get(f, f)}）；這條 rule 假設有（{n['basis']}）"})
                continue
            if f == "multi-target-probe" and jobs:
                if all(not j.get("multi_target") for j in jobs):
                    reasons.append({"basis": "feature", "feature": f, "from": "file",
                                    "why": "job " + "、".join(names) + " 直接抓每台的 exporter（metrics_path "
                                           + "、".join(sorted({j.get('metrics_path') or '/metrics' for j in jobs}))
                                           + "，沒有 `__param_target` relabel）；這條 rule 假設一個 exporter 代理多個 target"})
                continue
            ask.append(f)
        else:
            l = n["label"]
            if l in ans_labels:
                if ans_labels[l].strip().lower() in MS.NO_LABEL:
                    reasons.append({"basis": "label", "label": l, "from": "user",
                                    "why": f"{n['basis']}，answers 說沒有 `{l}` label：所有 target 會被算成同一組"})
                continue
            if any(x.get("label") == l and x.get("status") == "missing" for x in labels_required or []):
                reasons.append({"basis": "label", "label": l, "from": "file",
                                "why": f"要 `{l}` label，config 沒有產生（見「要的 label」）"})
                continue
            if not jobs:
                continue
            tl = [j.get("target_labels") or {} for j in jobs]
            if any(l in (t.get("from_static") or {}) or l in (t.get("from_relabel") or []) or t.get("open") for t in tl):
                continue
            reasons.append({"basis": "label", "label": l, "from": "file",
                            "why": f"{n['basis']}，但 job {'、'.join(j['name'] for j in jobs)} 的 target 沒有 `{l}` label"
                                   f"（config 沒有加；除非 exporter 自己輸出）：所有 target 會被算成同一組"})
    for x in labels_required or []:
        if x.get("label") in DEPLOY_LABELS and x.get("status") == "missing" and \
                not any(r.get("label") == x["label"] for r in reasons):
            reasons.append({"basis": "label", "label": x["label"], "from": x.get("from") or "file",
                            "why": f"要 `{x['label']}{x['match']}`，config 沒有產生這個 label"})
    applies = False if reasons else ("unknown" if ask else True)
    return {"applies": applies, "reasons": reasons, "ask": sorted(set(ask))}


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
            # one exporter, many targets: /probe or /scrape path, or relabel that moves the target into a URL parameter
            "multi_target": sc.get("metrics_path", "/metrics") in ("/probe", "/scrape")
                            or any(str(r.get("target_label", "")).startswith("__param_") for r in relabel),
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
    return {"global": g, "gS": gS, "gT": gT, "gS_raw": gS_raw, "gT_raw": gT_raw, "jobs": jobs, "rule_files": doc.get("rule_files"),
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


# ---------- labels on the alert's result (for routing) ----------

_AGG_NAMES = {"sum", "min", "max", "avg", "count", "group", "stddev", "stdvar", "quantile", "count_values"}
_KEEP_ALL_AGG = {"topk", "bottomk", "limitk", "limit_ratio"}  # keep the input series' labels


def _close_paren(s: str, i: int) -> int:
    """Index of the ')' matching the '(' at s[i] (skips strings)."""
    depth, q = 0, None
    for j in range(i, len(s)):
        c = s[j]
        if q:
            if c == "\\":
                continue
            if c == q:
                q = None
            continue
        if c in "\"'`":
            q = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j
    return len(s) - 1


def _split_args(s: str) -> list[str]:
    out, depth, cur, q = [], 0, "", None
    for c in s:
        if q:
            cur += c
            if c == q:
                q = None
            continue
        if c in "\"'`":
            q = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += c
    if cur.strip():
        out.append(cur)
    return [x.strip() for x in out]


def _depth0_split(expr: str, word: str) -> list[str]:
    """Split at a top-level binary keyword (or / and / unless)."""
    parts, depth, last, q = [], 0, 0, None
    i = 0
    while i < len(expr):
        c = expr[i]
        if q:
            if c == q:
                q = None
        elif c in "\"'`":
            q = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and re.match(rf"\b{word}\b", expr[i:]) and (i == 0 or not (expr[i - 1].isalnum() or expr[i - 1] == "_")):
            parts.append(expr[last:i])
            i += len(word)
            last = i
            continue
        i += 1
    parts.append(expr[last:])
    return [p.strip() for p in parts if p.strip()]


def result_label(expr: str, label: str, _depth: int = 0) -> tuple[str, str | None]:
    """Does the alert's result series carry `label`?  ("absent", None): the expression drops it (aggregation
    without it in `by`, absent() without an equality matcher on it, vector()); ("value", v): it is set to v
    (absent() with label="v"); ("made", None): label_replace / count_values / group_left write it; ("series", None):
    it is kept from the input series (only the series' own labels can say). The left operand decides, as in
    PromQL one-to-one matching; `or` keeps either side."""
    e = expr.strip()
    if _depth > 20 or not e:
        return "series", None
    gl = re.search(r"\bgroup_(left|right)\s*\(([^)]*)\)", e)
    if gl and label in [x.strip() for x in gl.group(2).split(",")]:
        return "made", None
    ors = _depth0_split(e, "or")
    if len(ors) > 1:
        res = [result_label(p, label, _depth + 1) for p in ors]
        kinds = {r[0] for r in res}
        if "made" in kinds:
            return "made", None
        if kinds == {"absent"}:
            return "absent", None
        if kinds == {"value"} and len({r[1] for r in res}) == 1:
            return res[0]
        return "series", None  # either side may carry it
    while e.startswith("(") and _close_paren(e, 0) == len(e) - 1:
        e = e[1:-1].strip()
    if e.startswith("("):
        return result_label(e[1:_close_paren(e, 0)], label, _depth + 1)
    m = re.match(r"([A-Za-z_][A-Za-z0-9_:]*)\s*", e)
    if not m:
        sc = re.match(r"[-+]?[0-9.]+(?:e[-+]?\d+)?\s*(?:==|!=|>=|<=|[-+*/%^<>])\s*(?:bool\s+)?(.+)$", e, re.S)
        if sc:
            return result_label(sc.group(1), label, _depth + 1)  # 1 - x: labels of x
        if re.match(r"[-+]?[0-9.]", e):
            return "absent", None  # scalar literal
        return "series", None
    word, rest_i = m.group(1), m.end()
    low = word.lower()
    if low in _AGG_NAMES | _KEEP_ALL_AGG:
        grouping, glist = None, []
        g = re.match(r"(by|without)\s*\(([^)]*)\)\s*", e[rest_i:], re.I)
        if g:
            grouping, glist = g.group(1).lower(), [x.strip() for x in g.group(2).split(",") if x.strip()]
            rest_i += g.end()
        if rest_i >= len(e) or e[rest_i] != "(":
            return "series", None
        close = _close_paren(e, rest_i)
        args = _split_args(e[rest_i + 1:close])
        if grouping is None:
            g = re.match(r"\s*(by|without)\s*\(([^)]*)\)", e[close + 1:], re.I)
            if g:
                grouping, glist = g.group(1).lower(), [x.strip() for x in g.group(2).split(",") if x.strip()]
        inner = args[-1] if args else ""
        if low == "count_values" and args and args[0].strip("\"'`") == label:
            return "made", None
        if low in _KEEP_ALL_AGG and grouping is None:
            return result_label(inner, label, _depth + 1)
        if grouping == "by":
            return result_label(inner, label, _depth + 1) if label in glist else ("absent", None)
        if grouping == "without":
            return ("absent", None) if label in glist else result_label(inner, label, _depth + 1)
        if low in _KEEP_ALL_AGG:
            return result_label(inner, label, _depth + 1)
        return "absent", None
    if rest_i < len(e) and e[rest_i] == "(":
        close = _close_paren(e, rest_i)
        args = _split_args(e[rest_i + 1:close])
        if low in ("absent", "absent_over_time"):
            for s in MS.selectors(args[0] if args else ""):
                for (l, op, v) in s["matchers"]:
                    if l == label and op == "=":
                        return "value", v
            return "absent", None
        if low in ("vector", "time", "scalar", "pi"):
            return "absent", None
        if low in ("label_replace", "label_join"):
            if len(args) > 1 and args[1].strip("\"'`") == label:
                return "made", None
            return result_label(args[0] if args else "", label, _depth + 1)
        if low == "histogram_quantile" and len(args) > 1:
            return result_label(args[1], label, _depth + 1)
        for arg in args:
            if not re.fullmatch(r"[-+]?[0-9.e]+|\"[^\"]*\"|'[^']*'", arg):
                return result_label(arg, label, _depth + 1)
        return "absent", None
    return "series", None  # a vector selector: the series' own labels


def route_label_gaps(e: dict, expr: str, tree: dict, by_job: dict, cfg_jobs: list, matched: set, gaps: dict,
                     ans_labels: dict | None = None) -> list[str]:
    """Labels a route matcher needs that the alert does not carry (rule labels, external_labels and target labels
    checked first). A label the expression drops (aggregation, absent(), up/scrape_* without it) is set to absent
    in e['_lab'] and recorded in gaps[(label, 'absent')]; returns the labels only the exporter could still add
    (not aggregated, the matched jobs add no such label), which stay runtime-dependent."""
    lab = e["_lab"]
    ans_labels = ans_labels or {}
    cands: dict[str, list] = {}
    for n in tree["nodes"]:
        ms = n["matchers"]
        for (l, op, v) in ms:
            if l == "alertname" or (l in lab and l != "__other__") or fullmatch(op, v, ""):
                continue
            if eval_matchers([m for m in ms if m[0] != l], lab)[0] == "no":
                continue  # this route is not for the alert anyway
            cands.setdefault(l, []).append((op, v))
    cond = []
    for L in sorted(cands):
        if L in ALERT_LEVEL_LABELS:
            lab[L] = ""  # set by the rule's labels or not at all: a rule without it simply does not take this route
            continue
        ans = ans_labels.get(L)
        if ans is not None:
            if ans.strip().lower() in MS.NO_LABEL:
                lab[L] = ""
                gaps.setdefault((L, "absent"), {})[e["name"]] = None
            else:
                lab[L] = None
            continue
        kind, val = result_label(expr, L)
        if kind == "value":
            lab[L] = val
            continue
        if kind == "made":
            lab[L] = None
            continue
        if "__other__" in lab:
            gaps.setdefault((L, "absent"), {})[e["name"]] = None  # up / scrape_*: only target labels
            continue
        if kind == "series" and L == "job":  # every scraped series has it: the matched job names
            lab[L] = set(matched) if matched else None
            continue
        if kind == "series" and L == "instance":
            lab[L] = None
            continue
        sels = [s.get("metric") or "" for s in MS.selectors(expr)]
        if kind == "series" and sels and all(m == "up" or m.startswith("scrape_") for m in sels):
            kind = "absent"  # up / scrape_* carry only target labels (checked below would say the same)
            js = [by_job[j] for j in matched if j in by_job] if matched else list(cfg_jobs)
            if not js or any(L in j["target_labels"]["from_static"] or L in j["target_labels"]["from_relabel"]
                             or j["target_labels"]["open"] or j["sd"] != ["static_configs"] for j in js):
                lab[L] = None
                continue
        if kind == "absent":
            lab[L] = ""
            js = [by_job[j] for j in matched if j in by_job]
            static = {x for j in js for x in j["target_labels"]["from_static"].get(L, [])}
            if static and not any(fullmatch(op, v, x) for op, v in cands[L] for x in static):
                continue  # the series carry another value (cluster=main vs a route for cluster=other): not meant for it
            gaps.setdefault((L, "absent"), {})[e["name"]] = None
            continue
        js = [by_job[j] for j in matched if j in by_job] if matched else list(cfg_jobs)
        if not js:
            continue  # no config for these jobs: only the series can tell
        if any(L in j["target_labels"]["from_static"] or L in j["target_labels"]["from_relabel"] or j["target_labels"]["open"]
               or j["sd"] != ["static_configs"] for j in js):
            continue
        cond.append(L)
    return cond


DMS_ROUTE_TEMPLATE = """route:
  routes:
    - matchers: ['alertname="Watchdog"']
      receiver: deadmans-switch
      group_wait: 0s
      group_interval: 1m
      repeat_interval: 1m
      continue: false
receivers:
  - name: deadmans-switch
    webhook_configs:
      - url: https://dms.example.invalid/ping"""


def watchdog_route(tree: dict, rules_out: list, stack: dict, ruler: dict) -> dict:
    """Where the Watchdog alert (the rule in the files, or the one we propose) is routed, its timings, whether the
    route is dedicated (a child route with an alertname matcher), and the DMS lower bound from contract deadman-wait:
    4 × max(resend, E) + repeat_interval + group_interval."""
    wd = next((e for e in rules_out if e["kind"] == "alert" and WATCHDOG_NAME.search(e["name"] or "")), None)
    if wd is not None and isinstance(wd.get("route"), dict) and wd["route"].get("matches"):
        rt = wd["route"]
    else:
        ext = IU_val(stack.get("external_labels")) or {}
        rt = route_for(tree, dict({k: v for k, v in ext.items()}, alertname="Watchdog", severity="none", __other__=""))
    by_path = {n["path"]: n for n in tree["nodes"]}
    m = next((x for x in rt["matches"] if x["certain"]), rt["matches"][0])
    node = by_path[m["path"]]
    dedicated = m["path"] != "route" and any(l == "alertname" for l, _, _ in node["matchers"])
    E = dur(IU_val(stack.get("evaluation_interval"))) or 60.0
    resend = 60.0 if ruler.get("value") == "prometheus" else 0.0
    rep_i, gi = dur(m["repeat_interval"]) or 0.0, dur(m["group_interval"]) or 0.0
    wait = 4 * max(resend, E) + rep_i + gi
    return {"rule": wd["name"] if wd else None, "proposed": wd is None, "path": m["path"], "receiver": m["receiver"],
            "dedicated": dedicated, "group_wait": m["group_wait"], "group_interval": m["group_interval"],
            "repeat_interval": m["repeat_interval"], "dms_min_wait": fmt(wait), "dms_min_wait_seconds": wait,
            "basis": f"4 × max(resend {fmt(resend)}, E {fmt(E)}) + repeat_interval {fmt(rep_i)} + group_interval {fmt(gi)}，推算",
            "contract": "deadman-wait", "template": None if dedicated else DMS_ROUTE_TEMPLATE}


def IU_val(node):
    if isinstance(node, dict):
        v = node.get("value")
        return None if v in (None, "unknown") else v
    return node


def _sev_critical(v) -> bool:
    return isinstance(v, str) and v.lower() in ("critical", "page", "p1", "sev1")


EMAIL_LIKE = {"email_configs"}
# labels only an alerting rule's `labels` set (never the series): a rule without one just does not take that route
ALERT_LEVEL_LABELS = {"severity", "priority"}
WATCHDOG_NAME = re.compile(r"watchdog|dead.?man|dms\b", re.I)


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
        stack["scrape_interval"] = V(fmt(cfg["gS"]), "file" if cfg["gS_raw"] else "default")
        stack["scrape_timeout"] = V(fmt(cfg["gT"]), "file" if cfg["gT_raw"] else "default")
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
    tv = answers.get("target_versions")
    if isinstance(tv, dict) and tv:
        stack["target_versions"] = V({str(k): str(v) for k, v in tv.items() if v is not None}, "user",
                                     note="answers.target_versions：scrape job → 資料庫版本")
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
    old_inv = (C._load(a.merge) or {}) if getattr(a, "merge", None) and os.path.exists(a.merge) else {}
    old_feats = {j["name"]: j["features"] for j in old_inv.get("jobs") or []
                 if isinstance(j, dict) and isinstance(j.get("features"), dict) and isinstance(j["features"].get("value"), list)}

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
            out.update({k: j[k] for k in ("interval", "timeout", "targets", "sd", "metrics_path", "multi_target",
                                          "target_labels", "dropped_metrics")})
            if j["static_targets"]:
                out["static_targets"] = j["static_targets"]
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
                out["exporter"] = V(by_port[0], "hint", basis="預設 port，且 rule 讀的 metric 在這張目錄卡片"
                                    + MS.cards_note(by_port[0], C.ROOT))
            elif votes:
                best = max(sorted(votes), key=lambda c: votes[c])
                out["exporter"] = V(best, "hint", basis=f"rule 讀這個 job 的 {votes[best]} 個 metric 在目錄卡片 {best}"
                                    f"{MS.cards_note(best, C.ROOT)}")
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
        fe = (answers.get("features") or {}).get(name)
        if isinstance(fe, list):
            out["features"] = V([str(x).strip().lower() for x in fe], "user",
                                note="answers.features：這個 job 的 target「有」的功能（沒列的當成沒有）")
        elif name in old_feats:
            out["features"] = dict(old_feats[name], **{"from": "user"})
        else:
            out["features"] = V(None, "unknown", note="這個 job 的 target 有哪些功能（check_coverage.py 的 FEATURES，"
                                                     "例如 heartbeat、semi-sync、multi-target-probe）；填清單後 --merge 重跑")
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
    deploy_bad: dict[tuple, list] = {}
    deploy_ask: dict[str, list] = {}
    features_by_job = {j["name"]: j["features"]["value"] for j in jobs_out if isinstance(j.get("features"), dict)}
    route_gaps: dict[tuple, dict] = {}
    route_values: dict[tuple, dict] = {}
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
        needs = deployment_needs(name, r, [s["metric"] for s in sels if s["metric"]], r_expr)
        if needs:
            djobs = [by_job[j] for j in sorted(rule_jobs_used.get(name, ())) if j in by_job]
            assumed = None
            if not djobs and by_job:  # job matcher fits no job: judge the deployment as if it named the closest one
                close = [c for op, v in e.get("_jm", []) if op == "="
                         for c in difflib.get_close_matches(v, sorted(by_job), n=1, cutoff=0.6)]
                if close:
                    djobs, assumed = [by_job[close[0]]], close[0]
            fit = deployment_fit(needs, djobs, features_by_job, ans_labels, e["labels_required"])
            if assumed:
                for x in fit["reasons"]:
                    x["why"] += f"（假設 rule 的 job 改成 `{assumed}`）"
            e["deployment"] = {"needs": needs, "applies": fit["applies"], "reasons": fit["reasons"], "ask": fit["ask"]}
            for x in fit["reasons"]:
                deploy_bad.setdefault((x["basis"], x.get("feature") or x.get("label"), x["why"]), []).append(name)
            for f in fit["ask"]:
                deploy_ask.setdefault(f, []).append(name)
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
                cond = route_label_gaps(e, r_expr, tree, by_job, cfg_jobs, rule_jobs_used.get(name, set()), route_gaps,
                                        ans_labels)
                e["route"] = route_for(tree, e["_lab"])
                if cond:
                    alt = route_for(tree, dict(e["_lab"], **{l: "" for l in cond}))
                    for l in cond:
                        route_gaps.setdefault((l, "exporter"), {}).setdefault(name, alt["receivers"])
                for (l, kind), rs in route_gaps.items():
                    if kind == "absent" and name in rs:
                        rs[name] = e["route"]["receivers"]
                for n in tree["nodes"][1:]:
                    res = [(m, eval_matchers([m], e["_lab"])[0]) for m in n["matchers"]]
                    no = [m for m, x in res if x == "no"]
                    if len(no) != 1 or len(res) < 2 or any(x != "yes" for m, x in res if m is not no[0]) \
                            or no[0][0] == "alertname" or no[0][0] in ALERT_LEVEL_LABELS:
                        continue
                    l = no[0][0]
                    v = e["_lab"].get(l, e["_lab"].get("__other__"))
                    if v in (None, "") or n["path"] in {m["path"] for m in e["route"]["matches"]} \
                            or n["eff"]["receiver"]["value"] in e["route"]["receivers"]:
                        continue
                    want = next(x for (ll, op, x) in n["matchers"] if ll == l)
                    vs = [v] if isinstance(v, str) else sorted(v)
                    if not any(want.lower() in x.lower() or x.lower() in want.lower()
                               or difflib.SequenceMatcher(None, want, x).ratio() >= 0.6 for x in vs):
                        continue  # a different value on purpose (another cluster / team): normal routing
                    shown = "／".join(vs)
                    route_values.setdefault((n["path"], l), {})[name] = (shown, e["route"]["receivers"])
            else:
                e["route"] = V(None, "unknown", note="沒有 alertmanager.yml")
    inv["rules"] = rules_out
    for (basis, what, why), names in deploy_bad.items():
        findings.append({"kind": "rule_not_applicable", "basis": basis, basis: what, "rules": names, "reason": why,
                         "fact": f"這條 rule 不適用這個部署（{len(names)} 條：{', '.join(names[:8])}{' 等' if len(names) > 8 else ''}）："
                                 f"{why}"})
    inv["_deploy_ask"] = deploy_ask
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
        unr, to_empty = [], {}
        for e in alerts:
            rt = e["route"]
            hit = [o for o in rt["matches"] if o["receiver"] in empty]
            if hit:
                certain = any(o["certain"] for o in hit) and not rt.get("depends_on_runtime_labels")
                unr.append({"rule": e["name"], "receiver": hit[0]["receiver"], "certain": certain,
                            "severity": e["labels_out"].get("severity")})
                if certain or len(rt["receivers"]) == 1:
                    to_empty.setdefault((hit[0]["receiver"], certain), []).append(e)
            if rt["receivers"] and any(x not in receivers for x in rt["receivers"]):
                findings.append({"kind": "receiver_undefined", "rule": e["name"],
                                 "fact": f"{e['name']} 路由到的 receiver {rt['receivers']} 不在 receivers 清單"})
        amo["unrouted_or_blackholed_rules"] = unr
        for (rcv, certain), es in to_empty.items():
            names = [x["name"] for x in es]
            crit = [x["name"] for x in es if _sev_critical(x["labels_out"].get("severity"))]
            findings.append({"kind": "routed_to_empty_receiver", "rules": names, "receiver": rcv, "certain": certain,
                             "fact": f"{len(names)} 條 rule 會送到空的 receiver {rcv}" + ("" if certain else "（依 runtime label 而定）")
                                     + (f"，其中 critical {len(crit)} 條" if crit else "")
                                     + f"：{', '.join(names[:8])}{' 等' if len(names) > 8 else ''}"})
        # route labels no rule carries
        for (l, kind), rs in sorted(route_gaps.items()):
            names = sorted(rs)
            recv = sorted({x for v in rs.values() for x in (v or [])})
            need = "；".join(f"{n['path']}：{mtext(n['matchers'])} → {n['eff']['receiver']['value']}"
                            for n in nodes if any(m[0] == l for m in n["matchers"]))
            cert = kind == "absent"
            fact = (f"route 要求 `{l}`，這些 rule 都沒有" + ("" if cert else f"（除非 exporter 自己輸出 `{l}`）")
                    + f"，實際會送到 {'、'.join(recv) or '—'}：{', '.join(names[:8])}{' 等' if len(names) > 8 else ''}"
                    f"（{need}；" + ("聚合、absent() 或 up 拿掉了這個 label，" if cert else "rule 沒有聚合，")
                    + "rule labels、external_labels、static／relabel target label 都沒有）")
            findings.append({"kind": "route_label_missing", "label": l, "certain": cert, "rules": names,
                             "fallback": recv, "fact": fact})
        for (path, l), rs in sorted(route_values.items()):
            n = next(x for x in nodes if x["path"] == path)
            names = sorted(rs)
            vals = sorted({v for v, _ in rs.values()})
            recv = sorted({x for _, rv in rs.values() for x in rv})
            findings.append({"kind": "route_label_value_mismatch", "label": l, "route": path, "values": vals, "rules": names,
                             "fallback": recv, "receiver": n["eff"]["receiver"]["value"],
                             "fact": f"route（{mtext(n['matchers'])} → {n['eff']['receiver']['value']}）要 {l}{next(op + chr(34) + v + chr(34) for (x, op, v) in n['matchers'] if x == l)}，"
                                     f"這些 rule 的 {l} 是 {'、'.join(vals)}（名稱相近，像是寫錯），實際會送到 {'、'.join(recv)}：{', '.join(names[:8])}{' 等' if len(names) > 8 else ''}"})
        # critical alerts that only reach email / empty receivers
        by_kind = {x["name"]: x for x in recv_out}
        nopage: dict[tuple, list] = {}
        for e in alerts:
            if not _sev_critical(e["labels_out"].get("severity")):
                continue
            rv = e["route"]["receivers"]
            if rv and all(x in by_kind and (by_kind[x]["empty"] or set(by_kind[x]["kinds"]) <= EMAIL_LIKE) for x in rv):
                nopage.setdefault(tuple(rv), []).append(e["name"])
        for rv, names in nopage.items():
            what = "、".join(f"{x}（{'空' if by_kind[x]['empty'] else 'email'}）" for x in rv)
            findings.append({"kind": "critical_not_paging", "rules": names, "receivers": list(rv),
                             "fact": f"{len(names)} 條 severity critical 的 rule 只會送到 {what}，不會 page："
                                     f"{', '.join(names[:8])}{' 等' if len(names) > 8 else ''}"})
        # Watchdog / dead man's switch route without a Watchdog rule
        alert_names = [e["name"] for e in rules_out if e["kind"] == "alert"]
        for n in nodes:
            wm = [(op, v) for (l, op, v) in n["matchers"] if l == "alertname" and op in ("=", "=~")]
            recv = n["eff"]["receiver"]["value"] or ""
            if not wm or not any(fullmatch(op, v, "Watchdog") or WATCHDOG_NAME.search(v) for op, v in wm) and \
                    not WATCHDOG_NAME.search(recv):
                continue
            if not any(all(fullmatch(op, v, a) for op, v in wm) for a in alert_names):
                findings.append({"kind": "dms_route_without_rule", "route": n["path"], "receiver": recv,
                                 "fact": f"alertmanager 有給 {mtext(n['matchers'])} 的 route（→ {recv}），但給的 rule 檔沒有這條 rule："
                                         "dead man's switch 收不到心跳（Watchdog 可能在別的檔案）"})
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
            if not equal:
                tnames = [e["name"] for e in alerts if eval_matchers(tgt, single(e["_lab"]))[0] != "no"
                          and e["name"] not in srules]
                findings.append({"kind": "inhibit_no_equal", "source": mtext(src), "target": mtext(tgt),
                                 "source_rules": srules, "target_rules": tnames,
                                 "fact": f"inhibit（{mtext(src)} 抑制 {mtext(tgt)}）沒有 `equal`：任何一台的 source alert 在響時，"
                                         f"所有 instance／叢集符合 target 的 alert 都被抑制"
                                         + (f"（source rule：{', '.join(srules[:5])}；可能被抑制：{len(tnames)} 條"
                                            + (f"，例如 {', '.join(tnames[:5])}" if tnames else "") + "）" if srules or tnames else "")})
            inh_out.append({"source": mtext(src), "target": mtext(tgt), "equal": equal, "source_rules": srules,
                            "suppressed": pairs})
        amo["inhibit"] = inh_out
        amo["ends_at_inputs"] = {"evaluation_interval": (stack.get("evaluation_interval") or {}).get("value"),
                                 "resend_delay": V("1m", "default") if ruler["value"] == "prometheus" else V(None, "unknown"),
                                 "contract": "resend-endsat",
                                 "note": "只記錄數值；EndsAt 與 repeat_interval 的關係見 docs/contracts.md#resend-endsat"}
        amo["watchdog_route"] = watchdog_route(tree, rules_out, stack, ruler)
        wr = amo["watchdog_route"]
        if not wr["dedicated"]:
            who = f"Watchdog rule `{wr['rule']}`" if wr["rule"] else "建議新增的 Watchdog"
            findings.append({"kind": "watchdog_no_dedicated_route", "route": wr["path"], "receiver": wr["receiver"],
                             "fact": f"{who} 在 Alertmanager 沒有專用的 route：會落到 {wr['path']}（→ {wr['receiver']}），"
                                     f"繼承 `group_wait` {wr['group_wait']}、`group_interval` {wr['group_interval']}、"
                                     f"`repeat_interval` {wr['repeat_interval']}；dead man's switch 依契約 deadman-wait "
                                     f"至少要等 {wr['dms_min_wait']} 才能判斷 ruler 停了（{wr['basis']}）"})
        inv["alertmanager"] = amo
    else:
        inv["alertmanager"] = V(None, "unknown", note="沒有 alertmanager.yml")

    for e in rules_out:
        e.pop("_lab", None)

    alerts_all = [e["name"] for e in rules_out if e["kind"] == "alert"]
    if alerts_all and not any(WATCHDOG_NAME.search(n or "") for n in alerts_all):
        dms = am is not None and any(f["kind"] == "dms_route_without_rule" for f in findings)
        findings.append({"kind": "no_watchdog_rule",
                         "fact": "給的 rule 檔沒有 Watchdog（一直在響、送到外部 dead man's switch 的 rule）"
                                 + ("" if dms else "；alertmanager.yml 也沒有給 Watchdog 的 route" if am is not None else "")
                                 + "：ruler 或 Alertmanager 停掉時，沒有任何 alert 會說出來"})

    # dedupe findings, drop internal keys
    seen, fl = set(), []
    for f in findings:
        if f["fact"] in seen:
            continue
        seen.add(f["fact"])
        fl.append(f)
    inv["findings_from_files"] = fl
    inv["questions"] = questions(inv, cfg, am, a, fl)
    inv.pop("_deploy_ask", None)
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
        api = ("`curl -s <vmalert>/metrics | grep vm_app_version`" if rk == "vmalert" else
               "`curl -s <prometheus>/api/v1/status/buildinfo` 與 `curl -s <prometheus>/api/v1/status/flags`")
        qs.append({"q": f"評估這些 rule 的 {rk} 是哪個版本？請直接貼 {api} 的輸出（唯讀；版本與旗標一次確認）",
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
        sel = "|".join(unmeasured)
        qs.append({"q": f"{', '.join(unmeasured)} 的 scrape 耗時 p99／最大值與 up==0 次數（7 天）：請貼這三個查詢的輸出 "
                        f"`max by (job) (quantile_over_time(0.99, scrape_duration_seconds{{job=~\"{sel}\"}}[7d]))`、"
                        f"`max by (job) (max_over_time(scrape_duration_seconds{{job=~\"{sel}\"}}[7d]))`、"
                        f"`sum by (job) (count_over_time((up{{job=~\"{sel}\"}} == 0)[7d:]))`",
                   "changes": ["scrape_timeout", "range 視窗", "k"], "affects": len(ranged),
                   "findings": cnt("timeout_gt_interval", "exporter_deadline_ge_timeout"), "answer_key": "measured"})
    short = [r["name"] for r in ranged if any(g.get("short") for g in r["ranges"]) and r["keep_range"]["from"] == "unknown"]
    if short:
        qs.append({"q": f"{', '.join(short[:5])}{' 等' if len(short) > 5 else ''} 的短視窗是刻意的嗎？",
                   "changes": ["方案 A（改 rule）能不能用"], "affects": len(short), "answer_key": "keep_range"})
    hinted = [j for j in inv["jobs"] if j["exporter"]["from"] == "hint"]
    if hinted:
        n = sum(1 for r in rules if set(r["jobs_matched"].get("value") or []) & {j["name"] for j in hinted})
        sel = "|".join(j["name"] for j in hinted)
        qs.append({"q": "確認 exporter：" + "、".join(f"{j['name']} = {j['exporter']['value']}？" for j in hinted)
                        + f"請貼 `group by (job, __name__, version) ({{__name__=~\".+_build_info\", job=~\"{sel}\"}})` 的輸出"
                          "（一次確認 exporter 與版本；參數另外在 inventory.yaml 填）",
                   "changes": ["exporter 期限", "metric 來源", "目錄卡片"], "affects": n,
                   "findings": cnt("exporter_deadline_ge_timeout"), "answer_key": "producers"})
    for f in [f for f in findings if f["kind"] == "route_label_missing"]:
        sib = [x for x in findings if x["kind"] == "route_label_missing" and x["label"] == f["label"]]
        if f is not sib[0]:
            continue
        sib = [x for x in sib if not x["certain"]]
        if not sib:
            continue  # the expression drops the label: nothing to ask, the fix is in the rule or the route
        names = sorted({n for x in sib for n in x["rules"]})
        recv = sorted({n for x in sib for n in x["fallback"]})
        qs.append({"q": f"route 要 `{f['label']}` label：exporter 會輸出 `{f['label']}` 嗎（沒有聚合的 rule 才用得到）？"
                        f"還是要在 rule 的 labels 或 relabel 加？沒有的話 {len(names)} 條 rule 會送到 {'、'.join(recv)}",
                   "changes": ["每條 rule 送到哪個 receiver", "critical 會不會 page"], "affects": len(names),
                   "findings": len(sib) + cnt("critical_not_paging"), "answer_key": f"labels.{f['label']}"})
    da = inv.get("_deploy_ask") or {}
    if da:
        names = sorted({n for v in da.values() for n in v})
        feats = "、".join(f"{f}（{FEATURE_TEXT.get(f, f)}）" for f in sorted(da))
        qs.append({"q": f"這個部署有沒有：{feats}？在 inventory.yaml 的 jobs[].features（或 answers 的 `features.<job>: [...]`）"
                        "列出有的功能，一次填完",
                   "changes": ["這條 rule 適不適用這個部署"], "affects": len(names), "findings": 0, "answer_key": "features"})
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
             "external_labels": "`external_labels`", "rule_query_offset": "`rule_query_offset`",
             "scrape_interval": "global `scrape_interval`", "scrape_timeout": "global `scrape_timeout`",
             "target_versions": "資料庫版本（依 job）"}
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
