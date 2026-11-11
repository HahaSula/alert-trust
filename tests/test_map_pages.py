"""catalog/map.yaml generation and the five-section layout of the generated component pages."""
import os
import re
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_map as M  # noqa: E402
import build_pages as B  # noqa: E402
import catalog as C  # noqa: E402

ORDER = ["architecture", "usage", "observe", "misuse", "blind"]
DBS = {"mysql", "mariadb", "postgres_exporter", "redis_exporter", "mongodb_exporter", "etcd", "tidb", "yugabytedb", "clickhouse"}


def fake_card(**kw):
    c = {"_path": "catalog/targets/t.yaml", "id": "t", "name": "T", "kind": "target", "family": "database",
         "status": "draft", "role": "測試用", "architecture": "architecture/db-kv.md",
         "usage": [{"id": "acct", "text": "帳號", "evidence": "doc", "refs": ["r1"]}],
         "signals": [
             {"id": "up-ok", "section": "observe", "tier": 1, "question": "活著嗎", "signal": "up", "metrics": ["up"],
              "evaluate_on": "observer", "evidence": "code", "source": "s", "upstream": {"status": "no"}, "rules": []},
             {"id": "too-slow", "section": "misuse", "tier": 2, "question": "太慢嗎", "signal": "x", "metrics": [],
              "evaluate_on": "self", "evidence": "tested", "source": "s", "upstream": {"status": "yes"}, "rules": []}],
         "bad_queries": [{"case": "全表掃描", "signals": ["too-slow"], "visible": "看得到", "not_visible": "看不到", "refs": ["r1"]}],
         "blind_spots": [{"what": "鎖的持有者", "category": "exporter", "why": "沒查", "workaround": "w", "options": "建議：自訂查詢"}]}
    c.update(kw)
    return c


class MapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cards = C.load_components()
        cls.map = M.build_map(cls.cards)

    def test_sections_and_plugins(self):
        self.assertEqual([s["id"] for s in self.map["sections"]], ORDER)
        plugins = self.map["plugins"]
        self.assertNotIn("depends", plugins[M.CORE])
        for name, p in plugins.items():
            if name != M.CORE:
                self.assertEqual(p["depends"], [M.CORE], name)
        self.assertEqual(plugins["alert-trust-host"]["cards"], ["host"])
        self.assertEqual(plugins["alert-trust-postgresql"]["cards"], ["postgres_exporter"])
        self.assertEqual({c for p, i in plugins.items() if p not in (M.CORE, "alert-trust-host") for c in i["cards"]}, DBS)

    def test_every_card_once_and_sections_partition_signals(self):
        owners = [c for p in self.map["plugins"].values() for c in p["cards"]]
        self.assertEqual(sorted(owners), sorted(c["id"] for c in self.cards))
        for card in self.cards:
            e = self.map["cards"][card["id"]]
            self.assertTrue(e["plugin"])
            self.assertEqual(e["usage"], [u["id"] for u in card.get("usage") or []])
            self.assertEqual(sorted(e["observe"] + e["misuse"] + e.get("unsectioned", [])),
                             sorted(s["id"] for s in card["signals"]))
            self.assertEqual(len(e["blind"]), len(card.get("blind_spots") or []))
            self.assertEqual([q["case"] for q in e["bad_queries"]], [q["case"] for q in card.get("bad_queries") or []])
            if card["id"] in DBS:
                self.assertTrue(os.path.exists(os.path.join(ROOT, "docs", e["guide"])), e["guide"])

    def test_render_is_marked_and_round_trips(self):
        text = M.render(self.map)
        self.assertIn("產生的檔案，不要手改", text.splitlines()[0])
        self.assertEqual(yaml.safe_load(text), self.map)
        self.assertEqual(text, M.render(M.build_map(self.cards)))

    def test_check_map_stale_missing_and_plugin(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "map.yaml")
            self.assertTrue(any("不存在" in e for e in M.check_map(self.cards, path)))
            M.write_map(self.cards, path)
            self.assertEqual(M.check_map(self.cards, path), [])
            with open(path, "a", encoding="utf-8") as f:
                f.write("# edited\n")
            self.assertTrue(any("過期" in e for e in M.check_map(self.cards, path)))
            errs = M.check_map(self.cards + [fake_card()], path)
            self.assertTrue(any("剛好屬於一個 plugin" in e for e in errs))
            bad = fake_card(id="t2", signals=[dict(fake_card()["signals"][0], id="observe-exporter")])
            self.assertTrue(any("段落錨點" in e for e in M.check_map([bad], path)))

    def test_committed_map_is_current(self):
        self.assertEqual(M.check_map(self.cards), [])


