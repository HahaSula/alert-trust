#!/usr/bin/env python3
"""Generate site pages from the catalog.

  python3 tools/build_pages.py        # writes docs/components/*.md, docs/reference/*.md, docs/targets/index.md, catalog/map.yaml

Generated files are not committed (see .gitignore); CI runs this before `mkdocs build`.
Component pages follow the five sections (架構 #architecture, 正確使用 #usage, 正確觀測 #observe,
不正確使用時的觀測 #misuse, 目前無法觀測的痕跡 #blind). Each signal becomes a heading whose anchor is the
signal id, so rule runbook_url links stay stable. catalog/map.yaml is committed (skills read it).
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_map as M  # noqa: E402
import catalog as C  # noqa: E402

TIER_TITLE = {1: "第 1 級：基本 monitor of monitor", 2: "第 2 級：接近極限的預警", 3: "第 3 級：根因與容量"}
USAGE_CHIP = {"tested": "實測", "code": "讀碼", "doc": "文件"}
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


_RULES = None


def ver_chip(versions: dict | None) -> str:
    """「版本：≥ 3.6」 next to a version-bound signal (catalog `versions`)."""
    return f" {chip('版本：' + C.version_range_text(versions), 'ver')}" if versions else ""


def rule_link(ref: str) -> str:
    global _RULES
    if _RULES is None:
        _RULES = C.load_rules()
    path, alert = ref.split("#", 1)
    mv = ((_RULES.get(ref) or {}).get("annotations") or {}).get(C.RULE_MIN_VERSION)
    return f"[`{alert}`]({C.REPO_URL}/blob/main/{path})" + (f" {chip('版本：≥ ' + str(mv), 'ver')}" if mv else "")


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


def signal_detail(s: dict, sources: dict, contracts: dict, level: str = "####") -> list[str]:
    """One signal: a heading whose anchor is the signal id (runbook_url targets it) and a detail table."""
    out = [f"{level} {s['question']} {{#{s['id']}}}\n"]
    rows = [("級別", chip(str(s["tier"]), "t" + str(s["tier"])) + " " + TIER_TITLE[s["tier"]].split("：")[1]),
            ("訊號", esc(codeify(s["signal"])))]
    if s.get("metrics"):
        rows.append(("Metric", "、".join(f"`{m}`" for m in s["metrics"])))
    if s.get("key_metrics") or s.get("metrics_any"):
        need = [f"`{m}`" for m in s.get("key_metrics") or []] + \
            ["（" + " 或 ".join(f"`{m}`" for m in g) + "）" for g in s.get("metrics_any") or []]
        rows.append(("完整涵蓋要有", "、".join(need)))
    where = C.EVALUATE_ON[s["evaluate_on"]]
    if s.get("judged_by") == "external":
        where += "；由外部判斷「沒收到」"
    rows.append(("在哪裡評估", where))
    if s.get("applies_when"):
        rows.append(("適用條件", esc(s["applies_when"])))
    if s.get("versions"):
        v = s["versions"]
        rows.append(("版本", ver_chip(v).strip() + (f" {esc(codeify(v['note']))}" if v.get("note") else "")
                     + "".join(f"[^{k}]" for k in v.get("refs") or [])))
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
    return out


def signal_section(sec: str, signals: list[dict], use_aspect: bool, sources: dict, contracts: dict) -> list[str]:
    """Overview table plus details, grouped by aspect (target cards) or tier (monitoring cards)."""
    if not signals:
        return ["這張卡還沒有這一段的訊號。\n"]
    out = ["| 級 |" + (" 面向 |" if use_aspect else "") + " 要回答的問題 | 在哪裡評估 | 上游 | 規則 |",
           "|---|" + ("---|" if use_aspect else "") + "---|---|---|---|"]
    if use_aspect:
        groups = [(a, C.ASPECTS[a], sorted([s for s in signals if s.get("aspect") == a], key=lambda s: s["tier"]))
                  for a in C.ASPECTS]
        groups.append(("other", "其他", [s for s in signals if s.get("aspect") not in C.ASPECTS]))
    else:
        groups = [(f"tier-{t}", TIER_TITLE[t], [s for s in signals if s["tier"] == t]) for t in (1, 2, 3)]
    for _, _, group in groups:
        for s in group:
            n = len(s.get("rules", []))
            asp = f" {C.ASPECTS.get(s.get('aspect'), '—').split('：')[0]} |" if use_aspect else ""
            out.append(f"| {chip(str(s['tier']), 't' + str(s['tier']))} |{asp} [{esc(s['question'])}](#{s['id']})"
                       f"{ver_chip(s.get('versions'))} | {C.EVALUATE_ON[s['evaluate_on']]} | {up(s['upstream']['status'])} | "
                       f"{n if n else '—'} |")
    out.append("")
    for key, title, group in groups:
        if not group:
            continue
        out.append(f"### {title} {{#{sec}-{key}}}\n")
        for s in group:
            out += signal_detail(s, sources, contracts)
    return out


def component_page(card: dict, sources: dict, contracts: dict) -> str:
    """Five sections in order: 架構, 正確使用, 正確觀測, 不正確使用時的觀測, 目前無法觀測的痕跡."""
    title = {s["id"]: s["title"] for s in M.SECTIONS}
    out = [GENERATED, f"# {card['name']}\n"]
    meta = [chip("草稿" if card["status"] == "draft" else "穩定", f"st-{card['status']}"), C.FAMILIES[card["family"]]]
    if card.get("versions_checked"):
        meta.append("驗證版本：" + "、".join(f"`{v}`" for v in card["versions_checked"]))
    plugin = M.plugin_of(card["id"])
    if plugin:
        meta.append(f"plugin：`{plugin}`")
    out.append(" · ".join(meta) + "\n")

    signals = card.get("signals", [])
    by_sec = M.split_signals(card)
    nav = [(title["architecture"], "architecture", None), (title["usage"], "usage", len(card.get("usage") or [])),
           (title["observe"], "observe", len(by_sec["observe"])), (title["misuse"], "misuse", len(by_sec["misuse"])),
           (title["blind"], "blind", len(card.get("blind_spots") or []))]
    out.append("本頁依五段排列（[範圍與結構](../map.md)）：" + " → ".join(
        f"[{i + 1} {t}](#{a})" + (f"（{n}）" if n is not None else "") for i, (t, a, n) in enumerate(nav)) + "\n")

    # 1 架構
    out.append(f"## 1 {title['architecture']} {{#architecture}}\n")
    role = " ".join(card["role"].split())
    if card.get("architecture"):
        out.append(f"[{card['name']} 架構](../{card['architecture']})：{role}\n")
    else:
        out.append(f"還沒有架構頁。{role}\n")
    na_asp = card.get("not_applicable_aspects") or {}
    if na_asp:
        out.append("### 不適用的面向 {#not-applicable}\n")
        for asp, r in na_asp.items():
            out.append(f"- **{C.ASPECTS.get(asp, asp)}**：{codeify(r.get('why', ''))}{marks(r)}")
        out.append("")

    # 2 正確使用
    out.append(f"## 2 {title['usage']} {{#usage}}\n")
    usage = card.get("usage") or []
    if usage:
        out.append("只寫原始碼推理、本機小實驗或標明的官方文件。\n")
        out.append("| 項目 | 說明 | 證據 |\n|---|---|---|")
        for u in usage:
            out.append(f"| <span id=\"usage-{u['id']}\"></span>`{u['id']}` | {esc(codeify(' '.join(str(u['text']).split())))} | "
                       f"{chip(USAGE_CHIP[u['evidence']], 'ev-' + u['evidence'])}"
                       f"{marks(u)} |")
        out.append("")
    else:
        out.append("還沒有整理出有依據的設定。\n")
    if card.get("contracts"):
        out.append("### 信任契約 {#contracts}\n")
        for cid in card["contracts"]:
            c = contracts[cid]
            out.append(f"- [`{c['expr']}`](../contracts.md#{cid})。違反時：{c['violation']}{marks(c)}")
        out.append("")

    use_aspect = any(s.get("aspect") for s in signals)
    # 3 正確觀測
    out.append(f"## 3 {title['observe']} {{#observe}}\n")
    out.append("看到的數字是不是真的：存活、collector／endpoint 是否完整、metric 是否存在、量測語意、資料新鮮度。\n")
    if card.get("silent_failures"):
        out.append("### 會安靜壞掉的方式 {#silent-failures}\n")
        for sf in card["silent_failures"]:
            out.append(f"- {codeify(sf['text'].strip())} {ev(sf['evidence'])}{marks(sf)}")
        out.append("")
    out += signal_section("observe", by_sec["observe"], use_aspect, sources, contracts)

    # 4 不正確使用時的觀測
    out.append(f"## 4 {title['misuse']} {{#misuse}}\n")
    out.append("誤用或狀態異常時看得到什麼：飽和、容量、延遲過大、誤設定的後果。\n")
    out += signal_section("misuse", by_sec["misuse"], use_aspect, sources, contracts)
    if by_sec[None]:
        out.append("### 尚未分段的訊號 {#unsectioned}\n")
        for s in by_sec[None]:
            out += signal_detail(s, sources, contracts)
    out.append("### 寫得不好的查詢 {#bad-query-table}\n")
    bq = card.get("bad_queries") or []
    if bq:
        out.append("| 寫法 | 看得到的訊號 | 看得到什麼 | exporter 看不到 |\n|---|---|---|---|")
        for q in bq:
            sig = "、".join(f"[`{sid}`](#{sid})" for sid in q.get("signals") or [])
            out.append(f"| {esc(q.get('case', ''))}{marks(q)} | {sig} | {esc(codeify(' '.join(str(q.get('visible', '')).split())))} | "
                       f"{esc(codeify(' '.join(str(q.get('not_visible', '')).split())))} |")
        out.append("")
    else:
        out.append("還沒有整理（卡片的 `bad_queries`）。\n")

    # 5 目前無法觀測的痕跡
    out.append(f"## 5 {title['blind']} {{#blind}}\n")
    if card.get("blind_spots"):
        out.append("有痕跡但 exporter／endpoint 沒輸出。方案只是建議，點到為止。\n")
        out.append("| 範圍 | 類型 | 為什麼 | 方案 | 已補上 |\n|---|---|---|---|---|")
        for b in card["blind_spots"]:
            link = f"[{esc(b['what'])}](../{b['section']})" if b.get("section") else esc(b["what"])
            cov = b.get("covered_by")
            done = (f"{esc(cov['tool'])}：" + "、".join(f"`{m}`" for m in cov["metrics"]) + marks(cov)) if cov else "—"
            out.append(f"| {link} | {C.BLIND_CATEGORIES[b['category']]} | {esc(b.get('why', ''))} | "
                       f"{esc(b.get('options') or b.get('workaround', ''))} | {done} |")
        out.append("")
    else:
        out.append("還沒有列出。\n")
    out.append(f"\n<small>來源檔：[`{card['_path']}`]({C.REPO_URL}/blob/main/{card['_path']})</small>\n")
    return "\n".join(out)


def section_counts(card: dict) -> dict[str, int]:
    """How many items each of sections 2–5 has (bad_queries counted separately)."""
    sec = [s.get("section") for s in card.get("signals", [])]
    return {"usage": len(card.get("usage") or []), "observe": sec.count("observe"), "misuse": sec.count("misuse"),
            "bad_queries": len(card.get("bad_queries") or []), "blind": len(card.get("blind_spots") or [])}


def index_page(cards: list[dict]) -> str:
    out = [GENERATED, "# 元件卡\n",
           "每張卡回答同一個問題：這個元件壞掉時，誰會知道？第 1 級訊號是基本 monitor of monitor。"
           "每張卡都依[五段](../map.md)排列；右邊四欄是各段的項目數。\n"]
    for fam, title in C.FAMILIES.items():
        group = [c for c in cards if c.get("family") == fam]
        if not group:
            continue
        out.append(f"## {title}\n")
        out.append("| 卡片 | 狀態 | 第 1 級 | 第 2 級 | 第 3 級 | 上游沒有的第 1 級 | 正確使用 | 正確觀測 | 不正確使用 | 無法觀測 |")
        out.append("|---|---|---|---|---|---|---|---|---|---|")
        for c in group:
            sig = c.get("signals", [])
            cnt = [sum(1 for s in sig if s["tier"] == t) for t in (1, 2, 3)]
            gap = sum(1 for s in sig if s["tier"] == 1 and s["upstream"]["status"] == "no")
            status = chip("草稿" if c["status"] == "draft" else "穩定", f"st-{c['status']}")
            n = section_counts(c)
            sec = " | ".join(f"[{n[k]}]({c['id']}.md#{k})" for k in ("usage", "observe", "misuse", "blind"))
            out.append(f"| [{c['name']}]({c['id']}.md) | {status} | {cnt[0]} | {cnt[1]} | {cnt[2]} | {gap} | {sec} |")
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


DB_PAGES = {"mysql": ("MySQL", "mysql.md"), "mariadb": ("MariaDB", "mariadb.md"), "postgres_exporter": ("PostgreSQL", "postgres.md"),
            "redis_exporter": ("Valkey／Redis", "valkey.md"), "etcd": ("etcd", "etcd.md"), "tidb": ("TiDB", "tidb.md"),
            "yugabytedb": ("YugabyteDB", "yugabytedb.md"), "clickhouse": ("ClickHouse", "clickhouse.md"),
            "mongodb_exporter": ("MongoDB", "mongodb.md")}


# Cards that were split or renamed: the old id still gets a short page so old runbook_url links land somewhere.
# Every signal id of the new cards keeps its anchor on the old page and links to the same anchor on each new card.
MOVED = {"mysqld_exporter": {"title": "mysqld_exporter（已拆成 MySQL 與 MariaDB）", "to": ["mysql", "mariadb"],
                             "why": "MySQL 與 MariaDB 的原始碼與 metric 名稱不同，2026-11-10 起各自一張卡；規則也分成 "
                                    "`rules/targets/mysql.yml` 與 `rules/targets/mariadb.yml`。"}}


def moved_page(old_id: str, info: dict, cards: dict) -> str:
    new = [cards[c] for c in info["to"] if c in cards]
    out = [GENERATED, f"# {info['title']}\n", f"這張卡已拆成 " + "、".join(f"[{c['name']}]({c['id']}.md)" for c in new)
           + f"。{info['why']}\n", "舊的 runbook 連結（`components/" + old_id + "/#訊號`）會落在下面同名的段落，再點到新卡。\n"]
    seen: dict[str, list[dict]] = {}
    for c in new:
        for s in c.get("signals", []):
            seen.setdefault(s["id"], []).append(c)
    out.append("## 訊號在哪一張卡\n")
    for sid, cs in seen.items():
        q = next(s["question"] for s in cs[0]["signals"] if s["id"] == sid)
        out.append(f"### {esc(q)} {{#{sid}}}\n")
        out.append("、".join(f"[{c['name']}]({c['id']}.md#{sid})" for c in cs) + "\n")
    return "\n".join(out) + "\n"


def targets_overview(cards: list[dict]) -> str:
    targets = [c for c in cards if c["kind"] == "target"]
    name = lambda c: DB_PAGES.get(c["id"], (c["name"], None))[0]  # noqa: E731
    out = [GENERATED, "# 資料庫總覽\n",
           "每個資料庫 target 都用同一組面向檢查，從大到小：叢集 → 複寫 → 實例 → 資源 → 工作量 → 單一查詢，"
           "再加上維護、保護與容量。每個 target 也都列出**目前無法監控的範圍**。\n",
           "## 五段的項目數 {#sections}\n",
           "每張卡依[五段框架](../map.md)排列，數字連到元件卡的那一段。第 1 段（架構）是一個連結，不計數。\n",
           "| 資料庫 | 2 正確使用 | 3 正確觀測 | 4 不正確使用時的觀測 | 其中寫得不好的查詢 | 5 目前無法觀測的痕跡 |",
           "|---|---|---|---|---|---|",
           *[f"| {name(c)} | " + " | ".join(
               f"[{section_counts(c)[k]}](../components/{c['id']}.md#{a})"
               for k, a in (("usage", "usage"), ("observe", "observe"), ("misuse", "misuse"),
                            ("bad_queries", "bad-query-table"), ("blind", "blind"))) + " |" for c in targets],
           "",
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
    by_id = {c["id"]: c for c in cards}
    for old_id, info in MOVED.items():
        if old_id not in by_id:
            with open(os.path.join(comp_dir, f"{old_id}.md"), "w", encoding="utf-8") as f:
                f.write(moved_page(old_id, info, by_id))
    with open(os.path.join(comp_dir, "index.md"), "w", encoding="utf-8") as f:
        f.write(index_page(cards))
    with open(os.path.join(ref_dir, "sources.md"), "w", encoding="utf-8") as f:
        f.write(sources_page(sources))
    with open(os.path.join(C.ROOT, "docs", "targets", "index.md"), "w", encoding="utf-8") as f:
        f.write(targets_overview(cards))
    with open(os.path.join(ref_dir, "terms.md"), "w", encoding="utf-8") as f:
        f.write(terms_page(C.load_terms()))
    M.write_map(cards)
    print(f"wrote {len(cards)} component pages, index, sources, catalog/map.yaml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
