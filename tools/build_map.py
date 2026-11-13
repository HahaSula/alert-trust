#!/usr/bin/env python3
"""Generate catalog/map.yaml: which plugin owns each card and what each card says in the five sections.

  python3 tools/build_map.py          # writes catalog/map.yaml (tools/build_pages.py also calls this)

The map is committed (skills read it); tools/validate.py fails when it is stale.
Plugin membership is decided here (PLUGINS); every card must belong to exactly one plugin.
"""
from __future__ import annotations

import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog as C  # noqa: E402

MAP_PATH = os.path.join(C.ROOT, "catalog", "map.yaml")

# The five sections in page order. `anchor` is the heading id on docs/components/<id>.md.
SECTIONS = [
    {"id": "architecture", "title": "架構", "field": "architecture", "anchor": "architecture"},
    {"id": "usage", "title": "正確使用", "field": "usage", "anchor": "usage"},
    {"id": "observe", "title": "正確觀測", "field": "signals[section=observe]", "anchor": "observe"},
    {"id": "misuse", "title": "不正確使用時的觀測", "field": "signals[section=misuse], bad_queries", "anchor": "misuse"},
    {"id": "blind", "title": "目前無法觀測的痕跡", "field": "blind_spots", "anchor": "blind"},
]
# Extra heading ids the generator uses inside sections; signal ids must not take them.
EXTRA_ANCHORS = {"bad-query-table", "silent-failures", "contracts", "not-applicable", "unsectioned"}
GROUP_ANCHORS = {f"{sec}-{g}" for sec in ("observe", "misuse")
                 for g in [*C.ASPECTS, "other", "tier-1", "tier-2", "tier-3"]}
RESERVED_ANCHORS = {s["anchor"] for s in SECTIONS} | EXTRA_ANCHORS | GROUP_ANCHORS

CORE = "alert-trust-core"
# `dir` is plugins/<dir>/ in the repo; `skills` are invoked as /<plugin>:<skill>.
PLUGINS = {
    CORE: {"title": "監控鏈", "dir": "core",
           "cards": ["prometheus", "vmagent", "victoriametrics", "victorialogs", "vmalert", "alertmanager", "thanos",
                     "grafana_alerting", "observer"],
           "skills": ["selfcheck", "env-profile", "rule-review", "prometheus", "victoriametrics", "alertmanager",
                      "policy-lint"]},
    "alert-trust-host": {"title": "主機與容器", "dir": "host", "cards": ["host"], "skills": ["host"]},
    "alert-trust-mysql": {"title": "MySQL", "dir": "mysql", "cards": ["mysql"], "skills": ["mysql"]},
    "alert-trust-mariadb": {"title": "MariaDB", "dir": "mariadb", "cards": ["mariadb"], "skills": ["mariadb"]},
    "alert-trust-postgresql": {"title": "PostgreSQL", "dir": "postgresql", "cards": ["postgres_exporter"],
                               "skills": ["postgresql"]},
    "alert-trust-valkey": {"title": "Valkey／Redis", "dir": "valkey", "cards": ["redis_exporter"], "skills": ["valkey"]},
    "alert-trust-mongodb": {"title": "MongoDB", "dir": "mongodb", "cards": ["mongodb_exporter"], "skills": ["mongodb"]},
    "alert-trust-etcd": {"title": "etcd", "dir": "etcd", "cards": ["etcd"], "skills": ["etcd"]},
    "alert-trust-tidb": {"title": "TiDB", "dir": "tidb", "cards": ["tidb"], "skills": ["tidb"]},
    "alert-trust-yugabytedb": {"title": "YugabyteDB", "dir": "yugabytedb", "cards": ["yugabytedb"],
                               "skills": ["yugabytedb"]},
    "alert-trust-clickhouse": {"title": "ClickHouse", "dir": "clickhouse", "cards": ["clickhouse"],
                               "skills": ["clickhouse"]},
}
# Hand-written "怎麼監控" pages for target cards (docs/targets/...).
GUIDES = {"mysql": "targets/mysql.md", "mariadb": "targets/mariadb.md", "postgres_exporter": "targets/postgres.md",
          "redis_exporter": "targets/valkey.md", "mongodb_exporter": "targets/mongodb.md", "etcd": "targets/etcd.md",
          "tidb": "targets/tidb.md", "yugabytedb": "targets/yugabytedb.md", "clickhouse": "targets/clickhouse.md"}

