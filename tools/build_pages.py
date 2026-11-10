#!/usr/bin/env python3
"""Generate site pages from the catalog.

  python3 tools/build_pages.py        # writes docs/components/*.md and docs/reference/sources.md

Generated files are not committed (see .gitignore); CI runs this before `mkdocs build`.
Each signal becomes a heading whose anchor is the signal id, so rule runbook_url links stay stable.
"""
from __future__ import annotations

import os
import re
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


_NAME = None


def codeify(text: str, extra=()) -> str:
    """Put backticks around metric names and glossary tokens so the hook can link them."""
    global _NAME
    if _NAME is None:
        names = {tok for t in C.load_terms().values() for tok in t.get("tokens", [])}
        names |= {m for c in C.load_components() for s in c.get("signals", []) for m in s.get("metrics", [])}
        names = {n for n in names if re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:.-]*", n) and ("_" in n or n.startswith("-"))}
        _NAME = re.compile(r"(?<![`\w.-])(" + "|".join(sorted(map(re.escape, names), key=len, reverse=True)) + r")(?![`\w])")
    parts = re.split(r"(`[^`]*`)", str(text))
    return "".join(p if p.startswith("`") else _NAME.sub(r"`\1`", p) for p in parts)


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
            out.append(f"- {codeify(sf['text'].strip())} {ev(sf['evidence'])}{marks(sf)}")
        out.append("")

    if card.get("contracts"):
        out.append("## 信任契約\n")
        for cid in card["contracts"]:
            c = contracts[cid]
            out.append(f"- [`{c['expr']}`](../contracts.md#{cid})。違反時：{c['violation']}{marks(c)}")
        out.append("")

    signals = card.get("signals", [])
    use_aspect = any(s.get("aspect") for s in signals)
    out.append("## 訊號總覽\n")
    out.append("| 級 |" + (" 面向 |" if use_aspect else "") + " 要回答的問題 | 在哪裡評估 | 上游 | 規則 |")
    out.append("|---|" + ("---|" if use_aspect else "") + "---|---|---|---|")
    for s in signals:
        n = len(s.get("rules", []))
        asp = f" {C.ASPECTS.get(s.get('aspect'), '—')} |" if use_aspect else ""
        out.append(f"| {chip(str(s['tier']), 't' + str(s['tier']))} |{asp} [{esc(s['question'])}](#{s['id']}) | "
                   f"{C.EVALUATE_ON[s['evaluate_on']]} | {up(s['upstream']['status'])} | {n if n else '—'} |")
    out.append("")

    if card.get("blind_spots"):
        out.append("## 目前無法監控的範圍\n")
        out.append("| 範圍 | 類型 | 為什麼 | 補法 | 已補上 |\n|---|---|---|---|---|")
        for b in card["blind_spots"]:
            link = f"[{esc(b['what'])}](../{b['section']})" if b.get("section") else esc(b["what"])
            cov = b.get("covered_by")
            done = (f"{esc(cov['tool'])}：" + "、".join(f"`{m}`" for m in cov["metrics"]) + marks(cov)) if cov else "—"
            out.append(f"| {link} | {C.BLIND_CATEGORIES[b['category']]} | {esc(b.get('why', ''))} | {esc(b['workaround'])} | {done} |")
        out.append("")

    na_asp = card.get("not_applicable_aspects") or {}
    if na_asp:
        out.append("## 不適用的面向\n")
        for asp, r in na_asp.items():
            out.append(f"- **{C.ASPECTS.get(asp, asp)}**：{codeify(r.get('why', ''))}{marks(r)}")
        out.append("")

    if use_aspect:
        sections = [(C.ASPECTS[a], sorted([s for s in signals if s.get("aspect") == a], key=lambda s: s["tier"]))
                    for a in C.ASPECTS]
        sections.append(("其他", [s for s in signals if s.get("aspect") not in C.ASPECTS]))
    else:
        sections = [(TIER_TITLE[t], [s for s in signals if s["tier"] == t]) for t in (1, 2, 3)]
    for title, group in sections:
        if not group:
            continue
        out.append(f"## {title}\n")
        for s in group:
            out.append(f"### {s['question']} {{#{s['id']}}}\n")
            rows = [("級別", chip(str(s["tier"]), "t" + str(s["tier"])) + " " + TIER_TITLE[s["tier"]].split("：")[1]),
                    ("訊號", esc(codeify(s["signal"])))]
            if s.get("metrics"):
                rows.append(("Metric", "、".join(f"`{m}`" for m in s["metrics"])))
            where = C.EVALUATE_ON[s["evaluate_on"]]
            if s.get("judged_by") == "external":
                where += "；由外部判斷「沒收到」"
            rows.append(("在哪裡評估", where))
            if s.get("applies_when"):
                rows.append(("適用條件", esc(s["applies_when"])))
            if s.get("silent_failure"):
                rows.append(("為什麼需要", esc(codeify(s["silent_failure"]))))
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
        pattern = C._load(pattern_path)
        out.append("## 每張 target 卡都要回答的問題\n")
        out.append("`tools/validate.py` 會檢查每張 target 卡都有這些訊號，或寫明不適用的理由。卡片的 `model` 決定用哪一組。\n")
        for key, title in (("required_signals", "透過 exporter（`model: exporter`）"),
                           ("required_signals_native", "資料庫自己提供 /metrics（`model: native`）")):
            if not pattern.get(key):
                continue
            out.append(f"### {title}\n")
            out.append("| 訊號 | 問題 | 至少 | 為什麼 |\n|---|---|---|---|")
            for r in pattern[key]:
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


