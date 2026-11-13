"""Load the alert-trust catalog. Requires PyYAML."""
from __future__ import annotations

import glob
import os
import re

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_URL = "https://hahasula.github.io/alert-trust"
REPO_URL = "https://github.com/HahaSula/alert-trust"

TIERS = {1, 2, 3}
EVIDENCE = {"tested": "實測", "code": "讀碼", "upstream": "上游", "inferred": "推論"}
UPSTREAM = {"yes": "有", "partial": "部分", "no": "無"}
EVALUATE_ON = {
    "self": "被監控的元件自己",
    "observer": "外部觀察者",
    "federate": "外部觀察者（經 /federate）",
    "dedicated": "獨立的 ruler",
    "external": "外部服務",
    "inventory": "盤點時檢查",
    "interview": "問架構",
}
KINDS = {"infra", "target", "architecture"}
FAMILIES = {
    "prometheus": "Prometheus 體系",
    "victoriametrics": "VictoriaMetrics 體系",
    "shared": "共用：通知與外部觀察",
    "database": "Target：資料庫",
}
STATUS = {"stable", "draft"}
BLIND_CATEGORIES = {"exporter": "元件有、exporter 或 endpoint 沒輸出", "database": "元件本身沒提供", "other": "要靠其他元件"}
ASPECTS = {
    "exporter": "監測自檢：exporter 有沒有真的在量",
    "cluster": "叢集與共識：quorum、成員、flow control",
    "replication": "複寫與 HA：延遲、角色、failover",
    "availability": "可用性：活著、連得上、寫得進去",
    "saturation": "資源與飽和：連線、執行緒、cache、IO、鎖等待",
    "workload": "工作量與效能：吞吐、錯誤、延遲、慢 SQL",
    "maintenance": "資料維護與完整性：vacuum／purge、wraparound、損毀",
    "protection": "資料保護：備份、log archive、可還原",
    "capacity": "容量與成長：趨勢與預測",
}
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
COMPONENT_ID_RE = re.compile(r"^[a-z0-9_]+$")


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys instead of silently keeping the last one."""


def _no_duplicates(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise ValueError(f"{node.start_mark.name}:{key_node.start_mark.line + 1}: 重複的 key {key!r}")
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def _load(path: str):
    with open(path, encoding="utf-8") as f:
        return yaml.load(f, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass


def load_sources(root: str = ROOT) -> dict:
    """catalog/sources.yaml plus catalog/sources.d/*.yaml (one file per target keeps edits apart)."""
    out = dict(_load(os.path.join(root, "catalog", "sources.yaml"))["sources"])
    for path in sorted(glob.glob(os.path.join(root, "catalog", "sources.d", "*.yaml"))):
        for key, val in ((_load(path) or {}).get("sources") or {}).items():
            if key in out:
                raise ValueError(f"{path}: source key {key!r} 重複")
            out[key] = val
    return out


def load_refs(root: str = ROOT) -> dict:
    """catalog/refs-code.yaml (generated, pinned source lines) plus catalog/refs.yaml (experiments)."""
    out: dict = {}
    paths = [os.path.join(root, "catalog", n) for n in ("refs-code.yaml", "refs.yaml")]
    paths += sorted(glob.glob(os.path.join(root, "catalog", "refs.d", "*.yaml")))
    for path in paths:
        if os.path.exists(path):
            for key, val in ((_load(path) or {}).get("refs") or {}).items():
                if key in out:
                    raise ValueError(f"{path}: ref key {key!r} 重複")
                out[key] = val
    return out


def load_terms(root: str = ROOT) -> dict:
    path = os.path.join(root, "catalog", "terms.yaml")
    return (_load(path) or {}).get("terms", {}) if os.path.exists(path) else {}


def term_anchor(key: str) -> str:
    return key.replace("_", "-").replace(".", "-")


def load_contracts(root: str = ROOT) -> dict:
    return _load(os.path.join(root, "catalog", "contracts.yaml"))["contracts"]


def component_files(root: str = ROOT) -> list[str]:
    files = glob.glob(os.path.join(root, "catalog", "components", "*.yaml"))
    files += glob.glob(os.path.join(root, "catalog", "targets", "*.yaml"))
    return sorted(files)


def load_components(root: str = ROOT) -> list[dict]:
    out = []
    for path in component_files(root):
        card = _load(path)
        card["_path"] = os.path.relpath(path, root)
        for s in card.get("signals", []):
            up = s.get("upstream") or {}
            # YAML 1.1 reads bare yes/no as booleans
            if isinstance(up.get("status"), bool):
                up["status"] = "yes" if up["status"] else "no"
        out.append(card)
    fam = list(FAMILIES)
    within = {"prometheus": 0, "vmagent": 0, "victoriametrics": 1, "victorialogs": 2, "vmalert": 3, "thanos": 1, "grafana_alerting": 0, "alertmanager": 1, "observer": 2}
    return sorted(out, key=lambda c: (fam.index(c["family"]) if c.get("family") in fam else 9,
                                      within.get(c.get("id"), 5), c.get("_path")))


def rule_files(root: str = ROOT) -> list[str]:
    return sorted(glob.glob(os.path.join(root, "rules", "**", "*.yml"), recursive=True))


def load_rules(root: str = ROOT) -> dict[str, dict]:
    """Return {"rules/x/y.yml#Alert": rule} for every alerting rule."""
    out = {}
    for path in rule_files(root):
        rel = os.path.relpath(path, root)
        for group in (_load(path) or {}).get("groups", []):
            for rule in group.get("rules", []):
                if "alert" in rule:
                    out[f"{rel}#{rule['alert']}"] = rule
    return out


# Five-section framework (docs/map.md): every card answers
#   1 architecture (card.architecture), 2 usage (card.usage), 3 observe and 4 misuse (signal.section;
#   card.bad_queries maps kinds of badly written queries to misuse signals),
#   5 blind (card.blind_spots, one-line options).
SECTIONS = {"observe": "正確觀測：看到的數字是不是真的", "misuse": "不正確使用時的觀測：誤用或故障時看得到什麼"}
USAGE_EVIDENCE = {"tested": "實測（本機小實驗）", "code": "讀碼（原始碼推理）", "doc": "官方文件"}
OPTIONS_MAX = 120


def check_five_sections(card: dict, all_refs: dict) -> tuple[list[str], int]:
    """Errors for usage / section / blind_spots.options, and how many signals still lack a section."""
    where = card.get("_path", card.get("id", "?"))
    errors, missing = [], 0
    seen = set()
    for i, u in enumerate(card.get("usage") or []):
        at = f"{where}: usage[{i}]"
        uid = u.get("id", "")
        if not ID_RE.match(uid):
            errors.append(f"{at} id 要是 kebab-case")
        if uid in seen:
            errors.append(f"{at} id {uid!r} 重複")
        seen.add(uid)
        if not str(u.get("text", "")).strip():
            errors.append(f"{at} 缺少 text")
        if u.get("evidence") not in USAGE_EVIDENCE:
            errors.append(f"{at} evidence 只能是 {'、'.join(USAGE_EVIDENCE)}（正確使用只寫原始碼推理、本機小實驗或標明的官方文件）")
        refs = u.get("refs") or []
        if not refs:
            errors.append(f"{at} 要有 refs")
        for r in refs:
            if r not in all_refs:
                errors.append(f"{at} refs {r!r} 不存在")
    for s in card.get("signals", []):
        sec = s.get("section")
        if sec is None:
            missing += 1
        elif sec not in SECTIONS:
            errors.append(f"{where}#{s.get('id')}: section 要是 {'、'.join(SECTIONS)} 之一")
    sig_ids = {s.get("id") for s in card.get("signals", [])}
    for i, q in enumerate(card.get("bad_queries") or []):
        at = f"{where}: bad_queries[{i}]"
        for f in ("case", "signals", "visible", "not_visible", "refs"):
            if not q.get(f):
                errors.append(f"{at} 缺少 {f}")
        for sid in q.get("signals") or []:
            if sid not in sig_ids:
                errors.append(f"{at} 訊號 {sid!r} 不在這張卡")
        for r in q.get("refs") or []:
            if r not in all_refs:
                errors.append(f"{at} refs {r!r} 不存在")
    for i, b in enumerate(card.get("blind_spots") or []):
        opt = b.get("options")
        if opt is not None and (not isinstance(opt, str) or "\n" in opt.strip() or len(opt) > OPTIONS_MAX):
            errors.append(f"{where}: blind_spots[{i}].options 要是一行、{OPTIONS_MAX} 字以內（方案點到為止）")
    return errors, missing


# Optional card field (catalog/components/*.yaml, catalog/targets/*.yaml):
#   must_ask: [{id, question, why, refs, applies_when?, answer_key?}]
# Component-specific questions whose answer changes conclusions (a flag that decides whether a metric exists, an
# exporter deadline, the data latency of a ruler). tools/inventory.py asks them, ranked high, for every card present
# in the inventory. id kebab-case and unique in the card; question / why non-empty strings; refs non-empty and
# existing; applies_when (optional) a feature from tools/check_coverage.py FEATURES: only asked when the inventory
# shows that feature (e.g. a patroni job); answer_key (optional) the answers.yaml key the answer goes to
# (default facts.<card>.<id>).
MUST_ASK_KEYS = {"id", "question", "why", "refs", "applies_when", "answer_key"}
_ANSWER_KEY_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z0-9_.-]+)?$")


def check_must_ask(card: dict, all_refs: dict, features: set | None = None) -> list[str]:
    """Errors for the optional `must_ask` field of a card."""
    if "must_ask" not in card:
        return []
    where = card.get("_path", card.get("id", "?"))
    ma = card["must_ask"]
    if not isinstance(ma, list) or not ma:
        return [f"{where}: must_ask 要是非空清單（[{{id, question, why, refs}}]）"]
    errors, seen = [], set()
    for i, q in enumerate(ma):
        at = f"{where}: must_ask[{i}]"
        if not isinstance(q, dict):
            errors.append(f"{at} 要是 {{id, question, why, refs}}")
            continue
        errors += [f"{at} 不認得 {k!r}（只有 {'、'.join(sorted(MUST_ASK_KEYS))}）" for k in q if k not in MUST_ASK_KEYS]
        qid = q.get("id", "")
        if not isinstance(qid, str) or not ID_RE.match(qid):
            errors.append(f"{at} id 要是 kebab-case")
        elif qid in seen:
            errors.append(f"{at} id {qid!r} 重複")
        seen.add(qid)
        for f in ("question", "why"):
            if not isinstance(q.get(f), str) or not q[f].strip():
                errors.append(f"{at} 缺少 {f}")
        refs = q.get("refs")
        if not isinstance(refs, list) or not refs or not all(isinstance(r, str) for r in refs):
            errors.append(f"{at} 要有 refs（ref key 清單）")
        else:
            errors += [f"{at} refs {r!r} 不存在" for r in refs if r not in all_refs]
        aw = q.get("applies_when")
        if aw is not None and (not isinstance(aw, str) or (features is not None and aw not in features)):
            errors.append(f"{at} applies_when 要是 check_coverage.FEATURES 裡的功能，不是 {aw!r}")
        ak = q.get("answer_key")
        if ak is not None and (not isinstance(ak, str) or not _ANSWER_KEY_RE.match(ak)):
            errors.append(f"{at} answer_key 要是 answers.yaml 的 key（例如 latency、exporter_deadline）")
    return errors


def runbook_url(component_id: str, signal_id: str) -> str:
    return f"{SITE_URL}/components/{component_id}/#{signal_id}"


def check_metric_fields(signal: dict) -> list[str]:
    """Optional signal fields that say what full coverage needs (tools/check_coverage.py `partial`):
      key_metrics: [m, ...]          every one must be read by a rule
      metrics_any: [[a, b], [c, d]]  each group is alternatives: one of them is enough
    Every name must also be in `metrics`. Without either field every `metrics` entry is required."""
    errs = []
    metrics = set(signal.get("metrics") or [])
    if "key_metrics" in signal:
        km = signal["key_metrics"]
        if not isinstance(km, list) or not km or not all(isinstance(m, str) and m for m in km):
            errs.append("key_metrics 要是非空的 metric 名稱清單")
        else:
            errs += [f"key_metrics 的 {m} 不在 metrics 裡" for m in km if m not in metrics]
            if len(set(km)) != len(km):
                errs.append("key_metrics 有重複")
    if "metrics_any" in signal:
        ma = signal["metrics_any"]
        if not isinstance(ma, list) or not ma or not all(isinstance(g, list) and len(g) >= 2 and
                                                         all(isinstance(m, str) and m for m in g) for g in ma):
            errs.append("metrics_any 要是清單的清單，每組至少 2 個可以互相替代的 metric")
        else:
            errs += [f"metrics_any 的 {m} 不在 metrics 裡" for g in ma for m in g if m not in metrics]
            km = set(signal.get("key_metrics") or [])
            errs += [f"{m} 同時在 key_metrics 與 metrics_any（key 一定要有，不會是替代）" for g in ma for m in g if m in km]
    return errs


TARGET_MODELS = {"exporter": "required_signals", "native": "required_signals_native"}


def required_target_signals(pattern: dict, card: dict) -> list[dict]:
    """The standard questions for a target card, by its model (exporter is the default)."""
    return pattern.get(TARGET_MODELS.get(card.get("model", "exporter"), ""), []) or []


# ---------- versions ----------
# Optional signal field (catalog/targets/*.yaml):
#   versions: {min: "3.6", max: "4.0", note: "...", refs: [ver-...]}
# The signal's metrics exist only for versions v with  min <= v < max  (min inclusive, max exclusive: `max` is the
# first version that no longer has them). At least one of min / max; note and refs optional (refs must exist).
# Only for a signal that is version-bound as a whole; when one rule inside a signal is, the rule carries the
# annotation `alert_trust_min_version: "x.y"` (same comparison, inclusive) instead.
# The versions are the database / server version (MySQL, PostgreSQL, etcd...), not the exporter's.
VERSION_KEYS = {"min", "max", "note", "refs"}
RULE_MIN_VERSION = "alert_trust_min_version"
_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")


def parse_version(v) -> tuple[int, ...] | None:
    """"8.4.13", "v3.6.16", "2024.2.3.0", "26.8.3.1", "11.4.13-MariaDB", "PostgreSQL 16.4" -> int tuple;
    None when there is no number (unknown, empty, None)."""
    if v is None or isinstance(v, bool):
        return None
    m = _VERSION_RE.search(str(v).strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def compare_versions(a, b) -> int:
    """-1 / 0 / 1; missing trailing parts count as 0 ("3.6" == "3.6.0", "3.6" < "3.6.16")."""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        raise ValueError(f"版本無法比較：{a!r}、{b!r}")
    n = max(len(pa), len(pb))
    pa, pb = pa + (0,) * (n - len(pa)), pb + (0,) * (n - len(pb))
    return (pa > pb) - (pa < pb)


def version_in_range(v, versions: dict) -> bool | None:
    """True / False for a known version, None when the version cannot be parsed."""
    if parse_version(v) is None:
        return None
    if versions.get("min") is not None and compare_versions(v, versions["min"]) < 0:
        return False
    if versions.get("max") is not None and compare_versions(v, versions["max"]) >= 0:
        return False
    return True


def version_range_text(versions: dict) -> str:
    """'≥ 3.6', '< 4.0', '≥ 3.6、< 4.0'."""
    parts = []
    if versions.get("min") is not None:
        parts.append(f"≥ {versions['min']}")
    if versions.get("max") is not None:
        parts.append(f"< {versions['max']}")
    return "、".join(parts)


def check_versions_field(signal: dict, refs: dict | None = None) -> list[str]:
    """Shape of `versions` on a signal; refs must exist when `refs` (the loaded refs) is given."""
    if "versions" not in signal:
        return []
    v = signal["versions"]
    if not isinstance(v, dict):
        return ["versions 要是 {min, max, note, refs}"]
    errs = [f"versions 不認得 {k!r}（只有 min、max、note、refs）" for k in v if k not in VERSION_KEYS]
    if v.get("min") is None and v.get("max") is None:
        errs.append("versions 至少要有 min 或 max")
    for k in ("min", "max"):
        if v.get(k) is not None and (not isinstance(v[k], str) or parse_version(v[k]) is None):
            errs.append(f"versions.{k} 要是加引號的版本字串（例如 \"3.6\"），不是 {v[k]!r}")
    if not errs and v.get("min") is not None and v.get("max") is not None and compare_versions(v["min"], v["max"]) >= 0:
        errs.append("versions.min 要小於 max（max 不含）")
    if "note" in v and not (isinstance(v["note"], str) and v["note"].strip()):
        errs.append("versions.note 要是非空字串")
    if "refs" in v:
        if not isinstance(v["refs"], list) or not all(isinstance(r, str) for r in v["refs"]):
            errs.append("versions.refs 要是 ref key 清單")
        elif refs is not None:
            errs += [f"versions.refs 裡的 {r} 不存在" for r in v["refs"] if r not in refs]
    return errs
