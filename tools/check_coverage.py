#!/usr/bin/env python3
"""Coverage checker: does this environment's monitor of monitor cover each component's signals?

  python3 tools/check_coverage.py --env env/prod.yaml --snapshot ./snapshot --out ./report [--live] [--tiers 1,2]
  python3 tools/check_coverage.py --env env/prod.yaml --inventory env/prod.inventory.yaml --out ./report
      (scrape jobs and unscraped components from the inventory; without --snapshot its rule files are evaluated
       by the env.yaml ruler of the inventory's ruler kind; --config / --snapshot given still win)

Inputs
  env.yaml                     components, observers, targets (see env/env.example.yaml)
  <snapshot>/<id>/rules.json   raw /api/v1/rules response of every ruler (Prometheus, vmalert, observers),
                               written by tools/collect.py
  --live                       also query each ruler's URL from env.yaml to check that rule selectors
                               match any series (otherwise data presence is reported as unchecked)
  --config prometheus.yml      scrape jobs of the Prometheus that scrapes the components: a component no job
                               scrapes (e.g. Alertmanager) is called out instead of only "missing rule"

Statuses
  covered         a rule watches the signal, is evaluated where the catalog says, and (with --live) has data
  self_only       only the monitored component evaluates it, so it stops when the component dies
  rule_job_mismatch a rule with the template's name (or reading the signal's metric) exists, but its job matcher
                  cannot select the job env.yaml gives (targets[].job / components[].job); checked offline with
                  the same selector parser as check_metric_sources.py
  rule_no_data    a rule exists but its selector matches no series (--live), or (--config) no scrape job has
                  the component's job
  partial         rules watch the signal but miss what the card requires: every `key_metrics` entry and one metric
                  of each `metrics_any` group (e.g. etcd health-second-path without probe_success). A card with
                  neither field requires every listed `metrics` entry; slave/replica and master/source spellings
                  count as the same metric. Probe rules (probe_success, own blackbox job) count when a label value
                  names the component (job="etcd-readyz")
  missing         no rule references the signal
  not_applicable  the signal's applies_when does not hold for this component: cluster / replicas / kubernetes,
                  or env.yaml lists `features:` for it and the needed feature is not there (vocabulary: FEATURES,
                  documented in env/env.example.yaml). Without a features list the signal stays and the note
                  says 「需要回答：有沒有 X」.
  needs_input     only an interview or inventory can answer it (e.g. the observer's failure domain); env values
                  `unknown` / `不知道` / empty are not answers

Watchdog: only a rule evaluated on the monitored ruler itself (or, for `dedicated`, a non-observer ruler with
`role: watchdog`) counts; an observer's own Watchdog only proves the observer is alive. A Watchdog rule also needs
an external dead man's switch named in env.yaml (`deadman.receiver`, or the component's `deadman`).
With --config, a component no scrape job scrapes has no data for signals that read its own metrics: rule_no_data,
whatever the placement. The to-do list is sorted by impact: tier, Watchdog / dead man's switch first, then the
chain stage of the card (collect → store → query → evaluate → route → receive), then status.

Outputs <out>/coverage.json (for AI) and <out>/coverage.md (for people).
Requires PyYAML (via tools/catalog.py); live queries use the standard library.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog as C  # noqa: E402
import check_metric_sources as MS  # noqa: E402 - same selector parser and matcher as the metric-source check

# env.yaml `type` is normally the card id itself (ls catalog/components catalog/targets); these are accepted aliases.
TYPE_ALIASES = {
    "vmcluster": "victoriametrics", "vmsingle": "victoriametrics", "thanos-ruler": "thanos", "thanos_ruler": "thanos",
    "grafana": "grafana_alerting", "valkey": "redis_exporter", "redis": "redis_exporter", "mysqld_exporter": "mysql",
    "mysqld": "mysql", "postgres": "postgres_exporter", "postgresql": "postgres_exporter",
    "mongodb": "mongodb_exporter", "mongo": "mongodb_exporter",
}
# aliases that name the exporter, not the database: the card is a guess, so the report asks which one it is
AMBIGUOUS_TYPES = {
    "mysqld_exporter": "需要回答：MySQL 還是 MariaDB？env.yaml 的 type 寫 `mysql` 或 `mariadb`（兩張卡的版本差異與專屬訊號不同）；先用 mysql 卡",
    "mysqld": "需要回答：MySQL 還是 MariaDB？env.yaml 的 type 寫 `mysql` 或 `mariadb`；先用 mysql 卡",
}


def card_for(type_: str | None, card_ids: set[str]) -> str | None:
    if type_ in card_ids:
        return type_
    return TYPE_ALIASES.get(type_ or "")


# components that evaluate alerting rules and expose a Prometheus-style /api/v1/rules
# (Thanos Ruler: /api/v1/rules; Grafana: /api/prometheus/grafana/api/v1/rules, save it as <id>/rules.json)
RULER_TYPES = {"prometheus", "vmalert", "thanos", "thanos-ruler", "thanos_ruler", "grafana_alerting", "grafana"}
STATUS_ORDER = ["missing", "rule_job_mismatch", "rule_no_data", "self_only", "partial", "needs_input", "covered",
                "not_applicable"]
STATUS_TEXT = {"covered": "涵蓋", "self_only": "只在自己身上", "rule_no_data": "規則在但沒資料", "missing": "缺少",
               "rule_job_mismatch": "規則在但 job 對不上", "not_applicable": "不適用", "needs_input": "需要回答",
               "partial": "部分涵蓋"}
ACTION = ("missing", "rule_job_mismatch", "rule_no_data", "self_only", "partial")
# chain stages in impact order (catalog cards: `stages:`); `source` (targets) is collected, `notify` is routing,
# `meta` (observer) receives
STAGE_ORDER = {"source": 0, "collect": 0, "store": 1, "query": 2, "evaluate": 3, "route": 4, "notify": 4,
               "receive": 5, "meta": 5}
DMS_SIGNALS = {"watchdog", "data-watchdog", "own-deadman", "deadman-wait", "end-to-end"}
NOT_ANSWERS = {"", "unknown", "不知道", "未知", "?", "？", "tbd", "none?", "n/a"}


def answered(v) -> bool:
    """env.yaml values like `unknown` / `不知道` / empty are not answers."""
    if v is None:
        return False
    if isinstance(v, (list, dict)):
        return bool(v)
    return str(v).strip().lower() not in NOT_ANSWERS
# signals whose rules watch other jobs (the data the component scrapes), not the component's own job
OTHER_JOB_SIGNALS = {"job-freshness", "known-true-canary", "watchdog", "data-watchdog", "scrapes-all-infra",
                     "log-freshness", "health-second-path"}


# ---------- inputs ----------

def load_env(path: str) -> dict:
    env = C._load(path) or {}
    env.setdefault("components", [])
    env.setdefault("observers", [])
    env.setdefault("targets", [])
    return env


def load_rules(snapshot: str, ruler_id: str) -> list[dict]:
    path = os.path.join(snapshot, ruler_id, "rules.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data = data.get("data", data)
    return [r for g in data.get("groups", []) for r in g.get("rules", []) if r.get("type") == "alerting"]


def rulers(env: dict) -> dict[str, dict]:
    out = {}
    for c in env["components"]:
        if c.get("type") in RULER_TYPES:
            out[c["id"]] = {**c, "_observer": False}
    for o in env["observers"]:
        out[o["id"]] = {**o, "_observer": True}
    return out


# ---------- matching ----------

def job_of(comp: dict) -> str:
    return str(comp.get("job") or comp["id"])


def _sel_job_ok(sel: dict, job: str) -> bool:
    return all(MS.matches(op, v, job) for (l, op, v) in sel["matchers"] if l == "job")


def _has_job(sel: dict) -> bool:
    return any(l == "job" for (l, _, _) in sel["matchers"])


def job_matches(expr: str, job: str) -> bool:
    """True if the expression has no job matcher, or a selector whose job matchers all match this job."""
    sels = [x for x in MS.selectors(expr) if _has_job(x)]
    return not sels or any(_sel_job_ok(x, job) for x in sels)


def rule_match(signal: dict, rule: dict, comp: dict) -> str | None:
    """None: the rule is not about this signal. "ok": it watches it. "job_mismatch": it is about this signal
    (same alert name as the template, or reads the signal's metric) but its job matcher cannot select the
    component's job from env.yaml (targets[].job / components[].job), so it returns nothing."""
    expr = str(rule.get("query", ""))
    names = {r.split("#", 1)[1] for r in signal.get("rules", [])}
    metrics = set(signal.get("metrics", [])) - {"probe_success"}  # probes run in the blackbox job
    sels = MS.selectors(expr)
    rel = [x for x in sels if x["metric"] in metrics]
    by_name = rule.get("name") in names
    job_known = comp.get("job") is not None and signal["id"] not in OTHER_JOB_SIGNALS
    job = job_of(comp)
    if by_name:
        if not job_known:
            return "ok"
        check = [x for x in (rel or sels) if _has_job(x)]
        if not check or any(_sel_job_ok(x, job) for x in (rel or sels)):
            return "ok"
        return "job_mismatch"
    if not rel:
        return None
    if all(x["metric"] == "up" for x in rel):
        # up is shared by every job: only count rules whose job matcher selects this component
        if any(_has_job(x) and _sel_job_ok(x, job) for x in rel):
            return "ok"
        return None
    if any(_sel_job_ok(x, job) for x in rel):
        return "ok"
    return "job_mismatch" if job_known else None


def rule_matches(signal: dict, rule: dict, comp: dict) -> bool:
    return rule_match(signal, rule, comp) == "ok"


def placement(ruler_id: str, ruler: dict, comp: dict) -> str:
    if ruler_id == comp["id"]:
        return "self"
    if ruler.get("_observer"):
        scrapes = ruler.get("scrapes")
        same_domain = ruler.get("failure_domain") and ruler.get("failure_domain") == comp.get("failure_domain")
        if (scrapes is None or comp["id"] in scrapes) and not same_domain:
            return "independent"
        return "same_domain"
    if comp.get("scraped_by") == ruler_id:
        return "scraper"  # the Prometheus that scrapes a target: expected place for target signals
    return "other"


# ---------- live data check ----------

def count_series(url: str, selector: str) -> int | None:
    q = urllib.parse.urlencode({"query": f"count({selector})"})
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/v1/query?{q}", timeout=15) as r:  # noqa: S310
            res = json.load(r)["data"]["result"]
        return int(float(res[0]["value"][1])) if res else 0
    except Exception:  # noqa: BLE001 - unreachable ruler is reported as unknown
        return None


def selectors(signal: dict, expr: str) -> list[str]:
    out = []
    for x in MS.selectors(expr):
        if x["metric"] in signal.get("metrics", []):
            out.append(expr[x["pos"]:x["end"]])
    return out


# ---------- applies_when ----------

# env.yaml `features:` vocabulary (components[] and targets[]). A signal's free-text applies_when is mapped
# to the features it needs (all groups must hold; any feature inside a group is enough). Documented in
# env/env.example.yaml; tests/test_coverage.py checks that every applies_when in the catalog is mapped or
# listed in APPLIES_UNMAPPED.
FEATURES = {
    "replication": "有複寫，不分主從（知道是哪一邊就改寫 replication-primary 或 replication-replica）",
    "replication-primary": "有複本的主庫（MySQL source、PostgreSQL primary、Redis master）",
    "replication-replica": "複本（MySQL replica、PostgreSQL standby、Redis replica）",
    "gtid": "MySQL／MariaDB GTID replication", "semi-sync": "semi-sync replication（MySQL plugin、MariaDB 內建）",
    "galera": "MariaDB Galera",
    "group-replication": "MySQL Group Replication／InnoDB Cluster", "heartbeat": "pt-heartbeat 之類的 heartbeat 表",
    "multi-target-probe": "一個 exporter 帶多個 target（/probe、/scrape、多個 --mongodb.uri）",
    "query-exporter": "有部署 query-exporter 補自訂查詢", "patroni": "PostgreSQL Patroni",
    "replication-slot": "PostgreSQL replication slot", "sync-replication": "PostgreSQL synchronous_standby_names",
    "archive-mode": "PostgreSQL archive_mode=on", "pgbackrest": "pgBackRest（pgbackrest_exporter）",
    "pg-stat-statements": "PostgreSQL pg_stat_statements", "node-exporter": "同一台主機有 node_exporter",
    "replica-set": "MongoDB replica set", "sharded": "MongoDB sharded cluster（exporter 連 mongos）",
    "sentinel": "Redis Sentinel", "redis-cluster": "Redis cluster_enabled yes", "rdb": "Redis 有設 save",
    "aof": "Redis appendonly yes", "check-keys": "redis_exporter 有用 --check-keys／--count-keys／--check-key-groups",
    "retention-size": "Prometheus 有設 storage.tsdb.retention.size", "results-limit": "vmalert group limit 或 -rule.resultsLimit > 0",
    "container": "跑在容器裡（Kubernetes、Docker）", "cpu-limit": "容器有設 CPU limit",
    "process-exporter": "同一台主機有 process-exporter",
}
IMPLIES = {"replication-primary": {"replication"}, "replication-replica": {"replication"}, "gtid": {"replication"},
           "semi-sync": {"replication"}}
SIDES = {"replication-primary", "replication-replica"}
_REPL = {"replication", "replication-primary", "replication-replica"}
APPLIES_FEATURES = [  # (regex on applies_when, [any-of groups]); first match wins
    (r"query-exporter", [{"query-exporter"}]),
    (r"multi-target|--mongodb\.uri", [{"multi-target-probe"}]),
    (r"semi-sync", [{"semi-sync"}]),
    (r"galera", [{"galera"}]),
    (r"group replication|innodb cluster", [{"group-replication"}]),
    (r"gtid", [{"gtid"}]),
    (r"patroni", [{"patroni"}]),
    (r"sentinel", [{"sentinel"}]),
    (r"cluster_enabled", [{"redis-cluster"}]),
    (r"sharded", [{"sharded"}]),
    (r"replica set", [{"replica-set"}]),
    (r"appendonly", [{"aof"}]),
    (r"有設 save", [{"rdb"}]),
    (r"check-keys|count-keys|check-key-groups", [{"check-keys"}]),
    (r"pg_stat_statements", [{"pg-stat-statements"}]),
    (r"synchronous_standby_names", [{"sync-replication"}]),
    (r"primary_slot_name", [{"replication-replica"}]),
    (r"replication slot", [{"replication-slot"}]),
    (r"archive_mode", [{"archive-mode"}]),
    (r"pgbackrest", [{"pgbackrest"}]),
    (r"node_exporter", [{"node-exporter"}]),
    (r"process-exporter", [{"process-exporter"}]),
    (r"CPU limit", [{"cpu-limit"}]),
    (r"容器", [{"container"}]),
    (r"retention\.size", [{"retention-size"}]),
    (r"resultslimit|group limit", [{"results-limit"}]),
    (r"有複本的主庫", [{"replication-primary"}]),
    (r"hot standby|^複本$|^replica$|的複本", [{"replication-replica"}]),
    (r"replication|主從複寫|複寫", [_REPL]),
]
# conditions that are not features (flavour / version / flag notes): stay "需要回答" when they matter
APPLIES_UNMAPPED = {"performance_schema=ON（MariaDB 預設 OFF，要重啟）", "要 --collect.info_schema.innodb_metrics",
                    "thread_handling=one-thread-per-connection（預設）", "MariaDB 11.4 以上（slave_connections_needed_for_purge）",
                    "PostgreSQL 17 以上要打開 --collector.stat_checkpointer",
                    "依賴時鐘的資料庫（YugabyteDB 的 hybrid clock、TiDB 的 TSO、MongoDB 的 wall time lag）"}
# replication signals that only make sense on one side, although the card says just "replication"
SIGNAL_SIDE = {
    (card, sig): side for card in ("mysql", "mariadb") for sig, side in (
        ("replication-running", "replication-replica"), ("replication-error", "replication-replica"),
        ("replica-status-present", "replication-replica"), ("replication-lag", "replication-replica"),
        ("replica-read-only", "replication-replica"), ("primary-writable", "replication-primary"))
}


def feature_requirement(cond: str, card_id: str | None = None, signal_id: str | None = None) -> list[set] | None:
    side = SIGNAL_SIDE.get((card_id, signal_id))
    for rx, req in APPLIES_FEATURES:
        if re.search(rx, cond, re.I):
            if side and req == [_REPL]:
                return [{side}]
            return [set(g) for g in req]
    return None


def expand(features) -> set[str]:
    out = set()
    for f in features or []:
        f = str(f).strip().lower()
        out.add(f)
        out |= IMPLIES.get(f, set())
    return out


def applies(signal: dict, comp: dict, env: dict, card_id: str | None = None) -> tuple[bool, str]:
    """(applies, note). not applicable only when env.yaml lists features for this component and the needed
    one is not there; without a features list the signal is kept and the note asks the question."""
    cond = str(signal.get("applies_when") or "").strip()
    if not cond:
        return True, ""
    if cond == "cluster":
        if "mode" in comp and not answered(comp.get("mode")):
            return True, "需要回答：是 single 還是 cluster？（env.yaml 的 mode）"
        ok = comp.get("mode") == "cluster" or comp.get("type") == "vmcluster"
        return ok, "" if ok else "不是 cluster"
    if cond.startswith("replicas"):
        ok = int(comp.get("replicas", 1)) > 1
        return ok, "" if ok else "只有 1 台"
    if cond == "kubernetes":
        ok = bool(env.get("kubernetes"))
        return ok, "" if ok else "env.yaml 沒有標 kubernetes: true"
    if cond == "prometheus in env":
        ok = any(c.get("type") == "prometheus" for c in env["components"])
        return ok, ""
    req = feature_requirement(cond, card_id, signal.get("id"))
    words = "、".join(sorted({f for g in req for f in g})) if req else ""
    raw = comp.get("features")
    if isinstance(raw, str) and not answered(raw) or isinstance(raw, list) and raw and not any(answered(x) for x in raw):
        raw = None  # `features: unknown` is not a list of what exists
    if raw is None:
        hint = f"（在 env.yaml 的 features 寫 {words}，或列出有的功能）" if words else "（在 env.yaml 的 features 標明）"
        return True, f"需要回答：有沒有「{cond}」{hint}"
    have = expand(raw)
    if req is None:
        return True, f"需要回答：「{cond}」成立嗎？（features 沒有對應的詞）"
    for group in req:
        if group & have:
            continue
        if group <= SIDES and "replication" in have and not (have & SIDES):
            return True, "需要回答：這台是 primary 還是 replica？（features 寫 replication-primary 或 replication-replica）"
        return False, f"env.yaml 的 features 沒有 {'／'.join(sorted(group))}（{cond}）"
    return True, ""


# ---------- observer card (architecture questions answered from env.yaml) ----------

def observer_item(signal: dict, env: dict) -> tuple[str, str]:
    obs = env["observers"]
    sid = signal["id"]
    if not obs:
        return "missing", "env.yaml 沒有任何外部觀察者"
    if sid == "failure-domain":
        bad = [o["id"] for o in obs if not answered(o.get("failure_domain")) or o.get("failure_domain") == "same"]
        return ("needs_input", f"這些觀察者的故障域未標明：{', '.join(bad)}") if bad else ("covered", "依 env.yaml（使用者說的）")
    if sid == "separate-alert-path":
        monitored_am = {c["id"] for c in env["components"] if c.get("type") == "alertmanager"}
        unk = [o["id"] for o in obs if not answered(o.get("notify_via"))]
        bad = [o["id"] for o in obs if answered(o.get("notify_via")) and o.get("notify_via") in monitored_am]
        if bad:
            return "missing", f"這些觀察者經過被監控的 Alertmanager：{', '.join(bad)}"
        if unk:
            return "needs_input", f"這些觀察者的 notify_via 未標明（unknown 不算回答）：{', '.join(unk)}"
        return "covered", "依 env.yaml（使用者說的）"
    if sid == "own-deadman":
        ok = all(answered(o.get("deadman")) for o in obs)
        return ("covered", "依 env.yaml（使用者說的）") if ok else ("needs_input", "觀察者的 deadman 欄位未填")
    if sid == "deadman-wait":
        dm = env.get("deadman") or {}
        return ("covered", f"max_silence = {dm.get('max_silence')}，請對照契約確認") if answered(dm.get("max_silence")) \
            else ("needs_input", "env.yaml 沒有 deadman.max_silence")
    if sid == "scrapes-all-infra":
        infra = {c["id"] for c in env["components"]}
        seen = {s for o in obs for s in (o.get("scrapes") or [])}
        miss = sorted(infra - seen)
        return ("missing", f"沒有觀察者抓這些元件：{', '.join(miss)}") if miss else ("covered", "依 env.yaml 的 scrapes")
    return "needs_input", "這一項要問架構"


# ---------- metric coverage (partial) ----------

def _norm_metric(m: str) -> str:
    """slave/replica and master/source spellings of the same MySQL/Redis metric count as one."""
    m = re.sub(r"(?<![A-Za-z0-9])replica(?![A-Za-z0-9])", "slave", m)   # whole tokens only: not "replication"
    return re.sub(r"(?<![A-Za-z0-9])source(?![A-Za-z0-9])", "master", m)


def rule_metrics(expr: str) -> tuple[set[str], list[str]]:
    """(metric names, __name__ regexes) a rule expression reads."""
    names, pats = set(), []
    for x in MS.selectors(expr):
        if x["metric"]:
            names.add(x["metric"])
        for (_, op, v) in x["name_matchers"]:
            if op == "=~":
                pats.append(v)
    return names, pats


def required_sets(signal: dict) -> list[list[str]] | None:
    """What full coverage needs, from the card: every `key_metrics` entry, and one metric of each
    `metrics_any` group. None when the card has neither (then every listed metric is required)."""
    if "key_metrics" not in signal and "metrics_any" not in signal:
        return None
    return [[m] for m in signal.get("key_metrics") or []] + [list(g) for g in signal.get("metrics_any") or []]


def missing_metrics(signal: dict, rules: list[dict]) -> list[str]:
    """Card metrics of the signal that none of the rules read (normalized for slave/replica). With key_metrics /
    metrics_any only those count: a missing key metric is listed by name, an unmet group as 「a 或 b」."""
    names, pats = set(), []
    for r in rules:
        n, p = rule_metrics(str(r.get("query", "")))
        names |= {_norm_metric(x) for x in n}
        pats += p

    def used(m: str) -> bool:
        m = _norm_metric(m)
        if m in names:
            return True
        for p in pats:
            try:
                if re.fullmatch(_norm_metric(p), m) or re.fullmatch(p, m):
                    return True
            except re.error:
                continue
        return False
    sets = required_sets(signal)
    if sets is None:
        want = list(dict.fromkeys(_norm_metric(m) for m in signal.get("metrics") or []))
        return [m for m in want if not used(m)]
    return [" 或 ".join(g) for g in sets if g and not any(used(m) for m in g)]


PROBE_METRICS = {"probe_success", "probe_http_status_code", "probe_duration_seconds"}


def related_probe_rules(signal: dict, comp: dict, card_id: str, rules_by_ruler: dict) -> list[dict]:
    """Rules reading a probe metric the card lists (blackbox runs in its own job, so rule_match skips them) whose
    selector names this component in a label value (job="etcd-readyz", instance=~".*etcd.*")."""
    want = set(signal.get("metrics") or []) & PROBE_METRICS
    if not want:
        return []
    words = {str(w).lower() for w in (comp.get("job"), comp.get("id"), comp.get("type"), card_id.replace("_exporter", ""))
             if w and len(str(w)) >= 3}
    out = []
    for rid, rules in rules_by_ruler.items():
        for r in rules:
            for x in MS.selectors(str(r.get("query", ""))):
                if x["metric"] in want and any(w in str(v).lower() for (_, _, v) in x["matchers"] for w in words):
                    out.append({"ruler": rid, "alert": r.get("name"), "query": r.get("query", ""), "probe": True})
                    break
    return out


def dms_named(env: dict, comp: dict) -> bool:
    dm = comp.get("deadman") if "deadman" in comp else (env.get("deadman") or {})
    if isinstance(dm, dict):
        return answered(dm.get("receiver"))
    return answered(dm)


# ---------- main evaluation ----------

def scrape_note(comp: dict, card_id: str, scrape_jobs: set[str] | None, unscraped: list[dict] | None = None) -> str:
    """With --config: is this component scraped at all? (rules copied from templates have no data otherwise)
    With --inventory, its unscraped_components (Alertmanager targets, the ruler itself) decide for components
    env.yaml gives no job."""
    if scrape_jobs is None or card_id == "observer":
        return ""
    job = comp.get("job")
    if job is None:
        hit = [u for u in unscraped or [] if u.get("component") in (card_id, comp.get("type"))]
        if hit:
            return "inventory：" + "；".join(u.get("fact", "") for u in hit) + "（先加 scrape job 再加規則）"
    if job is not None:
        return "" if job in scrape_jobs else f"config 沒有 job `{job}`：這個元件沒有被 scrape，先加 scrape job 再加規則"
    words = {str(comp.get("type") or "").lower(), str(card_id).lower().replace("_exporter", ""), str(comp["id"]).lower()}
    if any(w and w in j.lower() for j in scrape_jobs for w in words):
        return ""
    return (f"config 裡找不到像是 {comp.get('type')} 的 scrape job（有：{', '.join(sorted(scrape_jobs))}）："
            f"可能沒有被 scrape；有的話在 env.yaml 填 job")


def evaluate(env: dict, snapshot: str | None, tiers: set[int], live: bool, scrape_jobs: set[str] | None = None,
             rules_override: dict | None = None, unscraped: list[dict] | None = None) -> list[dict]:
    cards = {c["id"]: c for c in C.load_components()}
    rl = rulers(env)
    rules_by_ruler = {rid: (rules_override or {}).get(rid) if rid in (rules_override or {})
                      else (load_rules(snapshot, rid) if snapshot else []) for rid in rl}
    items = []
    ids = set(cards)
    instances = [(c, card_for(c.get("type"), ids)) for c in env["components"]]
    instances += [(t, card_for(t.get("type"), ids)) for t in env["targets"]]
    instances.append(({"id": "observers", "type": "observer"}, "observer"))
    for comp, card_id in instances:
        if card_id not in cards:
            items.append({"component": comp["id"], "card": None, "signal": None, "tier": None, "status": "needs_input",
                          "note": f"元件類型 {comp.get('type')!r} 沒有對應的元件卡"})
            continue
        card = cards[card_id]
        if comp.get("type") in AMBIGUOUS_TYPES and comp.get("type") not in cards:
            items.append({"component": comp["id"], "card": card_id, "signal": None, "tier": None, "status": "needs_input",
                          "note": AMBIGUOUS_TYPES[comp["type"]]})
        unscraped_txt = scrape_note(comp, card_id, scrape_jobs, unscraped)
        for s in card["signals"]:
            if s["tier"] not in tiers:
                continue
            item = {"component": comp["id"], "card": card_id, "signal": s["id"], "tier": s["tier"],
                    "question": s["question"], "evaluate_on": s["evaluate_on"], "rules": [], "note": "",
                    "suggested_rules": s.get("rules", []), "runbook": C.runbook_url(card_id, s["id"])}
            ok, why = applies(s, comp, env, card_id)
            if not ok:
                item.update(status="not_applicable", note=why)
                items.append(item)
                continue
            extra = [x for x in (why, unscraped_txt if s.get("metrics") else "") if x]

            def push(status: str, note: str = "") -> None:
                item.update(status=status, note="；".join([n for n in (note, *extra) if n]))
                items.append(item)

            if card_id == "observer":
                push(*observer_item(s, env))
                continue
            if s["evaluate_on"] in ("interview", "inventory", "external"):
                push("needs_input", {"interview": "要問架構", "inventory": "盤點時檢查（/api/v1/rules、/flags）",
                                     "external": "由外部服務判斷，例如 dead man's switch 是否收到"}[s["evaluate_on"]])
                continue
            mismatched, foreign_wd = [], []
            for rid, rules in rules_by_ruler.items():
                for r in rules:
                    m = rule_match(s, r, comp)
                    if m is None:
                        continue
                    entry = {"ruler": rid, "alert": r.get("name"), "placement": placement(rid, rl[rid], comp),
                             "query": r.get("query", "")}
                    if s["id"] == "watchdog" and rid != comp["id"] and \
                            (rl[rid].get("_observer") or not (s["evaluate_on"] == "dedicated" and rl[rid].get("role") == "watchdog")):
                        foreign_wd.append(entry)  # another ruler's Watchdog proves that ruler is alive, not this one
                        continue
                    (item["rules"] if m == "ok" else mismatched).append(entry)
            item["mismatched_rules"] = mismatched
            if foreign_wd:
                item["other_rulers_watchdog"] = foreign_wd
                extra.append(f"{'、'.join(sorted({r['ruler'] for r in foreign_wd}))} 自己的 Watchdog 只證明它自己活著，"
                             f"不算 {comp['id']} 的 Watchdog")
            own_data = bool(s.get("metrics")) and s["id"] not in OTHER_JOB_SIGNALS
            if unscraped_txt and own_data and scrape_jobs is not None and item["rules"]:
                push("rule_no_data", f"沒有資料：{comp['id']} 沒有被 scrape，讀它自己 metric 的規則回傳空"
                                     "（只在自己身上或放在觀察者都一樣）")
                continue
            if not item["rules"]:
                if mismatched:
                    names = "、".join(f"`{r['alert']}`" for r in mismatched[:4])
                    push("rule_job_mismatch", f"{names} 的 job 條件選不到 env.yaml 的 job `{job_of(comp)}`，回傳空、不會響"
                                              "（包在 absent() 裡的則一直在響）；把 job 條件改成實際的 job")
                else:
                    push("missing")
                continue
            wants_self = s["evaluate_on"] in ("self", "dedicated")
            good = [r for r in item["rules"]
                    if r["placement"] in (("self", "scraper", "independent") if wants_self else ("independent",))]
            if not good:
                push("self_only", "只有被監控的元件（或同一故障域的 ruler）在評估，它掛掉時這條規則也停")
                continue
            probes = related_probe_rules(s, comp, card_id, rules_by_ruler)
            gaps = missing_metrics(s, good + probes)
            if gaps:
                first = _norm_metric((s.get("metrics") or [""])[0])
                legacy = required_sets(s) is None
                push("partial", f"規則沒有用到 {'、'.join('`' + m + '`' for m in gaps[:4])}{' 等' if len(gaps) > 4 else ''}"
                                + ("（卡片列的第一個 metric）" if first in gaps and legacy else "")
                                + ("；卡片沒有標哪些 metric 可以互相替代（`metrics_any`／`key_metrics`），先當成都要" if legacy
                                   else "（卡片的 `key_metrics`／`metrics_any`：完整涵蓋一定要有）"))
                continue
            if probes:
                item["probe_rules"] = probes
            if s["id"] == "watchdog" and not dms_named(env, comp):
                push("needs_input", "Watchdog 規則在，但 env.yaml 沒寫它送到哪個外部 dead man's switch（deadman.receiver）；"
                                    "沒有外部接收端時 Watchdog 停了也沒有人知道")
                continue
            if live:
                has_data = False
                for r in good:
                    url = rl[r["ruler"]].get("url")
                    counts = [count_series(url, sel) for sel in selectors(s, r["query"])] if url else []
                    r["series"] = counts
                    if any(c for c in counts) or not counts:
                        has_data = True
                push("covered" if has_data else "rule_no_data",
                     "" if has_data else "規則的 selector 查不到任何 series：檢查 job 名稱、label 或這個 metric 是否存在")
            else:
                push("covered", "未用 --live 檢查資料是否存在" if comp.get("job") is None or s["id"] in OTHER_JOB_SIGNALS
                     else f"job `{comp['job']}` 對得上；未用 --live 檢查資料是否存在")
    return items


ADVICE = {
    "missing": ("沒有規則", "從建議規則複製，改成這個環境的 job", "多一條規則要維護；照範本加之前先確認 metric 有被 scrape"),
    "rule_job_mismatch": ("規則在，但 job 條件選不到這個元件", "把 rule 的 job 條件改成 env.yaml／config 裡的 job",
                          "要一起改用到同一個 job 的 dashboard"),
    "rule_no_data": ("規則在，但沒有資料", "先讓 metric 有資料（加 scrape job、開 collector），再看規則",
                     "多一個 scrape job 的負載"),
    "self_only": ("只有被監控的元件自己在評估", "把規則放到外部觀察者（或另一個故障域的 ruler）",
                  "要多維護一台觀察者"),
    "partial": ("規則在，但沒用到卡片列的全部 metric", "照說明欄補上沒用到的 metric（範本見建議規則）",
                "多幾條規則；有些 metric 要先開 collector 或加 probe job"),
}


def impact_key(i: dict, cards: dict | None = None) -> tuple:
    """Tier first, then Watchdog / dead man's switch, then the card's first chain stage, infra before targets."""
    card = (cards or {}).get(i.get("card")) or {}
    stages = card.get("stages") or []
    stage = min((STAGE_ORDER.get(x, 9) for x in stages), default=9)
    return (i.get("tier") or 9, 0 if i.get("signal") in DMS_SIGNALS else 1, stage,
            1 if card.get("kind") == "target" else 0, STATUS_ORDER.index(i["status"]), i["component"])


def render_md(env: dict, items: list[dict], tiers: set[int]) -> str:
    counts = {k: sum(1 for i in items if i["status"] == k) for k in STATUS_ORDER}
    out = [f"# Monitor of monitor 覆蓋：{env.get('name', '')}", "",
           f"產生時間 {time.strftime('%Y-%m-%d %H:%M')}，檢查第 {'、'.join(map(str, sorted(tiers)))} 級訊號。", ""]
    act = [i for i in items if i["status"] in ACTION]
    out += ["## 建議先做", ""]
    if not act:
        out.append("- 檢查的訊號都有規則在看，沒有要先處理的項目。")
    for st in ACTION:
        group = [i for i in act if i["status"] == st]
        if not group:
            continue
        what, todo, cost = ADVICE[st]
        comps = list(dict.fromkeys(i["component"] for i in group))
        out.append(f"- **{what}**：{len(group)} 個訊號（{'、'.join(comps)}）。**建議**：{todo}。**代價**：{cost}。")
    ask = [i for i in items if i["status"] != "not_applicable" and "需要回答" in (i.get("note") or "")]
    if ask:
        qs = list(dict.fromkeys(n for i in ask for n in i["note"].split("；") if n.startswith("需要回答")))
        out.append(f"- **要先確認的條件**（決定訊號適不適用）：{'；'.join(q.removeprefix('需要回答：') for q in qs[:6])}"
                   f"{' 等' if len(qs) > 6 else ''}")
    out += ["", "| " + " | ".join(STATUS_TEXT[k] for k in STATUS_ORDER) + " |",
            "|" + "---|" * len(STATUS_ORDER),
            "| " + " | ".join(str(counts[k]) for k in STATUS_ORDER) + " |", ""]
    out += ["## 要先處理", "", "依影響排序：級數、Watchdog／dead man's switch、鏈上的位置（採集 → 儲存 → 查詢 → 評估 → 路由 → 接收）。", "",
            "| 級 | 元件 | 訊號 | 狀態 | 說明 | 建議規則 |", "|---|---|---|---|---|---|"]
    cards = {c["id"]: c for c in C.load_components()}
    for i in sorted(items, key=lambda x: impact_key(x, cards)):
        if i["status"] in ACTION:
            sug = "、".join(f"`{r.split('#')[1]}`" for r in i.get("suggested_rules", [])) or "—"
            out.append(f"| {i.get('tier') or ''} | {i['component']} | [{i.get('question') or i['signal']}]({i.get('runbook', '')}) | "
                       f"{STATUS_TEXT[i['status']]} | {i.get('note', '')} | {sug} |")
    out.append("")
    for comp in dict.fromkeys(i["component"] for i in items):
        out += [f"## {comp}", "", "| 級 | 訊號 | 狀態 | 規則（ruler：alert，位置） | 說明 |", "|---|---|---|---|---|"]
        for i in [x for x in items if x["component"] == comp]:
            rules = "<br>".join(f"{r['ruler']}：{r['alert']}（{r['placement']}）" for r in i.get("rules", [])[:4]) or \
                "<br>".join(f"{r['ruler']}：{r['alert']}（job 對不上）" for r in i.get("mismatched_rules", [])[:4]) or "—"
            out.append(f"| {i.get('tier') or ''} | {i.get('question') or i.get('signal') or ''} | {STATUS_TEXT[i['status']]} | "
                       f"{rules} | {i.get('note', '')} |")
        out.append("")
    return "\n".join(out)


def inventory_rules(env: dict, inv: dict) -> dict[str, list[dict]]:
    """No snapshot: the inventory's rule files, evaluated by the env.yaml ruler of the inventory's ruler kind
    (the only ruler, or the first of that type)."""
    import inventory_use as IU
    import rules_to_snapshot as R2S
    files = list((inv.get("sources") or {}).get("rules") or [])
    kind = IU.val((inv.get("stack") or {}).get("ruler"))
    cands = [c for c in env["components"] if c.get("type") in RULER_TYPES]
    pick = next((c for c in cands if c.get("type") == kind), cands[0] if cands else None)
    if not pick or not files:
        return {}
    snap = R2S.convert([f for f in files if os.path.exists(f)])
    return {pick["id"]: [r for g in snap["data"]["groups"] for r in g["rules"] if r.get("type") == "alerting"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env", required=True)
    ap.add_argument("--snapshot", help="collect.py／rules_to_snapshot.py 的目錄；有 --inventory 時可省略（用 inventory 記的 rule 檔）")
    ap.add_argument("--inventory", help="tools/inventory.py 的 env/<name>.inventory.yaml：scrape job、沒被抓的元件、rule 檔從這裡讀")
    ap.add_argument("--out", required=True)
    ap.add_argument("--live", action="store_true", help="query each ruler to check rule selectors match series")
    ap.add_argument("--tiers", default="1", help="comma-separated tiers, default 1")
    ap.add_argument("--config", action="append", default=[],
                    help="prometheus.yml of the scraping Prometheus (repeatable): flags components no job scrapes")
    a = ap.parse_args(argv)
    env = load_env(a.env)
    tiers = {int(t) for t in a.tiers.split(",")}
    scrape_jobs = {j["job"] for c in a.config for j in MS.load_config(c)} if a.config else None
    inv, override, unscraped = None, None, None
    if a.inventory:
        import inventory_use as IU
        inv = IU.load(a.inventory)
        if scrape_jobs is None and IU.has_config(inv):
            scrape_jobs = IU.job_names(inv)
        unscraped = IU.unscraped(inv)
        if not a.snapshot:
            override = inventory_rules(env, inv)
    if not a.snapshot and override is None:
        ap.error("要給 --snapshot（或 --inventory）")
    items = evaluate(env, a.snapshot, tiers, a.live, scrape_jobs, override, unscraped)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "coverage.json"), "w", encoding="utf-8") as f:
        json.dump({"env": env.get("name"), "generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "tiers": sorted(tiers),
                   "live": a.live, "items": items}, f, indent=1, ensure_ascii=False)
    with open(os.path.join(a.out, "coverage.md"), "w", encoding="utf-8") as f:
        f.write(render_md(env, items, tiers))
    bad = sum(1 for i in items if i["status"] in ACTION)
    print(f"{len(items)} 個訊號，建議先處理 {bad} 個 → {a.out}/coverage.md（「建議先做」）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