def terms_page(terms: dict) -> str:
    kinds = {"config": "設定", "metric": "Metric"}
    out = [GENERATED, "# 名詞：設定與 metric\n",
           "文件裡的 interval、耗時、offset 各自指哪個設定、從哪個 metric 看實際值。"
           "其他頁面用反引號寫出這些名稱時，會自動連到這裡。\n",
           "| 名詞 | 符號 | 類型 | 適用 | 預設 |", "|---|---|---|---|---|"]
    for key, t in terms.items():
        out.append(f"| [{t['title']}](#{C.term_anchor(key)}) | {t.get('symbol', '')} | {kinds[t['kind']]} | "
                   f"{'、'.join(t.get('systems', []))} | {esc(t.get('default', ''))} |")
    out.append("")
    for kind, title in (("config", "設定"), ("metric", "Metric")):
        out.append(f"## {title}\n")
        for key, t in terms.items():
            if t["kind"] != kind:
                continue
            out.append(f"### {t['title']} {{#{C.term_anchor(key)}}}\n")
            rows = []
            if t.get("symbol"):
                rows.append(("符號", f"`{t['symbol']}`，見[信任契約](../contracts.md)"))
            rows.append(("名稱", "、".join(f"`{x}`" for x in t.get("tokens", [key]))))
            rows.append(("在哪裡" if kind == "config" else "來源", esc(t.get("where", ""))))
            if t.get("default"):
                rows.append(("預設", esc(t["default"])))
            for i, o in enumerate(t.get("observe", []) or []):
                rows.append(("怎麼看" if i == 0 else "", esc(o)))
            if t.get("note"):
                rows.append(("注意", esc(t["note"])))
            rows.append(("依據", "".join(f"[^{r}]" for r in t.get("refs", []))))
            out.append("| | |\n|---|---|")
            out += [f"| {k} | {v} |" for k, v in rows]
            out.append("")
    return "\n".join(out)


DB_PAGES = {"mysqld_exporter": ("MySQL／MariaDB", "mysql.md"), "postgres_exporter": ("PostgreSQL", "postgres.md"),
            "redis_exporter": ("Valkey／Redis", "valkey.md"), "etcd": ("etcd", "etcd.md"), "tidb": ("TiDB", "tidb.md")}


def targets_overview(cards: list[dict]) -> str:
    targets = [c for c in cards if c["kind"] == "target"]
    name = lambda c: DB_PAGES.get(c["id"], (c["name"], None))[0]  # noqa: E731
    out = [GENERATED, "# 資料庫總覽\n",
           "每個資料庫 target 都用同一組面向檢查，從大到小：叢集 → 複寫 → 實例 → 資源 → 工作量 → 單一查詢，"
           "再加上維護、保護與容量。每個 target 也都列出**目前無法監控的範圍**。\n",
           "## 面向覆蓋\n",
           "數字是該面向的訊號數；「不適用」附理由，見各自的元件卡。\n",
           "| 面向 | " + " | ".join(name(c) for c in targets) + " |",
           "|---|" + "---|" * len(targets)]
    for asp, title in C.ASPECTS.items():
        row = []
        for c in targets:
            n = sum(1 for s in c["signals"] if s.get("aspect") == asp)
            na = (c.get("not_applicable_aspects") or {}).get(asp)
            row.append(f"{n}" + ("（另有不適用說明）" if na else "") if n else ("不適用" if na else "—"))
        out.append(f"| {title.split('：')[0]} | " + " | ".join(row) + " |")
    out.append("")
    out.append("## 目前無法監控的範圍\n")
    out.append("分成三類：資料庫有但 exporter 沒輸出（補一個自訂查詢就看得到）、資料庫本身沒提供（只能靠 log 或外部工具）、要靠其他元件（node_exporter、備份工具、HA 管理工具）。\n")
    for c in targets:
        out.append(f"### {name(c)}\n")
        for cat, ctitle in C.BLIND_CATEGORIES.items():
            items = [b for b in c.get("blind_spots", []) if b["category"] == cat]
            if not items:
                continue
            out.append(f"**{ctitle}**\n")
            for b in items:
                link = f"[{b['what']}](../{b['section']})" if b.get("section") else b["what"]
                cov = b.get("covered_by")
                done = f" **已用 {cov['tool']} 補上**：" + "、".join(f"`{m}`" for m in cov["metrics"]) if cov else ""
                out.append(f"- {link}：{b.get('why', '')}。補法：{b['workaround']}{done}")
            out.append("")
    out.append("## 每個資料庫的頁面\n")
    for c in targets:
        title, page = DB_PAGES.get(c["id"], (c["name"], None))
        links = [f"[怎麼監控]({page})"] if page else []
        links.append(f"[監控訊號（元件卡）](../components/{c['id']}.md)")
        out.append(f"- **{title}**：" + "、".join(links))
    return "\n".join(out) + "\n"


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
    with open(os.path.join(C.ROOT, "docs", "targets", "index.md"), "w", encoding="utf-8") as f:
        f.write(targets_overview(cards))
    with open(os.path.join(ref_dir, "terms.md"), "w", encoding="utf-8") as f:
        f.write(terms_page(C.load_terms()))
    print(f"wrote {len(cards)} component pages, index, sources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
