#!/usr/bin/env python3
"""Generate site pages from the catalog.

  python3 tools/build_pages.py        # writes docs/components/*.md and docs/reference/sources.md

Generated files are not committed (see .gitignore); CI runs this before `mkdocs build`.
Each signal becomes a heading whose anchor is the signal id, so rule runbook_url links stay stable.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog as C  # noqa: E402

TIER_TITLE = {1: "第 1 級：基本 monitor of monitor", 2: "第 2 級：接近極限的預警", 3: "第 3 級：根因與容量"}
GENERATED = "<!-- 由 tools/build_pages.py 產生，請改 catalog/ 下的 YAML -->\n"


def chip(text: str, cls: str) -> str:
    return f'<span class="at-chip at-{cls}">{text}</span>'


def ev(e: str) -> str:
    return chip(C.EVIDENCE[e], f"ev-{e}")


def up(s: str) -> str:
    return chip(C.UPSTREAM[s], f"up-{s}")


def src(key: str, sources: dict) -> str:
    s = sources[key]
    if s.get("visibility") == "public" and s.get("url"):
        return f"[{s['title']}]({s['url']})"
    return f"{s['title']}（未公開）"


def marks(item: dict) -> str:
    """Footnote markers; hooks/refs.py appends the citations."""
    return "".join(f"[^{k}]" for k in item.get("refs", []) or [])


def rule_link(ref: str) -> str:
    path, alert = ref.split("#", 1)
    return f"[`{alert}`]({C.REPO_URL}/blob/main/{path})"


def esc(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def component_page(card: dict, sources: dict, contracts: dict) -> str:
    out = [GENERATED, f"# {card['name']}\n"]
    meta = [chip("草稿" if card["status"] == "draft" else "穩定", f"st-{card['status']}"), C.FAMILIES[card["family"]]]
    if card.get("versions_checked"):
        meta.append("驗證版本：" + "、".join(f"`{v}`" for v in card["versions_checked"]))
    out.append(" · ".join(meta) + "\n")
    out.append(card["role"].strip() + "\n")
    if card.get("architecture"):
        out.append(f"架構與執行模型見 [{card['name']} 架構](../{card['architecture']})。\n")

    if card.get("silent_failures"):
        out.append("## 會安靜壞掉的方式\n")
        for sf in card["silent_failures"]:
            out.append(f"- {sf['text'].strip()} {ev(sf['evidence'])}{marks(sf)}")
        out.append("")

    if card.get("contracts"):
        out.append("## 信任契約\n")
        for cid in card["contracts"]:
            c = contracts[cid]
            out.append(f"- [`{c['expr']}`](../contracts.md#{cid})。違反時：{c['violation']}{marks(c)}")
        out.append("")

    signals = card.get("signals", [])
    out.append("## 訊號總覽\n")
    out.append("| 級 | 要回答的問題 | 在哪裡評估 | 上游 | 規則 |")
    out.append("|---|---|---|---|---|")
    for s in signals:
        n = len(s.get("rules", []))
        out.append(f"| {chip(str(s['tier']), 't' + str(s['tier']))} | [{esc(s['question'])}](#{s['id']}) | "
                   f"{C.EVALUATE_ON[s['evaluate_on']]} | {up(s['upstream']['status'])} | {n if n else '—'} |")
    out.append("")

    for tier in (1, 2, 3):
        group = [s for s in signals if s["tier"] == tier]
        if not group:
            continue
        out.append(f"## {TIER_TITLE[tier]}\n")
        for s in group:
            out.append(f"### {s['question']} {{#{s['id']}}}\n")
            rows = [("訊號", esc(s["signal"]))]
            if s.get("metrics"):
                rows.append(("Metric", "、".join(f"`{m}`" for m in s["metrics"])))
            where = C.EVALUATE_ON[s["evaluate_on"]]
            if s.get("judged_by") == "external":
                where += "；由外部判斷「沒收到」"
            rows.append(("在哪裡評估", where))
            if s.get("applies_when"):
                rows.append(("適用條件", esc(s["applies_when"])))
            if s.get("silent_failure"):
                rows.append(("為什麼需要", esc(s["silent_failure"])))
            rows.append(("證據", f"{ev(s['evidence'])} {src(s['source'], sources)}{marks(s)}"))
            note = s["upstream"].get("note")
            rows.append(("上游規則", up(s["upstream"]["status"]) + (f" {esc(note)}" if note else "")))
            if s.get("contract"):
                c = contracts[s["contract"]]
                rows.append(("契約", f"[`{c['expr']}`](../contracts.md#{s['contract']})"))
            rows.append(("規則範本", "、".join(rule_link(r) for r in s["rules"]) if s.get("rules") else "尚無"))
            out.append("| | |\n|---|---|")
            out += [f"| {k} | {v} |" for k, v in rows]
            out.append("")
    out.append(f"\n<small>來源檔：[`{card['_path']}`]({C.REPO_URL}/blob/main/{card['_path']})</small>\n")
    return "\n".join(out)


def index_page(cards: list[dict]) -> str:
    out = [GENERATED, "# 元件卡\n",
           "每張卡回答同一個問題：這個元件壞掉時，誰會知道？第 1 級訊號是基本 monitor of monitor。\n"]
    for fam, title in C.FAMILIES.items():
        group = [c for c in cards if c.get("family") == fam]
        if not group:
            continue
        out.append(f"## {title}\n")
        out.append("| 卡片 | 狀態 | 第 1 級 | 第 2 級 | 第 3 級 | 上游沒有的第 1 級 |")
        out.append("|---|---|---|---|---|---|")
        for c in group:
            sig = c.get("signals", [])
            cnt = [sum(1 for s in sig if s["tier"] == t) for t in (1, 2, 3)]
            gap = sum(1 for s in sig if s["tier"] == 1 and s["upstream"]["status"] == "no")
            status = chip("草稿" if c["status"] == "draft" else "穩定", f"st-{c['status']}")
            out.append(f"| [{c['name']}]({c['id']}.md) | {status} | {cnt[0]} | {cnt[1]} | {cnt[2]} | {gap} |")
        out.append("")
    pattern_path = os.path.join(C.ROOT, "catalog", "target-pattern.yaml")
    if os.path.exists(pattern_path):
        out.append("## 每張 target 卡都要回答的問題\n")
        out.append("任何 exporter 都適用。`tools/validate.py` 會檢查每張 target 卡都有這些訊號，或寫明不適用的理由。\n")
        out.append("| 訊號 | 問題 | 至少 | 為什麼 |\n|---|---|---|---|")
        for r in C._load(pattern_path)["required_signals"]:
            out.append(f"| `{r['id']}` | {esc(r['question'])} | {chip(str(r['min_tier']), 't' + str(r['min_tier']))} | {esc(r['why'])} |")
        out.append("")
    return "\n".join(out)


def sources_page(sources: dict) -> str:
    kinds = {"experiment": "自己的實驗", "upstream-doc": "上游文件", "upstream-rules": "上游規則",
             "upstream-code": "上游原始碼", "community-rules": "社群規則", "community-doc": "社群文件", "tool": "工具"}
    out = [GENERATED, "# 來源\n", "目錄裡每個訊號、契約與失效方式都標了來源。標「未公開」的是還沒整理成公開報告的實驗。\n"]
    for kind, title in kinds.items():
        group = {k: v for k, v in sources.items() if v.get("kind") == kind}
        if not group:
            continue
        out.append(f"## {title}\n")
        out.append("| Key | 來源 | 確認時間 | 備註 |\n|---|---|---|---|")
        for k, v in group.items():
            out.append(f"| `{k}` | {src(k, sources)} | {esc(v.get('checked', ''))} | {esc(v.get('note', ''))} |")
        out.append("")
    return "\n".join(out)


def main() -> int:
    sources, contracts, cards = C.load_sources(), C.load_contracts(), C.load_components()
    comp_dir = os.path.join(C.ROOT, "docs", "components")
    ref_dir = os.path.join(C.ROOT, "docs", "reference")
    os.makedirs(comp_dir, exist_ok=True)
    os.makedirs(ref_dir, exist_ok=True)
    for card in cards:
        with open(os.path.join(comp_dir, f"{card['id']}.md"), "w", encoding="utf-8") as f:
            f.write(component_page(card, sources, contracts))
    with open(os.path.join(comp_dir, "index.md"), "w", encoding="utf-8") as f:
        f.write(index_page(cards))
    with open(os.path.join(ref_dir, "sources.md"), "w", encoding="utf-8") as f:
        f.write(sources_page(sources))
    print(f"wrote {len(cards)} component pages, index, sources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