def headings(page: str) -> list[tuple[int, str]]:
    return [(len(m.group(1)), m.group(2)) for m in re.finditer(r"^(#{2,4}) .*\{#([a-z0-9-]+)\}\s*$", page, re.M)]


class ComponentPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources, cls.contracts = C.load_sources(), C.load_contracts()

    def page(self, card):
        return B.component_page(card, self.sources, self.contracts)

    def test_fake_card_layout(self):
        sources = {"s": {"title": "S", "visibility": "public", "url": "https://example.invalid"}}
        old = B._RULES
        B._RULES = {}
        try:
            page = B.component_page(fake_card(), sources, {})
        finally:
            B._RULES = old
        top = [a for lvl, a in headings(page) if lvl == 2]
        self.assertEqual(top, ORDER)
        sec = {a: page.index("{#" + a + "}") for a in ORDER + ["bad-query-table", "up-ok", "too-slow"]}
        self.assertTrue(sec["observe"] < sec["up-ok"] < sec["misuse"] < sec["too-slow"] < sec["bad-query-table"] < sec["blind"])
        self.assertIn('<span id="usage-acct"></span>', page)
        self.assertIn("at-ev-doc", page)
        self.assertIn("[`too-slow`](#too-slow)", page)
        self.assertIn("建議：自訂查詢", page)

    def test_real_cards_keep_every_anchor(self):
        rules = C.load_rules()
        wanted: dict[str, set[str]] = {}
        for rule in rules.values():
            m = re.search(r"/components/([a-z0-9_]+)/#([a-z0-9-]+)$", (rule.get("annotations") or {}).get("runbook_url", ""))
            if m:
                wanted.setdefault(m.group(1), set()).add(m.group(2))
        for card in C.load_components():
            page = self.page(card)
            hs = headings(page)
            ids = [a for _, a in hs]
            self.assertEqual([a for lvl, a in hs if lvl == 2], ORDER, card["id"])
            self.assertEqual(len(ids), len(set(ids)), f"{card['id']}: duplicate anchors")
            for s in card["signals"]:
                self.assertEqual(ids.count(s["id"]), 1, f"{card['id']}#{s['id']}")
                where = "observe" if s.get("section") == "observe" else "misuse"
                pos = page.index("{#" + s["id"] + "}")
                self.assertLess(page.index("{#" + where + "}"), pos)
                self.assertLess(pos, page.index("{#" + ORDER[ORDER.index(where) + 1] + "}"))
            self.assertLessEqual(wanted.get(card["id"], set()), set(ids), card["id"])
            for b in card.get("blind_spots") or []:
                if b.get("section"):
                    self.assertIn(f"](../{b['section']})", page)

    def test_targets_overview_has_section_counts(self):
        cards = C.load_components()
        page = B.targets_overview(cards)
        self.assertIn("{#sections}", page)
        pg = next(c for c in cards if c["id"] == "postgres_exporter")
        n = B.section_counts(pg)
        self.assertIn(f"[{n['usage']}](../components/postgres_exporter.md#usage)", page)
        self.assertIn(f"[{n['blind']}](../components/postgres_exporter.md#blind)", page)


class TargetGuideTest(unittest.TestCase):
    def test_guides_link_to_five_sections(self):
        for cid, guide in M.GUIDES.items():
            with open(os.path.join(ROOT, "docs", guide), encoding="utf-8") as f:
                text = f.read()
            self.assertIn("{#five-sections}", text, guide)
            for a in ORDER:
                self.assertIn(f"../components/{cid}.md#{a}", text, guide)


if __name__ == "__main__":
    unittest.main()
