"""check_coverage.py fixes from the simulated reviews (fixtures: tests/fixtures/coverage-review)."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import check_coverage as CC  # noqa: E402
import check_metric_sources as MS  # noqa: E402
import rules_to_snapshot as R2S  # noqa: E402

FX = os.path.join(ROOT, "tests", "fixtures", "coverage-review")


class CoverageReviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        for rid, f in (("prom", "prom-rules.yml"), ("vmalert", "vmalert-rules.yml"), ("ext", "ext-rules.yml")):
            os.makedirs(os.path.join(cls.tmp.name, rid))
            with open(os.path.join(cls.tmp.name, rid, "rules.json"), "w", encoding="utf-8") as fh:
                json.dump(R2S.convert([os.path.join(FX, f)]), fh)
        cls.env = CC.load_env(os.path.join(FX, "env.yaml"))
        cls.jobs = {j["job"] for j in MS.load_config(os.path.join(FX, "prometheus.yml"))}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def items(self, env=None, tiers=(1, 2)):
        got = CC.evaluate(env or self.env, self.tmp.name, set(tiers), live=False, scrape_jobs=self.jobs)
        return {(i["component"], i["signal"]): i for i in got}, got

    def test_observer_watchdog_is_not_the_rulers(self):
        it, _ = self.items()
        wd = it[("prom", "watchdog")]
        self.assertEqual(wd["status"], "missing")
        self.assertIn("ext 自己的 Watchdog", wd["note"])

    def test_watchdog_needs_external_dms(self):
        env = dict(self.env, observers=[])
        env["components"] = [dict(c) for c in env["components"]]
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "prom"))
            with open(os.path.join(d, "prom", "rules.json"), "w", encoding="utf-8") as fh:
                json.dump(R2S.convert([os.path.join(FX, "ext-rules.yml")]), fh)   # Watchdog on prom itself
            got = {(i["component"], i["signal"]): i for i in CC.evaluate(env, d, {1}, False, self.jobs)}
            self.assertEqual(got[("prom", "watchdog")]["status"], "needs_input")
            self.assertIn("dead man's switch", got[("prom", "watchdog")]["note"])
            env["deadman"] = {"receiver": "external-webhook"}
            got = {(i["component"], i["signal"]): i for i in CC.evaluate(env, d, {1}, False, self.jobs)}
            self.assertEqual(got[("prom", "watchdog")]["status"], "covered")

    def test_unknown_is_not_an_answer(self):
        it, _ = self.items()
        self.assertEqual(it[("observers", "separate-alert-path")]["status"], "needs_input")
        self.assertEqual(it[("observers", "own-deadman")]["status"], "needs_input")
        self.assertFalse(CC.answered("不知道"))
        self.assertFalse(CC.answered(" unknown "))
        self.assertTrue(CC.answered("am-meta"))
        ok, note = CC.applies({"applies_when": "cluster"}, self.env["components"][1], self.env)
        self.assertTrue(ok)
        self.assertIn("需要回答", note)

    def test_unscraped_ruler_has_no_data(self):
        it, _ = self.items()
        self.assertEqual(it[("vmalert", "rule-errors")]["status"], "rule_no_data")   # not self_only
        self.assertIn("沒有被 scrape", it[("vmalert", "rule-errors")]["note"])

    def test_partial_when_key_metric_unused(self):
        it, _ = self.items()
        h = it[("etcd-1", "health-second-path")]
        self.assertEqual(h["status"], "partial")
        self.assertIn("probe_success", h["note"])
        q = it[("etcd-1", "quorum")]
        self.assertEqual(q["status"], "partial")
        self.assertIn("第一個 metric", q["note"])
        self.assertEqual(it[("etcd-1", "has-leader")]["status"], "covered")

    def test_slave_replica_spelling_is_one_metric(self):
        sig = {"metrics": ["mysql_slave_status_slave_sql_running", "mysql_slave_status_replica_sql_running"]}
        self.assertEqual(CC.missing_metrics(sig, [{"query": 'mysql_slave_status_replica_sql_running{job="m"} == 0'}]), [])
        self.assertEqual(CC.missing_metrics({"metrics": ["a_total", "b_total"]}, [{"query": '{__name__=~"a_.*"} > 0'}]),
                         ["b_total"])

    def test_todo_sorted_by_impact(self):
        _, got = self.items()
        md = CC.render_md(self.env, got, {1, 2})
        todo = md.split("## 要先處理")[1].split("\n## ")[0]
        rows = [l for l in todo.splitlines() if l.startswith("| 1 ") or l.startswith("| 2 ")]
        self.assertTrue(rows[0].startswith("| 1 | prom |") and "#watchdog" in rows[0], rows[0])
        self.assertIn("#watchdog", rows[1])                                    # vmalert's Watchdog next, before other tier-1 rows
        tiers = [int(l.split("|")[1]) for l in rows]
        self.assertEqual(tiers, sorted(tiers))
        cards = {c["id"]: c for c in __import__("catalog").load_components()}
        keys = [CC.impact_key(i, cards) for i in sorted(got, key=lambda x: CC.impact_key(x, cards)) if i["status"] in CC.ACTION]
        self.assertEqual(keys, sorted(keys))


if __name__ == "__main__":
    unittest.main()
