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
    within = {"prometheus": 0, "vmagent": 0, "victoriametrics": 1, "victorialogs": 2, "vmalert": 3, "alertmanager": 0, "observer": 1}
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


def runbook_url(component_id: str, signal_id: str) -> str:
    return f"{SITE_URL}/components/{component_id}/#{signal_id}"
