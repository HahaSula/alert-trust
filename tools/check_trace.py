#!/usr/bin/env python3
"""Report-only trace of gaps per catalog card (catalog/components, catalog/targets). Never fails.

  python3 tools/check_trace.py                 # count table + every item
  python3 tools/check_trace.py --card mysql    # one card
  python3 tools/check_trace.py --summary       # count table only (CI step summary)
  python3 tools/check_trace.py --json out.json
  python3 tools/check_trace.py --write-baseline   # rewrite catalog/trace-baseline.json (the ratchet; deliberate only)

Gap classes (counts and lists only, no percentages):
  G1     misuse signal that no failure mode points at (detected_by.signals). Only for cards that have `mechanisms`
         or `failure_modes` (P1; other cards count 0 and say so).
  G2     failure mode with no signal: accounted for only by `accepted`, or by blind spots none of which has `options`.
  G3     signal with no rules and upstream.status no / missing (「有訊號、沒偵測」).
         G3.interview: the same, but evaluate_on is interview / inventory / external (問架構、盤點時檢查、外部服務:
         answered by asking or reading config, never by a rule on a ruler, as in check_coverage.py). Listed
         separately and not counted as a detection gap.
  G4     metric existence: names a card's signals list (`metrics`, which contains `key_metrics` / `metrics_any`)
         plus the selectors of the PromQL rules those signals reference, against the metric-name lists saved in lab/:
           confirmed    in a saved list of that card, with the versions (lists) it was seen in; lists of different
                        versions are not merged: a name seen only in one version says so
           other_source produced by another exporter the card also uses (query-exporter queries_* / custom-query
                        prefixes mysql_q_ / pgq_ / chq_, patroni_*, pgbackrest_*, blackbox probe_*, OTHER_SOURCE
                        below): the card's own lists cannot hold it
           not_in_list  the card has saved lists but the name is not in them (may be a counter that appears only after
                        an event, or a partial list: check by hand)
           no_list      no saved list for this card
         A list is partial when it was saved with a name-prefix filter (every name starts with one prefix, as the
         lab run.sh `__name__=~"mysql_.*"` queries do), or without the side exporters the card uses (the card has
         other_source names), or is named in PARTIAL_LISTS.
         Names produced by the scraper or other exporters (check_metrics.EXTERNAL: up, scrape_*, ALERTS*, node_*...)
         and recording-rule outputs (names with ':') are counted as external and not checked.
  G5     signal with rules whose evidence is not tested.
  G6     blind_spots with category database (元件本身沒提供).
  G7     item with evidence tested (signal, usage, silent failure, failure mode ...) and no `tested_by`: it cites an
         experiment as a whole, not the hypothesis that showed it (P2). Counted for every card; the items are listed
         only for cards that already use tested_by.
Saved lists (SAVED_LISTS below): lab/targets/<dir>/**/names-*.txt (one name per line) and
lab/targets/mysql/results/metric-names-*.txt (a Python list, MariaDB 11.4 per its compose.yml); the lab directory maps
to a card through LAB_DIR_CARD. lab/metric-names/results/prom-*.json holds check_metrics.py results; the metrics it
marks `found` count as a saved list for that component (the matching .txt is only the summary line).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog as C  # noqa: E402
import check_metric_sources as MS  # noqa: E402
import check_metrics as CM  # noqa: E402

GAPS = ("G1", "G2", "G3", "G4", "G5", "G6", "G7")
G4_CLASSES = ("confirmed", "not_in_list", "no_list")
G4_ALL = ("confirmed", "other_source", "not_in_list", "no_list", "external")
G4_LABEL = {"confirmed": "已確認（附看到它的版本）", "other_source": "其他 exporter 產生（卡片自己的清單不會有）",
            "not_in_list": "不在已存清單（可能是事件後才出現的 counter，或清單不完整，要人工確認）",
            "no_list": "這張卡沒有已存清單", "external": "外部或 recording rule（不檢查）"}
# evaluate_on values answered by asking / reading config / an external service, not by a rule (check_coverage.py)
INTERVIEW_TYPES = ("interview", "inventory", "external")
# names another exporter produces: (regex, source). Card-specific entries: {card: [(regex, source)]}
OTHER_SOURCE = [
    (r"^(queries_|query_(timestamp|interval|latency)|database_errors)", "query-exporter 本身"),
    (r"^(mysql_q_|pgq_|chq_)", "query-exporter 自訂查詢"),
    (r"^patroni_", "Patroni REST API /metrics"),
    (r"^pgbackrest_", "pgbackrest_exporter"),
    (r"^probe_", "blackbox_exporter"),
    (r"^namedprocess_", "process-exporter"),
]
OTHER_SOURCE_CARD = {"yugabytedb": [(r"^pg_", "postgres_exporter（接 YSQL）")]}
# explicit overrides: list file (repo-relative) -> why it is partial
PARTIAL_LISTS: dict[str, str] = {}
# list file stem -> version, where the file name has no version (from each lab's compose.yml / run.sh)
LIST_VERSION = {
    "names-my1": "MySQL 8.4.7", "names-my2": "MySQL 8.4.7", "names-mdb1": "MariaDB 11.4", "names-mdb2": "MariaDB 11.4",
    "metric-names-normal": "MariaDB 11.4", "metric-names-noprocess": "MariaDB 11.4",
    "names-e-p16": "PostgreSQL 16.15", "names-e-p17": "PostgreSQL 17.11",
    "names-postgres": "PostgreSQL 16", "names-postgres-nomon": "PostgreSQL 16",
    "names-e-vk1": "Valkey 9.1.3", "names-e-kg": "Valkey 9.1.3", "names-e-r72": "Redis 7.2.17", "names-e-r8": "Redis 8.10.3",
}
_VERSION = re.compile(r"\d+(?:\.\d+)+")
BASELINE = os.path.join("catalog", "trace-baseline.json")
# the classes the ratchet watches (tests/test_trace.py): a count above the baseline fails
RATCHET = ("G1", "G2", "G3", "G5", "G6", "G7", "G4.not_in_list", "G4.no_list")
UPDATE_BASELINE = "python3 tools/check_trace.py --write-baseline"
GAP_LABEL = {"G1": "misuse 訊號沒有任何 failure mode 指到", "G2": "failure mode 沒有訊號，只靠接受不偵測或沒有方案的 blind spot",
             "G3": "有訊號、沒偵測（沒有 rules，上游也沒有）", "G4": "metric 存在性",
             "G5": "有 rules，但 evidence 不是 tested", "G6": "blind_spots 是 database（元件本身沒提供）",
             "G7": "實測項目沒有指到實驗裡的假設（tested_by）"}
P1_NOTE = "G1/G2：只算有 mechanisms／failure_modes 的卡（P1 試點）；其他卡是 0，代表還沒寫，不代表沒有缺口"
# lab/targets/<dir> -> card id, when they differ
LAB_DIR_CARD = {"valkey": "redis_exporter", "postgres": "postgres_exporter", "mongodb": "mongodb_exporter"}
_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")


def _card_of_names_file(rel: str, cards: set[str]) -> str | None:
    parts = rel.split("/")
    if len(parts) < 3 or parts[0] != "lab" or parts[1] != "targets":
        return None
    d, base = parts[2], parts[-1]
    if d == "mysql":
        if base.startswith("names-mdb") or base.startswith("metric-names-"):
            return "mariadb"  # ha/names-mdb*: MariaDB 11.4; results/metric-names-*: compose.yml runs mariadb:11.4
        if base.startswith("names-my"):
            return "mysql"
        return None
    card = LAB_DIR_CARD.get(d, d)
    return card if card in cards else None


def list_version(rel: str) -> str:
    """Version label of one saved list: LIST_VERSION by file stem, else the version in the file name, else the stem."""
    stem = os.path.splitext(os.path.basename(rel))[0]
    if stem in LIST_VERSION:
        return LIST_VERSION[stem]
    m = _VERSION.search(stem)
    return m.group(0) if m else re.sub(r"^(metric-)?names-", "", stem)


def prefix_filter(names: set[str]) -> str | None:
    """`x_` when every name of a list starts with the same prefix (saved with a name filter), else None."""
    heads = {n.split("_", 1)[0] for n in names if "_" in n}
    if len(names) >= 5 and len(heads) == 1 and all("_" in n for n in names):
        return next(iter(heads)) + "_"
    return None


def other_source(card: str, metric: str) -> str | None:
    for pat, src in OTHER_SOURCE_CARD.get(card, []) + OTHER_SOURCE:
        if re.match(pat, metric):
            return src
    return None


def saved_lists(root: str, cards: set[str]) -> dict[str, dict]:
    """{card: {"names": set (union, for a quick test), "files": [rel paths], "lists": [{file, version, names,
    partial, why}]}} from the metric-name lists saved in lab/. Each list keeps its own version."""
    out: dict[str, dict] = {}

    def add(card: str, rel: str, names: set[str], filtered: bool = True) -> None:
        e = out.setdefault(card, {"names": set(), "files": [], "lists": []})
        e["names"] |= names
        e["files"].append(rel)
        why = []
        if rel in PARTIAL_LISTS:
            why.append(PARTIAL_LISTS[rel])
        pf = prefix_filter(names) if filtered else None  # check_metrics results hold only the card's own names
        if pf:
            why.append(f"只存了 `{pf}` 開頭的名稱")
        e["lists"].append({"file": rel, "version": list_version(rel), "names": names, "partial": bool(why), "why": why})

    paths = glob.glob(os.path.join(root, "lab", "targets", "**", "names-*.txt"), recursive=True)
    paths += glob.glob(os.path.join(root, "lab", "targets", "**", "metric-names-*.txt"), recursive=True)
    for path in sorted(paths):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        card = _card_of_names_file(rel, cards)
        if card is None:
            continue
        with open(path, encoding="utf-8") as f:
            names = set(_NAME.findall(f.read()))
        add(card, rel, names)
    for path in sorted(glob.glob(os.path.join(root, "lab", "metric-names", "results", "prom-*.json"))):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        by: dict[str, set] = {}
        for r in rows:
            if r.get("status") == "found" and r.get("component") in cards:
                by.setdefault(r["component"], set()).add(r["metric"])
        for card, names in by.items():
            add(card, rel, names, filtered=False)
    return out


def rule_metrics(root: str) -> dict[str, set[str]]:
    """{"rules/x.yml#Alert": {metric names in expr}} for PromQL rules (vlogs groups are skipped)."""
    out: dict[str, set[str]] = {}
    for path in C.rule_files(root):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        for group in (C._load(path) or {}).get("groups", []) or []:
            if group.get("type") not in (None, "prometheus"):
                continue
            for rule in group.get("rules", []) or []:
                if "alert" in rule:
                    out[f"{rel}#{rule['alert']}"] = {s["metric"] for s in MS.selectors(str(rule.get("expr", "")))
                                                     if s["metric"]}
    return out


def _external(m: str) -> bool:
    return bool(CM.EXTERNAL.match(m)) or ":" in m


def trace_card(card: dict, lists: dict, rmetrics: dict) -> dict:
    cid = card["id"]
    signals = card.get("signals", []) or []
    g3, g3i, g5 = [], [], []
    used: dict[str, set[str]] = {}  # metric -> where it is used
    for s in signals:
        sid = s.get("id")
        rules = s.get("rules") or []
        status = (s.get("upstream") or {}).get("status")
        if not rules and status in (None, "no", False):
            item = {"signal": sid, "tier": s.get("tier"), "upstream": status or "missing"}
            if s.get("evaluate_on") in INTERVIEW_TYPES:
                g3i.append(dict(item, evaluate_on=s.get("evaluate_on")))
            else:
                g3.append(item)
        if rules and s.get("evidence") != "tested":
            g5.append({"signal": sid, "tier": s.get("tier"), "evidence": s.get("evidence"), "rules": len(rules)})
        for m in s.get("metrics") or []:
            used.setdefault(m, set()).add(f"{sid}（metrics）")
        for r in rules:
            for m in rmetrics.get(r, ()):
                used.setdefault(m, set()).add(f"{sid}（{r}）")
    saved = lists.get(cid)
    g4: dict[str, list] = {k: [] for k in G4_ALL}
    lists = (saved or {}).get("lists") or []
    for m in sorted(used):
        item = {"metric": m, "used_by": sorted(used[m])}
        seen = [l["version"] for l in lists if CM.is_exposed(m, l["names"])]
        src = other_source(cid, m)
        if _external(m):
            cls = "external"
        elif seen:
            cls = "confirmed"
            item["versions"] = sorted(set(seen))
            if len(set(seen)) < len({l["version"] for l in lists}):
                item["not_in_versions"] = sorted({l["version"] for l in lists} - set(seen))
        elif src:
            cls = "other_source"
            item["source"] = src
        elif saved is None:
            cls = "no_list"
        else:
            cls = "not_in_list"
        g4[cls].append(item)
    side = sorted({g["source"] for g in g4["other_source"]})
    info = []
    for l in lists:
        why = list(l["why"]) + ([f"沒有包含其他 exporter（{'、'.join(side)}）"] if side else [])
        info.append({"file": l["file"], "version": l["version"], "names": len(l["names"]), "partial": bool(why), "why": why})
    g6 = [{"what": b.get("what")} for b in card.get("blind_spots") or [] if b.get("category") == "database"]
    tested = [(w, x) for w, x in C.evidence_items(card) if x.get("evidence") == "tested"]
    g7 = [{"item": w.split(": ", 1)[-1] if ": " in w else w} for w, x in tested if not x.get("tested_by")]
    g1, g2 = [], []
    modes = card.get("failure_modes") or []
    has_p1 = bool(modes or card.get("mechanisms"))
    if has_p1:
        pointed = {sid for f in modes for sid in (f.get("detected_by") or {}).get("signals") or []}
        g1 = [{"signal": s.get("id"), "tier": s.get("tier")} for s in signals
              if s.get("section") == "misuse" and s.get("id") not in pointed]
        with_options = {b.get("id") for b in card.get("blind_spots") or [] if b.get("id") and b.get("options")}
        for f in modes:
            d = f.get("detected_by") or {}
            if not d.get("signals") and not with_options & set(d.get("blind_spots") or []):
                g2.append({"failure_mode": f.get("id"), "mechanism": f.get("mechanism"),
                           "by": "accepted" if d.get("accepted") else "blind_spots"})
    return {"card": cid, "path": card.get("_path"), "signals": len(signals), "lists": saved["files"] if saved else [],
            "list_info": info, "lists_partial": bool(info) and all(x["partial"] for x in info),
            "p1": has_p1, "mechanisms": len(card.get("mechanisms") or []), "failure_modes": len(modes),
            "G1": g1, "G2": g2, "G3": g3, "G3.interview": g3i, "G4": g4, "G5": g5, "G6": g6, "G7": g7,
            "tested": len(tested), "p2": len(tested) > len(g7)}


def trace(root: str = C.ROOT) -> list[dict]:
    cards = C.load_components(root)
    lists = saved_lists(root, {c["id"] for c in cards})
    rm = rule_metrics(root)
    return [trace_card(c, lists, rm) for c in cards]


def counts(t: dict) -> dict:
    """Counts derived from the item lists (G4 is the number of names outside confirmed and external)."""
    return {"signals": t["signals"], "G1": len(t["G1"]), "G2": len(t["G2"]), "G3": len(t["G3"]), "G3.interview": len(t["G3.interview"]), "G5": len(t["G5"]),
            "G6": len(t["G6"]), "G7": len(t["G7"]), **{f"G4.{k}": len(t["G4"][k]) for k in G4_ALL}}


def baseline_of(result: list[dict]) -> dict:
    """What catalog/trace-baseline.json holds: counts per card and class."""
    return {"note": f"check_trace.py 的缺口計數基準；tests/test_trace.py 在 {'、'.join(RATCHET)} 任何一項增加時失敗。"
                    f"更新：{UPDATE_BASELINE}",
            "cards": {t["card"]: {k: v for k, v in counts(t).items() if k != "signals"} for t in result}}


def load_baseline(root: str = C.ROOT) -> dict:
    with open(os.path.join(root, BASELINE), encoding="utf-8") as f:
        return json.load(f)


def ratchet(result: list[dict], base: dict) -> list[str]:
    """Gap counts above the baseline, one line each (a card missing from the baseline counts from 0)."""
    out = []
    cards = base.get("cards") or {}
    for t in result:
        now, was = counts(t), cards.get(t["card"]) or {}
        for k in RATCHET:
            if now[k] > int(was.get(k, 0)):
                out.append(f"{t['card']} {k}: {was.get(k, 0)} → {now[k]}")
    return out


def totals(result: list[dict]) -> dict:
    out: dict[str, int] = {}
    for t in result:
        for k, v in counts(t).items():
            out[k] = out.get(k, 0) + v
    return out


COLS = ("signals", "G1", "G2", "G3", "G3.interview", "G5", "G6", "G7", "G4.confirmed", "G4.other_source", "G4.not_in_list", "G4.no_list",
        "G4.external")


def render(result: list[dict], items: bool = True) -> str:
    lines = ["# check_trace（只報告，不判定）", "", P1_NOTE, "",
             "| 卡 | 訊號 | G1 | G2 | G3 | G3 問架構 | G5 | G6 | G7 | G4 已確認 | G4 其他來源 | G4 不在清單 | G4 沒有清單 | G4 外部 | 清單 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for t in result + [{"card": "**合計**", "_totals": totals(result)}]:
        c = t.get("_totals") or counts(t)
        state = "" if "_totals" in t else "沒有" if not t["list_info"] else "部分" if t["lists_partial"] else \
            "完整" if not any(x["partial"] for x in t["list_info"]) else "有部分"
        lines.append(f"| {t['card']} | " + " | ".join(str(c[k]) for k in COLS) + f" | {state} |")
    lines += ["", "G1 " + GAP_LABEL["G1"] + "；G2 " + GAP_LABEL["G2"] + "。",
              "G3 " + GAP_LABEL["G3"] + "；G3 問架構：同樣沒有 rules，但 evaluate_on 是 "
              + "／".join(INTERVIEW_TYPES) + "（用問的或盤點，不算偵測缺口）；G5 " + GAP_LABEL["G5"] + "；G6 " + GAP_LABEL["G6"] + "；G7 " + GAP_LABEL["G7"] + "（清單只列已經開始用 tested_by 的卡）。",
              "G4：" + "；".join(f"{k} {G4_LABEL[k]}" for k in G4_ALL) + "。",
              "清單：部分 = 這張卡的已存清單都不完整（存的時候用前綴過濾，或沒有包含其他 exporter）；不在清單的名稱要人工確認。"]
    if not items:
        return "\n".join(lines) + "\n"
    for t in result:
        lines += ["", f"## {t['card']}（{t['path']}）",
                  "已存 metric 清單：" + ("" if t["list_info"] else "沒有")]
        for x in t["list_info"]:
            lines.append(f"  清單 {x['file']}（{x['version']}，{x['names']} 個名稱"
                         + (f"；部分：{'；'.join(x['why'])}" if x["partial"] else "") + "）")
        if t["p1"]:
            lines.append(f"  機制 {t['mechanisms']} 個、failure mode {t['failure_modes']} 個")
        for g in t["G1"]:
            lines.append(f"  G1 {t['card']}#{g['signal']} tier {g['tier']}")
        for g in t["G2"]:
            lines.append(f"  G2 {t['card']}: {g['failure_mode']}（機制 {g['mechanism']}，{g['by']}）")
        for g in t["G3"]:
            lines.append(f"  G3 {t['card']}#{g['signal']} tier {g['tier']} upstream {g['upstream']}")
        for g in t["G3.interview"]:
            lines.append(f"  G3.interview {t['card']}#{g['signal']} tier {g['tier']} evaluate_on {g['evaluate_on']}")
        for g in t["G5"]:
            lines.append(f"  G5 {t['card']}#{g['signal']} tier {g['tier']} evidence {g['evidence']}，{g['rules']} 條 rule")
        for g in t["G6"]:
            lines.append(f"  G6 {t['card']}: {g['what']}")
        if t["p2"]:
            lines.append(f"  實測項目 {t['tested']} 個，其中 {t['tested'] - len(t['G7'])} 個指到假設")
            for g in t["G7"]:
                lines.append(f"  G7 {t['card']}: {g['item']}")
        for cls in ("not_in_list", "no_list"):
            for g in t["G4"][cls]:
                lines.append(f"  G4.{cls} {t['card']}: {g['metric']} ← {'、'.join(g['used_by'])}")
        for g in t["G4"]["other_source"]:
            lines.append(f"  G4.other_source {t['card']}: {g['metric']}（{g['source']}）")
        for g in t["G4"]["confirmed"]:
            if g.get("not_in_versions"):
                lines.append(f"  G4.confirmed {t['card']}: {g['metric']} 只在 {'、'.join(g['versions'])} 看到"
                             f"（{'、'.join(g['not_in_versions'])} 的清單沒有）")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Report-only gap trace per card (never fails)")
    ap.add_argument("--card", help="只看這張卡（id）")
    ap.add_argument("--json", metavar="PATH", help="把完整結果寫成 JSON")
    ap.add_argument("--summary", action="store_true", help="只印計數表（CI step summary 用）")
    ap.add_argument("--write-baseline", action="store_true",
                    help=f"把目前的計數寫進 {BASELINE}（測試用它擋住缺口增加；只在確認過差異後更新）")
    a = ap.parse_args()
    result = trace()
    if a.write_baseline:
        with open(os.path.join(C.ROOT, BASELINE), "w", encoding="utf-8") as f:
            json.dump(baseline_of(result), f, indent=1, ensure_ascii=False, sort_keys=True)
            f.write("\n")
        print(f"已寫入 {BASELINE}")
        return 0
    if a.card:
        result = [t for t in result if t["card"] == a.card]
        if not result:
            print(f"沒有卡 {a.card!r}")
            return 0
    print(render(result, items=not a.summary), end="")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"note": P1_NOTE, "totals": totals(result), "cards": result}, f, indent=1, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
