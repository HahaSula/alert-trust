import json
import os
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import check_trace as T  # noqa: E402
import validate as V  # noqa: E402

RUNBOOK = "https://hahasula.github.io/alert-trust/components/etcd/#a"


def _sig(sid, rules=(), status="no", evidence="code", metrics=("m",)):
    return {"id": sid, "tier": 2, "metrics": list(metrics), "evidence": evidence, "rules": list(rules),
            "upstream": {"status": status}}


class TraceFixtureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = self.tmp.name

        def write(rel, data):
            path = os.path.join(r, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(data if isinstance(data, str) else yaml.safe_dump(data, allow_unicode=True))

        card = {"id": "etcd", "family": "database", "kind": "target", "signals": [
            _sig("no-rule-no-up", status="no", metrics=["etcd_server_has_leader"]),
            _sig("no-rule-missing-up", status=None),
            _sig("no-rule-partial", status="partial"),
            _sig("rule-code", rules=["rules/targets/etcd.yml#A"], status="yes",
                 metrics=["etcd_server_proposals_failed_total", "up"]),
            _sig("rule-tested", rules=["rules/targets/etcd.yml#B"], evidence="tested", metrics=["etcd_x"]),
        ], "blind_spots": [{"what": "d1", "category": "database"}, {"what": "e1", "category": "exporter"}]}
        card["signals"][1]["upstream"] = {}
        write("catalog/targets/etcd.yaml", card)
        write("catalog/components/vmagent.yaml", {"id": "vmagent", "family": "victoriametrics", "kind": "infra",
                                                  "signals": [_sig("s", metrics=["vmagent_foo"])]})
        write("rules/targets/etcd.yml", {"groups": [{"name": "g", "rules": [
            {"alert": "A", "expr": "rate(etcd_server_proposals_failed_total[5m]) > 0 and on() job:recorded:sum > 1",
             "labels": {"tier": "2"}, "annotations": {"runbook_url": RUNBOOK}},
            {"alert": "B", "expr": "etcd_event_only_total > 0", "labels": {"tier": "2"},
             "annotations": {"runbook_url": RUNBOOK}}]}]})
        # a ':' name and only-after-event counter; `etcd_server_proposals_failed` is saved without _total
        write("lab/targets/etcd/results/names-etcd-1.txt", "etcd_server_has_leader\netcd_server_proposals_failed\netcd_x\n")
        self.result = {t["card"]: t for t in T.trace(r)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_g3_g5_g6(self):
        t = self.result["etcd"]
        self.assertEqual([g["signal"] for g in t["G3"]], ["no-rule-no-up", "no-rule-missing-up"])
        self.assertEqual(t["G3"][1]["upstream"], "missing")
        self.assertEqual([g["signal"] for g in t["G5"]], ["rule-code"])
        self.assertEqual([g["what"] for g in t["G6"]], ["d1"])

    def test_g4_classes(self):
        t = self.result["etcd"]
        names = {k: [g["metric"] for g in v] for k, v in t["G4"].items()}
        self.assertEqual(names["confirmed"], ["etcd_server_has_leader", "etcd_server_proposals_failed_total", "etcd_x"])
        self.assertEqual(names["not_in_list"], ["etcd_event_only_total", "m"])
        self.assertEqual(names["external"], ["job:recorded:sum", "up"])
        self.assertEqual(t["lists"], ["lab/targets/etcd/results/names-etcd-1.txt"])
        v = self.result["vmagent"]
        self.assertEqual([g["metric"] for g in v["G4"]["no_list"]], ["vmagent_foo"])
        self.assertEqual(v["lists"], [])

    def test_lab_file_mapping(self):
        cards = {"mysql", "mariadb", "redis_exporter", "postgres_exporter", "tidb"}
        f = T._card_of_names_file
        self.assertEqual(f("lab/targets/mysql/ha/results/names-my1.txt", cards), "mysql")
        self.assertEqual(f("lab/targets/mysql/ha/results/names-mdb2.txt", cards), "mariadb")
        self.assertEqual(f("lab/targets/mysql/results/metric-names-normal.txt", cards), "mariadb")
        self.assertEqual(f("lab/targets/valkey/results/names-e-kg.txt", cards), "redis_exporter")
        self.assertEqual(f("lab/targets/postgres/target/results/names-e-p16.txt", cards), "postgres_exporter")
        self.assertEqual(f("lab/targets/tidb/results/names-pd-8.5.8.txt", cards), "tidb")
        self.assertIsNone(f("lab/targets/unknown/results/names-x.txt", cards))


class E1Test(unittest.TestCase):
    REFS = {"c1": {"kind": "code"}, "x1": {"kind": "experiment"}}

    def card(self, **item):
        return {"_path": "t.yaml", "id": "t", "signals": [dict({"id": "s"}, **item)]}

    def test_code_only_fails(self):
        errs = V.check_tested_refs([self.card(evidence="tested", refs=["c1"])], {}, self.REFS)
        self.assertEqual(len(errs), 1)
        self.assertIn("E1", errs[0])
        self.assertIn("t.yaml.signals[s]", errs[0])

    def test_experiment_ok_and_other_items(self):
        self.assertEqual(V.check_tested_refs([self.card(evidence="tested", refs=["c1", "x1"])], {}, self.REFS), [])
        self.assertEqual(V.check_tested_refs([self.card(evidence="code", refs=["c1"])], {}, self.REFS), [])
        card = {"_path": "t.yaml", "id": "t", "signals": [],
                "usage": [{"id": "u", "evidence": "tested", "refs": []}],
                "silent_failures": [{"evidence": "tested"}]}
        self.assertEqual(len(V.check_tested_refs([card], {"k": {"evidence": "tested", "refs": ["c1"]}}, self.REFS)), 3)

    def test_real_catalog_has_no_violations(self):
        import catalog as C
        self.assertEqual(V.check_tested_refs(C.load_components(), C.load_contracts(), C.load_refs()), [])


class RealCatalogTest(unittest.TestCase):
    def test_counts_match_listing(self):
        result = T.trace()
        text = T.render(result)
        tot = T.totals(result)
        for g in ("G3", "G5", "G6"):
            self.assertEqual(tot[g], sum(len(t[g]) for t in result))
            self.assertEqual(tot[g], sum(1 for line in text.splitlines() if line.startswith(f"  {g} ")))
        for cls in ("not_in_list", "no_list"):
            self.assertEqual(tot[f"G4.{cls}"], sum(1 for line in text.splitlines() if line.startswith(f"  G4.{cls} ")))
        self.assertEqual(tot["signals"], sum(t["signals"] for t in result))
        row = next(line for line in text.splitlines() if line.startswith("| **合計**"))
        self.assertEqual(row, "| **合計** | " + " | ".join(str(tot[k]) for k in T.COLS) + " |")
        json.dumps(result, ensure_ascii=False)  # --json must serialise


if __name__ == "__main__":
    unittest.main()
