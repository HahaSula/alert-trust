"""Read env/<name>.inventory.yaml (tools/inventory.py) into the shapes the later tools use.

check_metric_sources.py, fit_contracts.py and check_coverage.py take `--inventory`; with it they read jobs, stack,
rules, unscraped_components and alertmanager from the inventory instead of parsing the config again.
Precedence, highest first: a flag given on the command line, a value marked `from: user` / `confirmed: true` in
the inventory, a config passed with --config, the other inventory values (file / default / hint).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog as C  # noqa: E402


def load(path: str) -> dict:
    inv = C._load(path) or {}
    inv.setdefault("jobs", [])
    inv.setdefault("rules", [])
    inv.setdefault("stack", {})
    inv["_path"] = path
    return inv


def is_user(node) -> bool:
    return isinstance(node, dict) and (node.get("confirmed") is True or node.get("from") == "user")


def val(node, default=None):
    """The value of a {value, from} node (None / 'unknown' -> default)."""
    if isinstance(node, dict) and "value" in node:
        v = node["value"]
        return default if v is None or v == "unknown" else v
    return default if node is None else node


def known(node) -> bool:
    return isinstance(node, dict) and node.get("from") not in (None, "unknown") and node.get("value") not in (None, "unknown")


def config_jobs(inv: dict) -> list[dict]:
    """Jobs read from a scrape config (not the job names guessed from the rules when there was no config)."""
    return [j for j in inv.get("jobs") or [] if not j.get("pattern") and "interval" in j and j.get("from") is None]


def has_config(inv: dict) -> bool:
    return bool((inv.get("sources") or {}).get("config"))


def job_names(inv: dict) -> set[str]:
    """Every job value the inventory knows: config jobs, answered job names, jobs a rule matched."""
    out = {j["name"] for j in config_jobs(inv)} if has_config(inv) else \
        {j["name"] for j in inv.get("jobs") or [] if j.get("from") == "user"}
    for r in inv.get("rules") or []:
        jm = r.get("jobs_matched") or {}
        if jm.get("from") in ("file", "user"):
            out |= set(jm.get("value") or [])
    return out


def answers(inv: dict, given: dict | None = None) -> dict:
    """The answers the inventory was built with (sources.answers, when it still exists), plus the user's own
    edits in the inventory (exporter from user → producers, label answers, job names, version, target_versions).
    `given` (--answers) wins."""
    out: dict = {}
    src = (inv.get("sources") or {}).get("answers")
    if src and os.path.exists(src):
        out = dict(C._load(src) or {})
    prod = {k: list(v) for k, v in (out.get("producers") or {}).items()}
    for j in inv.get("jobs") or []:
        ex = j.get("exporter") or {}
        if is_user(ex) and ex.get("value"):
            js = prod.setdefault(str(ex["value"]), [])
            if j["name"] not in js:
                js.append(j["name"])
    if prod:
        out["producers"] = prod
    labels = dict(out.get("labels") or {})
    for r in inv.get("rules") or []:
        for lr in r.get("labels_required") or []:
            if lr.get("status") == "answered" and lr.get("answer") is not None:
                labels.setdefault(lr["label"], lr["answer"])
    if labels:
        out["labels"] = labels
    if not has_config(inv):
        names = [j["name"] for j in inv.get("jobs") or [] if j.get("from") == "user" and not j.get("pattern")]
        if names:
            out["job_names"] = sorted(set(out.get("job_names") or []) | set(names))
    st = inv.get("stack") or {}
    if is_user(st.get("version")) and val(st.get("version")) is not None:
        out.setdefault("version", val(st["version"]))
    if is_user(st.get("target_versions")) and isinstance(val(st.get("target_versions")), dict):
        tv = dict(val(st["target_versions"]))
        tv.update(out.get("target_versions") or {})
        out["target_versions"] = tv
    for k, v in (given or {}).items():
        out[k] = v
    return out


def target_versions(inv: dict) -> dict[str, str]:
    """job -> database / server version the user gave (answers.target_versions, kept in stack.target_versions)."""
    node = (inv.get("stack") or {}).get("target_versions")
    v = val(node)
    return {str(k): str(x) for k, x in v.items()} if isinstance(v, dict) else {}


def ms_jobs(inv: dict) -> list[dict]:
    """Scrape jobs in the shape check_metric_sources.load_config returns."""
    out = []
    for j in config_jobs(inv) if has_config(inv) else []:
        tl = j.get("target_labels") or {}
        labels = {"job", "instance"} | set(tl.get("from_static") or {}) | set(tl.get("from_relabel") or [])
        relabel = [{"source_labels": ["__name__"], "regex": rx, "action": "drop"} for rx in j.get("dropped_metrics") or []]
        relabel += [{"source_labels": ["__name__"], "regex": rx, "action": "keep"} for rx in j.get("kept_only_metrics") or []]
        notes = []
        if "job" in (tl.get("from_relabel") or []):
            notes.append("relabel_configs 會改寫 job label")
        extra = {"static_targets": list(j["static_targets"])} if j.get("static_targets") else {}
        out.append({**extra, "scrape_interval": val(j.get("interval")), "scrape_timeout": val(j.get("timeout")),
                    "target_labels": sorted(l for l in labels if not l.startswith("__")),
                    "open_labels": list(tl.get("open") or []), "job": j["name"], "metrics_path": j.get("metrics_path", "/metrics"),
                    "module": None, "sd": j.get("sd") or ["static_configs"], "honor_labels": bool(tl.get("honor_labels")),
                    "federate_match": None, "metric_relabel": relabel, "notes": notes + ["inventory"],
                    "rewrites_job": bool(j.get("rewrites_job"))})
    return out


def rule(inv: dict, name: str) -> dict | None:
    return next((r for r in inv.get("rules") or [] if r.get("name") == name), None)


def nodata_rules(inv: dict) -> dict[str, str]:
    """rule -> why, for rules the inventory says never fire / always fire (no data)."""
    out = {}
    for r in inv.get("rules") or []:
        f = r.get("fires") or {}
        if f.get("value") in ("never", "always"):
            out[r["name"]] = f"inventory：fires {f['value']}（{', '.join(f.get('why') or [])}）"
    return out


TRUE_WORDS = ("true", "yes", "1", "是", "刻意", "keep")
FALSE_WORDS = ("false", "no", "0", "否", "不是", "change")


def keep_state(v) -> bool | None:
    """keep_range answer -> True (intentional, keep the window), False (not intentional, may change), None (unknown)."""
    if isinstance(v, bool):
        return v
    s = str(v if v is not None else "").strip().lower()
    if s in TRUE_WORDS:
        return True
    if s in FALSE_WORDS:
        return False
    return None


def keep_range(inv: dict) -> list[str]:
    return [r["name"] for r in inv.get("rules") or [] if keep_state(val(r.get("keep_range"))) is True]


def change_range(inv: dict) -> list[str]:
    """Rules whose short window the user said is not intentional (keep_range: false): option A may change them."""
    return [r["name"] for r in inv.get("rules") or [] if keep_state(val(r.get("keep_range"))) is False]


# ---------- facts from answers.yaml and env.yaml (one reading for every tool) ----------

RULER_TYPES = ("prometheus", "vmalert", "thanos", "grafana_alerting")
_UNANSWERED = ("", "unknown", "不知道", "未知", "none", "null", "?")


def answered(v) -> bool:
    return v is not None and str(v).strip().lower() not in _UNANSWERED


def _lowest(versions: list[str]) -> str | None:
    vs = [v for v in versions if C.parse_version(v) is not None]
    if not vs:
        return None
    lo = vs[0]
    for v in vs[1:]:
        if C.compare_versions(v, lo) < 0:
            lo = v
    return lo


def facts(answers: dict | None, env: dict | None, ruler: str | None = None) -> dict:
    """Versions and facts the user gave, read the same way by inventory.py, fit_contracts.py, review.py and
    check_coverage.py:
      version          answers.version, else env.yaml components[] of the ruler's type (the lowest when several)
      version_src      "answers.version" / "env.yaml components[<id>].version"
      ruler            answers.ruler, else the only ruler type in env.yaml components[]
      target_versions  job -> version: answers.target_versions, overridden by env.yaml targets[] (job + version)
      latency / dedup_interval / eval_delay   answers keys of the same name (vmalert data latency L, VictoriaMetrics
                       -dedup.minScrapeInterval, vmalert -rule.evalDelay), else env.yaml components[] fields
                       latency / dedup_interval / eval_delay
    Values like unknown / 不知道 / empty are not answers."""
    answers, env = answers or {}, env or {}
    comps = [c for c in env.get("components") or [] if isinstance(c, dict)]
    out: dict = {"conflicts": []}
    rk = answers.get("ruler") if answered(answers.get("ruler")) else None
    if rk is None:
        kinds = sorted({str(c.get("type")) for c in comps if c.get("type") in RULER_TYPES})
        if len(kinds) == 1:
            rk = kinds[0]
            out["ruler_src"] = "env.yaml components[].type"
    elif rk:
        out["ruler_src"] = "answers.ruler"
    out["ruler"] = rk
    kind = ruler or rk
    env_v = {str(c.get("id")): str(c["version"]) for c in comps
             if c.get("type") == kind and answered(c.get("version")) and C.parse_version(c.get("version")) is not None}
    if answered(answers.get("version")):
        out["version"], out["version_src"] = str(answers["version"]), "answers.version"
        lo = _lowest(list(env_v.values()))
        if lo and C.parse_version(lo) != C.parse_version(out["version"]):
            out["conflicts"].append(f"answers.version {out['version']} 和 env.yaml 的 {kind} 版本 {lo} 不同；用 answers.version")
    elif env_v:
        lo = _lowest(list(env_v.values()))
        cid = next(k for k, v in env_v.items() if v == lo)
        out["version"], out["version_src"] = lo, f"env.yaml components[{cid}].version"
        if len(set(env_v.values())) > 1:
            out["version_src"] += f"（{len(env_v)} 台版本不同，取最低的）"
    tv = {str(k): str(v) for k, v in (answers.get("target_versions") or {}).items() if answered(v)}
    for t in env.get("targets") or []:
        if isinstance(t, dict) and t.get("job") and answered(t.get("version")):
            tv[str(t["job"])] = str(t["version"])
    out["target_versions"] = tv
    for k in ("latency", "dedup_interval", "eval_delay"):
        if answered(answers.get(k)):
            out[k], out[k + "_src"] = str(answers[k]), f"answers.{k}"
            continue
        vals = [str(c[k]) for c in comps if answered(c.get(k))]
        if vals:
            out[k], out[k + "_src"] = vals[0], f"env.yaml components[].{k}"
    return out


def load_answers(path: str | None) -> dict:
    return (C._load(path) or {}) if path and os.path.exists(path) else {}


def load_env(path: str | None) -> dict:
    return (C._load(path) or {}) if path and os.path.exists(path) else {}


def unscraped(inv: dict) -> list[dict]:
    u = inv.get("unscraped_components")
    return u if isinstance(u, list) else []
