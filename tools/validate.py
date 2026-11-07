#!/usr/bin/env python3
"""Validate the catalog, the rule files and the links between them.

  python3 tools/validate.py            # errors exit 1, warnings are printed
  python3 tools/validate.py --strict   # warnings also exit 1

Checks: required fields and enums, unique ids, every source/contract/rule reference resolves,
every alerting rule has a runbook_url that points at an existing signal, every contract has an
anchor in docs/contracts.md.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog as C  # noqa: E402


def validate(root: str = C.ROOT) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    sources = C.load_sources(root)
    contracts = C.load_contracts(root)
    rules = C.load_rules(root)
    components = C.load_components(root)

    for cid, c in contracts.items():
        if not C.ID_RE.match(cid):
            errors.append(f"contracts.yaml: id {cid!r} 不是 kebab-case")
        if c.get("source") not in sources:
            errors.append(f"contracts.yaml: {cid} 的 source {c.get('source')!r} 不存在")
        if c.get("evidence") not in C.EVIDENCE:
            errors.append(f"contracts.yaml: {cid} 的 evidence {c.get('evidence')!r} 無效")
    contracts_doc = os.path.join(root, "docs", "contracts.md")
    if os.path.exists(contracts_doc):
        text = open(contracts_doc, encoding="utf-8").read()
        for cid in contracts:
            if "{#" + cid + "}" not in text:
                errors.append(f"docs/contracts.md: 缺少契約 {cid} 的錨點 {{#{cid}}}")

    signal_ids: dict[str, set[str]] = {}
    referenced_rules: set[str] = set()
    seen_components: set[str] = set()
    for card in components:
        where = card["_path"]
        for field in ("id", "name", "kind", "status", "stages", "role", "signals"):
            if field not in card:
                errors.append(f"{where}: 缺少 {field}")
        comp = card.get("id", "")
        if not C.COMPONENT_ID_RE.match(comp):
            errors.append(f"{where}: id {comp!r} 只能用小寫英數與底線")
        if os.path.splitext(os.path.basename(where))[0] != comp:
            errors.append(f"{where}: 檔名要和 id {comp!r} 相同")
        if comp in seen_components:
            errors.append(f"{where}: id {comp!r} 重複")
        seen_components.add(comp)
        if card.get("kind") not in C.KINDS:
            errors.append(f"{where}: kind {card.get('kind')!r} 無效")
        if card.get("status") not in C.STATUS:
            errors.append(f"{where}: status {card.get('status')!r} 無效")
        for ref in card.get("contracts", []):
            if ref not in contracts:
                errors.append(f"{where}: 契約 {ref!r} 不存在")
        for i, sf in enumerate(card.get("silent_failures", [])):
            if sf.get("evidence") not in C.EVIDENCE:
                errors.append(f"{where}: silent_failures[{i}] evidence 無效")
            if sf.get("source") not in sources:
                errors.append(f"{where}: silent_failures[{i}] source {sf.get('source')!r} 不存在")

        ids = signal_ids.setdefault(comp, set())
        for s in card.get("signals", []):
            sid = s.get("id", "")
            at = f"{where}#{sid}"
            if not C.ID_RE.match(sid):
                errors.append(f"{at}: signal id 要是 kebab-case")
            if sid in ids:
                errors.append(f"{at}: signal id 重複")
            ids.add(sid)
            for field in ("tier", "question", "signal", "metrics", "evaluate_on", "evidence", "source", "upstream", "rules"):
                if field not in s:
                    errors.append(f"{at}: 缺少 {field}")
            if s.get("tier") not in C.TIERS:
                errors.append(f"{at}: tier 要是 1、2 或 3")
            if s.get("evaluate_on") not in C.EVALUATE_ON:
                errors.append(f"{at}: evaluate_on {s.get('evaluate_on')!r} 無效")
            if s.get("evidence") not in C.EVIDENCE:
                errors.append(f"{at}: evidence {s.get('evidence')!r} 無效")
            if s.get("source") not in sources:
                errors.append(f"{at}: source {s.get('source')!r} 不存在")
            if (s.get("upstream") or {}).get("status") not in C.UPSTREAM:
                errors.append(f"{at}: upstream.status 要是 yes、partial 或 no")
            if s.get("contract") and s["contract"] not in contracts:
                errors.append(f"{at}: 契約 {s['contract']!r} 不存在")
            for ref in s.get("rules", []):
                referenced_rules.add(ref)
                if ref not in rules:
                    errors.append(f"{at}: 規則 {ref} 不存在")
            if (s.get("tier") == 1 and not s.get("rules") and s.get("evaluate_on") in ("observer", "federate")
                    and (s.get("upstream") or {}).get("status") != "yes"):
                warnings.append(f"{at}: 第 1 級訊號沒有規則範本，上游也沒有完整涵蓋")

    pattern = re.compile(re.escape(C.SITE_URL) + r"/components/([a-z0-9_]+)/#([a-z0-9-]+)$")
    for ref, rule in rules.items():
        url = (rule.get("annotations") or {}).get("runbook_url")
        if not url:
            errors.append(f"{ref}: 缺少 annotations.runbook_url")
            continue
        m = pattern.match(url)
        if not m:
            errors.append(f"{ref}: runbook_url 格式不對：{url}")
        elif m.group(2) not in signal_ids.get(m.group(1), set()):
            errors.append(f"{ref}: runbook_url 指向不存在的訊號 {m.group(1)}#{m.group(2)}")
        if ref not in referenced_rules:
            warnings.append(f"{ref}: 沒有任何訊號引用這條規則")
        tier = (rule.get("labels") or {}).get("tier")
        if tier not in ("1", "2", "3"):
            errors.append(f"{ref}: labels.tier 要是字串 \"1\"、\"2\" 或 \"3\"")
    return errors, warnings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="警告也視為失敗")
    a = ap.parse_args()
    errors, warnings = validate()
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    comps = C.load_components()
    n_sig = sum(len(c.get("signals", [])) for c in comps)
    print(f"\n{len(comps)} 張卡、{n_sig} 個訊號、{len(C.load_rules())} 條規則；{len(errors)} 個錯誤、{len(warnings)} 個警告")
    return 1 if errors or (a.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