HEADER = ("# 產生的檔案，不要手改：由 tools/build_map.py（tools/build_pages.py 會呼叫）從 catalog/components、catalog/targets 產生。\n"
          "# Skill 讀這份決定元件屬於哪個 plugin、每段有哪些項目；tools/validate.py 會檢查它是否過期。\n"
          "# 卡片 id → 頁面 docs/components/<id>.md；每段的錨點見 sections[].anchor，訊號的錨點就是訊號 id。\n")


def plugin_of(card_id: str) -> str | None:
    return next((p for p, info in PLUGINS.items() if card_id in info["cards"]), None)


def split_signals(card: dict) -> dict[str, list[dict]]:
    out = {"observe": [], "misuse": [], None: []}
    for s in card.get("signals", []):
        out.get(s.get("section"), out[None]).append(s)
    return out


def card_entry(card: dict) -> dict:
    sig = split_signals(card)
    e = {
        "name": card["name"],
        "kind": card["kind"],
        "plugin": plugin_of(card["id"]),
        "page": f"components/{card['id']}.md",
    }
    if card["id"] in GUIDES:
        e["guide"] = GUIDES[card["id"]]
    e["architecture"] = card.get("architecture")
    e["usage"] = [u["id"] for u in card.get("usage") or []]
    e["observe"] = [s["id"] for s in sig["observe"]]
    e["misuse"] = [s["id"] for s in sig["misuse"]]
    e["bad_queries"] = [{"case": q.get("case"), "signals": list(q.get("signals") or [])} for q in card.get("bad_queries") or []]
    e["blind"] = [b.get("what") for b in card.get("blind_spots") or []]
    if sig[None]:
        e["unsectioned"] = [s["id"] for s in sig[None]]
    if card.get("must_ask"):
        # questions that change conclusions for this component: tools/inventory.py asks them, ranked high
        e["must_ask"] = [{k: q[k] for k in ("id", "question", "why", "refs", "applies_when", "answer_key") if k in q}
                         for q in card["must_ask"]]
    return e


def build_map(cards: list[dict]) -> dict:
    plugins = {}
    for p, info in PLUGINS.items():
        d = {"title": info["title"], "dir": f"plugins/{info['dir']}", "cards": list(info["cards"]),
             "skills": [f"{p}:{sk}" for sk in info["skills"]]}
        if p != CORE:
            d["depends"] = [CORE]
        plugins[p] = d
    return {
        "sections": [dict(s) for s in SECTIONS],
        "plugins": plugins,
        "cards": {c["id"]: card_entry(c) for c in cards},
    }


def render(m: dict) -> str:
    body = yaml.safe_dump(m, allow_unicode=True, sort_keys=False, width=10_000, default_flow_style=False)
    return HEADER + body


def check_map(cards: list[dict], path: str = MAP_PATH) -> list[str]:
    """Errors: card without a plugin, plugin listing an unknown card, reserved anchors, stale or missing map."""
    errors = []
    ids = {c["id"] for c in cards}
    for c in cards:
        owners = [p for p, info in PLUGINS.items() if c["id"] in info["cards"]]
        if len(owners) != 1:
            errors.append(f"{c['_path']}: 卡片要剛好屬於一個 plugin（tools/build_map.py PLUGINS），目前 {owners or '沒有'}")
        for s in c.get("signals", []):
            if s.get("id") in RESERVED_ANCHORS:
                errors.append(f"{c['_path']}#{s['id']}: 訊號 id 和元件頁的段落錨點相同，換一個 id")
        for u in c.get("usage") or []:
            if u.get("id") in RESERVED_ANCHORS or f"usage-{u.get('id')}" in {s.get("id") for s in c.get("signals", [])}:
                errors.append(f"{c['_path']}: usage {u.get('id')!r} 的錨點會和其他錨點衝突")
    for p, info in PLUGINS.items():
        for cid in info["cards"]:
            if cid not in ids:
                errors.append(f"tools/build_map.py: plugin {p} 列了不存在的卡片 {cid}")
    rel = os.path.relpath(path, C.ROOT)
    if not os.path.exists(path):
        errors.append(f"{rel} 不存在：執行 python3 tools/build_pages.py")
    elif open(path, encoding="utf-8").read() != render(build_map(cards)):
        errors.append(f"{rel} 過期（卡片改過）：執行 python3 tools/build_pages.py 再 commit")
    return errors


def write_map(cards: list[dict], path: str = MAP_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(build_map(cards)))


def main() -> int:
    cards = C.load_components()
    write_map(cards)
    print(f"wrote {os.path.relpath(MAP_PATH, C.ROOT)} ({len(cards)} cards)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
