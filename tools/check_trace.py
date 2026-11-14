#!/usr/bin/env python3
"""Report-only trace of gaps per catalog card (catalog/components, catalog/targets). Never fails.

  python3 tools/check_trace.py                 # count table + every item
  python3 tools/check_trace.py --card mysql    # one card
  python3 tools/check_trace.py --summary       # count table only (CI step summary)
  python3 tools/check_trace.py --json out.json

Gap classes (counts and lists only, no percentages):
  G1/G2  need mechanism data (P1); not estimated here.
  G3     signal with no rules and upstream.status no / missing (「有訊號、沒偵測」).
  G4     metric existence: names a card's signals list (`metrics`, which contains `key_metrics` / `metrics_any`)
         plus the selectors of the PromQL rules those signals reference, against the metric-name lists saved in lab/:
           confirmed    in a saved list of that card
           not_in_list  the card has saved lists but the name is not in them (may be a counter that appears only after
                        an event, or a list filtered by prefix: check by hand)
           no_list      no saved list for this card
         Names produced by the scraper or other exporters (check_metrics.EXTERNAL: up, scrape_*, ALERTS*, node_*...)
         and recording-rule outputs (names with ':') are counted as external and not checked.
  G5     signal with rules whose evidence is not tested.
  G6     blind_spots with category database (元件本身沒提供).
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

GAPS = ("G3", "G4", "G5", "G6")
G4_CLASSES = ("confirmed", "not_in_list", "no_list")
G4_LABEL = {"confirmed": "已確認", "not_in_list": "不在已存清單（可能是事件後才出現的 counter，要人工確認）",
            "no_list": "這張卡沒有已存清單", "external": "外部或 recording rule（不檢查）"}
GAP_LABEL = {"G3": "有訊號、沒偵測（沒有 rules，上游也沒有）", "G4": "metric 存在性",
             "G5": "有 rules，但 evidence 不是 tested", "G6": "blind_spots 是 database（元件本身沒提供）"}
P1_NOTE = "G1/G2：需要 P1 的機制資料"
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


def saved_lists(root: str, cards: set[str]) -> dict[str, dict]:
    """{card: {"names": set, "files": [rel paths]}} from the metric-name lists saved in lab/."""
    out: dict[str, dict] = {}

    def add(card: str, rel: str, names: set[str]) -> None:
        e = out.setdefault(card, {"names": set(), "files": []})
        e["names"] |= names
        e["files"].append(rel)

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
            add(card, rel, names)
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
    g3, g5 = [], []
    used: dict[str, set[str]] = {}  # metric -> where it is used
    for s in signals:
        sid = s.get("id")
        rules = s.get("rules") or []
        status = (s.get("upstream") or {}).get("status")
        if not rules and status in (None, "no", False):
            g3.append({"signal": sid, "tier": s.get("tier"), "upstream": status or "missing"})
        if rules and s.get("evidence") != "tested":
            g5.append({"signal": sid, "tier": s.get("tier"), "evidence": s.get("evidence"), "rules": len(rules)})
        for m in s.get("metrics") or []:
            used.setdefault(m, set()).add(f"{sid}（metrics）")
        for r in rules:
            for m in rmetrics.get(r, ()):
                used.setdefault(m, set()).add(f"{sid}（{r}）")
    saved = lists.get(cid)
    g4: dict[str, list] = {k: [] for k in G4_CLASSES + ("external",)}
    for m in sorted(used):
        if _external(m):
            cls = "external"
        elif saved is None:
            cls = "no_list"
        else:
            cls = "confirmed" if CM.is_exposed(m, saved["names"]) else "not_in_list"
        g4[cls].append({"metric": m, "used_by": sorted(used[m])})
    g6 = [{"what": b.get("what")} for b in card.get("blind_spots") or [] if b.get("category") == "database"]
    return {"card": cid, "path": card.get("_path"), "signals": len(signals), "lists": saved["files"] if saved else [],
            "G3": g3, "G4": g4, "G5": g5, "G6": g6}


def trace(root: str = C.ROOT) -> list[dict]:
    cards = C.load_components(root)
    lists = saved_lists(root, {c["id"] for c in cards})
    rm = rule_metrics(root)
    return [trace_card(c, lists, rm) for c in cards]


def counts(t: dict) -> dict:
    """Counts derived from the item lists (G4 is the number of names outside confirmed and external)."""
    return {"signals": t["signals"], "G3": len(t["G3"]), "G5": len(t["G5"]), "G6": len(t["G6"]),
            **{f"G4.{k}": len(t["G4"][k]) for k in G4_CLASSES + ("external",)}}


def totals(result: list[dict]) -> dict:
    out: dict[str, int] = {}
    for t in result:
        for k, v in counts(t).items():
            out[k] = out.get(k, 0) + v
    return out


COLS = ("signals", "G3", "G5", "G6", "G4.confirmed", "G4.not_in_list", "G4.no_list", "G4.external")


def render(result: list[dict], items: bool = True) -> str:
    lines = ["# check_trace（只報告，不判定）", "", P1_NOTE, "",
             "| 卡 | 訊號 | G3 | G5 | G6 | G4 已確認 | G4 不在清單 | G4 沒有清單 | G4 外部 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for t in result + [{"card": "**合計**", "_totals": totals(result)}]:
        c = t.get("_totals") or counts(t)
        lines.append(f"| {t['card']} | " + " | ".join(str(c[k]) for k in COLS) + " |")
    lines += ["", "G3 " + GAP_LABEL["G3"] + "；G5 " + GAP_LABEL["G5"] + "；G6 " + GAP_LABEL["G6"] + "。",
              "G4：" + "；".join(f"{k} {G4_LABEL[k]}" for k in G4_CLASSES + ("external",)) + "。"]
    if not items:
        return "\n".join(lines) + "\n"
    for t in result:
        lines += ["", f"## {t['card']}（{t['path']}）",
                  "已存 metric 清單：" + ("、".join(t["lists"]) if t["lists"] else "沒有")]
        for g in t["G3"]:
            lines.append(f"  G3 {t['card']}#{g['signal']} tier {g['tier']} upstream {g['upstream']}")
        for g in t["G5"]:
            lines.append(f"  G5 {t['card']}#{g['signal']} tier {g['tier']} evidence {g['evidence']}，{g['rules']} 條 rule")
        for g in t["G6"]:
            lines.append(f"  G6 {t['card']}: {g['what']}")
        for cls in ("not_in_list", "no_list"):
            for g in t["G4"][cls]:
                lines.append(f"  G4.{cls} {t['card']}: {g['metric']} ← {'、'.join(g['used_by'])}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Report-only gap trace per card (never fails)")
    ap.add_argument("--card", help="只看這張卡（id）")
    ap.add_argument("--json", metavar="PATH", help="把完整結果寫成 JSON")
    ap.add_argument("--summary", action="store_true", help="只印計數表（CI step summary 用）")
    a = ap.parse_args()
    result = trace()
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
