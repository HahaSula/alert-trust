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
        if card.get("family") not in C.FAMILIES:
            errors.append(f"{where}: family {card.get('family')!r} 要是 {', '.join(C.FAMILIES)} 之一")
        if card.get("architecture") and not os.path.exists(os.path.join(root, "docs", card["architecture"])):
            errors.append(f"{where}: architecture 頁 {card['architecture']} 不存在")
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

    errors += check_target_pattern(components, root)
    ref_errors, missing_refs = check_refs(components, contracts, root)
    errors += ref_errors
    if missing_refs:
        warnings.append(f"{len(missing_refs)} 個項目標了實測或讀碼，但還沒有 refs（用 --refs 列出）")
    MISSING_REFS[:] = missing_refs
    presence_errors, presence_warnings = check_presence(rules)
    errors += presence_errors
    warnings += presence_warnings
    return errors, warnings


MISSING_REFS: list[str] = []
DOC_MARK = re.compile(r"\[\^([a-z0-9][a-z0-9-]*)\](?!:)")


def check_refs(components: list[dict], contracts: dict, root: str) -> tuple[list[str], list[str]]:
    """Every [^key] in docs and every refs: [key] in the catalog must resolve; list items that lack refs."""
    refs = C.load_refs(root)
    sources = C.load_sources(root)
    errors, missing = [], []
    for key, r in refs.items():
        if r.get("kind") not in ("code", "experiment"):
            errors.append(f"refs: {key} 的 kind 要是 code 或 experiment")
        if r.get("kind") == "experiment" and not r.get("url") and r.get("visibility") != "private":
            errors.append(f"refs: {key} 沒有 url，也沒有標 visibility: private")
    docs = os.path.join(root, "docs")
    for dirpath, _, names in os.walk(docs):
        if os.path.relpath(dirpath, docs).startswith("components"):
            continue  # generated from the catalog, checked below
        for n in names:
            if n.endswith(".md"):
                path = os.path.join(dirpath, n)
                text = open(path, encoding="utf-8").read()
                for m in DOC_MARK.finditer(text):
                    if m.group(1) not in refs and f"[^{m.group(1)}]:" not in text:
                        errors.append(f"{os.path.relpath(path, root)}: 參考 [^{m.group(1)}] 不存在")

    def check(where: str, item: dict) -> None:
        for key in item.get("refs", []) or []:
            if key not in refs:
                errors.append(f"{where}: refs 裡的 {key} 不存在")
        if item.get("evidence") in ("tested", "code") and not item.get("refs"):
            src = sources.get(item.get("source"), {})
            # code read from a public upstream file already has a file-level link
            if not (item.get("evidence") == "code" and src.get("visibility") == "public" and src.get("url")):
                missing.append(where)

    for cid, c in contracts.items():
        check(f"contracts.yaml#{cid}", c)
    seen_tokens: dict[str, str] = {}
    for key, t in C.load_terms(root).items():
        if t.get("kind") not in ("config", "metric"):
            errors.append(f"terms.yaml: {key} 的 kind 要是 config 或 metric")
        if not t.get("refs"):
            errors.append(f"terms.yaml: {key} 沒有 refs")
        for r in t.get("refs", []) or []:
            if r not in refs:
                errors.append(f"terms.yaml: {key} 的 refs 裡的 {r} 不存在")
        for tok in t.get("tokens", []) or []:
            if tok in seen_tokens:
                errors.append(f"terms.yaml: token {tok!r} 同時屬於 {seen_tokens[tok]} 與 {key}")
            seen_tokens[tok] = key
    for card in components:
        for i, sf in enumerate(card.get("silent_failures", [])):
            check(f"{card['_path']}#silent_failures[{i}]", sf)
        for s in card.get("signals", []):
            check(f"{card['_path']}#{s['id']}", s)
    return errors, missing


def check_target_pattern(components: list[dict], root: str) -> list[str]:
    """Every target card answers the questions in catalog/target-pattern.yaml, or says why not."""
    path = os.path.join(root, "catalog", "target-pattern.yaml")
    if not os.path.exists(path):
        return []
    required = C._load(path)["required_signals"]
    errors = []
    for card in components:
        if card.get("kind") != "target":
            continue
        by_id = {s["id"]: s for s in card.get("signals", [])}
        na = card.get("not_applicable") or {}
        for req in required:
            rid = req["id"]
            if rid in na:
                if not str(na[rid]).strip():
                    errors.append(f"{card['_path']}: not_applicable.{rid} 要寫理由")
                continue
            if rid not in by_id:
                errors.append(f"{card['_path']}: 缺少 target 標準訊號 {rid}（{req['question']}），或在 not_applicable 寫理由")
            elif by_id[rid].get("tier", 9) > req["min_tier"]:
                errors.append(f"{card['_path']}#{rid}: 標準訊號至少要第 {req['min_tier']} 級")
            elif req.get("contract") and by_id[rid].get("contract") != req["contract"]:
                errors.append(f"{card['_path']}#{rid}: 要引用契約 {req['contract']}")
    return errors


# metric{...} == 0 / != 1 / < 1 written directly on a selector. When the series disappears the expression
# returns nothing, so the alert cannot fire exactly when the source is gone.
PRESENCE_RE = re.compile(
    r"(?<![\w:\])])([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(\{[^}]*\})?\s*(==\s*0|!=\s*1|<\s*1)(?![\d.])")
PRESENCE_SKIP = {"up", "bool", "by", "on", "without", "ignoring", "group_left", "group_right", "offset"}


def check_presence(rules: dict[str, dict]) -> tuple[list[str], list[str]]:
    """Lint: a 'value is bad' rule needs a companion that fires when the series is missing."""
    names = {ref.split("#", 1)[1] for ref in rules}
    errors, warnings = [], []
    for ref, rule in rules.items():
        expr = str(rule.get("expr", ""))
        hits = [m.group(1) for m in PRESENCE_RE.finditer(expr) if m.group(1) not in PRESENCE_SKIP]
        if not hits:
            continue
        if re.search(r"\babsent(_over_time)?\s*\(|\bunless\b|\bor\b", expr):
            continue
        covered = (rule.get("annotations") or {}).get("absence_covered_by")
        if not covered:
            warnings.append(f"{ref}: {', '.join(sorted(set(hits)))} 消失時這條規則回傳空；"
                            f"加上 absent／unless，或用 annotations.absence_covered_by 指出補位的規則")
            continue
        for alert in [a.strip() for a in str(covered).split(",") if a.strip()]:
            if alert not in names:
                errors.append(f"{ref}: absence_covered_by 指向不存在的規則 {alert}")
    return errors, warnings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="警告也視為失敗")
    ap.add_argument("--refs", action="store_true", help="列出還沒有 refs 的項目")
    a = ap.parse_args()
    errors, warnings = validate()
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    if a.refs:
        for m in MISSING_REFS:
            print(f"NOREF {m}")
    comps = C.load_components()
    n_sig = sum(len(c.get("signals", [])) for c in comps)
    print(f"\n{len(comps)} 張卡、{n_sig} 個訊號、{len(C.load_rules())} 條規則；{len(errors)} 個錯誤、{len(warnings)} 個警告")
    return 1 if errors or (a.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
