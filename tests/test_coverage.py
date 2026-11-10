"""Offline test of tools/check_coverage.py on a snapshot taken from lab/coverage (three planted gaps)."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import check_coverage as CC  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "coverage")


class CoverageTest(unittest.TestCase):
    def setUp(self):
        env = CC.load_env(os.path.join(FIX, "env.yaml"))
        self.items = {(i["component"], i["signal"]): i for i in CC.evaluate(env, os.path.join(FIX, "snapshot"), {1}, live=False)}

    def test_planted_gaps(self):
        self.assertEqual(self.items[("prom-a", "wal-write-failed")]["status"], "missing")
        self.assertEqual(self.items[("prom-a", "rule-failures")]["status"], "self_only")
        # the planted job-name mismatch (job=~".*prometheus.*" vs env job prom-a-self) is now seen offline
        item = self.items[("prom-a", "prometheus-down")]
        self.assertEqual(item["status"], "rule_job_mismatch")
        self.assertEqual(item["mismatched_rules"][0]["placement"], "independent")
        self.assertEqual(item["mismatched_rules"][0]["alert"], "PrometheusDown")

    def test_canaries_belong_on_the_component(self):
        self.assertEqual(self.items[("prom-a", "watchdog")]["status"], "covered")

    def test_observer_questions_from_env(self):
        self.assertEqual(self.items[("observers", "separate-alert-path")]["status"], "covered")
        self.assertEqual(self.items[("observers", "scrapes-all-infra")]["status"], "covered")

    def test_every_card_is_a_valid_type(self):
        import catalog as C
        ids = {c["id"] for c in C.load_components()}
        for cid in ids:
            self.assertEqual(CC.card_for(cid, ids), cid)
        self.assertEqual(CC.card_for("valkey", ids), "redis_exporter")
        self.assertEqual(CC.card_for("thanos-ruler", ids), "thanos")
        self.assertIsNone(CC.card_for("zabbix", ids))
        env = {"components": [{"id": "tr", "type": "thanos"}, {"id": "g", "type": "grafana_alerting"}], "observers": []}
        self.assertEqual(set(CC.rulers(env)), {"tr", "g"})

    def test_job_matcher(self):
        self.assertTrue(CC.job_matches('up{job=~".*prometheus.*"}', "my-prometheus"))
        self.assertFalse(CC.job_matches('up{job=~".*prometheus.*"}', "prom-a-self"))
        self.assertTrue(CC.job_matches("prometheus_tsdb_wal_writes_failed_total", "anything"))


FIX2 = os.path.join(ROOT, "tests", "fixtures", "coverage-db")


class CoverageDbTest(unittest.TestCase):
    """DB-only rule set (like the sim-mysql review): job names, features, unscraped Alertmanager."""

    def setUp(self):
        import json
        import tempfile
        import rules_to_snapshot as R2S
        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, "prom"))
        with open(os.path.join(self.tmp.name, "prom", "rules.json"), "w", encoding="utf-8") as fh:
            json.dump(R2S.convert([os.path.join(FIX2, "rules.yml")]), fh)
        self.env = CC.load_env(os.path.join(FIX2, "env.yaml"))

    def tearDown(self):
        self.tmp.cleanup()

    def items(self, tiers=(1, 2, 3), scrape_jobs=None, env=None):
        got = CC.evaluate(env or self.env, self.tmp.name, set(tiers), live=False, scrape_jobs=scrape_jobs)
        return {(i["component"], i["signal"]): i for i in got}

    def test_name_match_needs_the_job(self):
        it = self.items()
        self.assertEqual(it[("db-wrong", "exporter-up")]["status"], "rule_job_mismatch")
        self.assertEqual(it[("db-wrong", "db-up")]["status"], "rule_job_mismatch")
        self.assertEqual(it[("db-ok", "exporter-up")]["status"], "covered")
        self.assertEqual(it[("db-ok", "db-up")]["status"], "covered")

    def test_exporter_alias_asks_which_database(self):
        # env type mysqld_exporter names the exporter, not the database: mysql card plus a question
        self.assertEqual(CC.card_for("mysqld_exporter", {"mysql", "mariadb"}), "mysql")
        self.assertEqual(CC.card_for("mariadb", {"mysql", "mariadb"}), "mariadb")
        it = self.items()
        q = it[("db-ok", None)]
        self.assertEqual(q["status"], "needs_input")
        self.assertIn("MySQL 還是 MariaDB", q["note"])
        self.assertEqual(it[("db-ok", "exporter-up")]["card"], "mysql")

    def test_features_make_signals_not_applicable(self):
        it = self.items()
        self.assertEqual(it[("db-ok", "custom-queries")]["status"], "not_applicable")       # no query-exporter
        self.assertEqual(it[("db-ok", "probe-blast-radius")]["status"], "not_applicable")   # not multi-target
        self.assertEqual(it[("db-ok", "semi-sync")]["status"], "not_applicable")
        self.assertEqual(it[("db-ok", "replication-running")]["status"], "not_applicable")  # primary side
        self.assertNotEqual(it[("db-ok", "primary-writable")]["status"], "not_applicable")
        self.assertNotEqual(it[("db-replica", "replication-running")]["status"], "not_applicable")
        self.assertEqual(it[("db-replica", "primary-writable")]["status"], "not_applicable")
        self.assertIn("primary 還是 replica", it[("db-generic", "replication-running")]["note"])

    def test_no_features_list_asks(self):
        note = self.items()[("db-wrong", "custom-queries")]["note"]
        self.assertIn("需要回答：有沒有", note)
        self.assertIn("query-exporter", note)

    def test_config_flags_unscraped_component(self):
        it = self.items(scrape_jobs={"mysqld", "prometheus"})
        self.assertIn("找不到像是 alertmanager", it[("am", "alertmanager-down")]["note"])
        self.assertEqual(it[("db-wrong", "exporter-up")]["status"], "rule_job_mismatch")
        env = dict(self.env, targets=[dict(self.env["targets"][0], job="gone")])
        self.assertIn("config 沒有 job `gone`", self.items(scrape_jobs={"mysqld"}, env=env)[("db-ok", "exporter-up")]["note"])

    def test_render_is_advice_first(self):
        md = CC.render_md(self.env, list(self.items().values()), {1, 2, 3})
        self.assertLess(md.index("## 建議先做"), md.index("## 要先處理"))
        self.assertIn("**建議**", md)
        self.assertIn("**代價**", md)

    def test_every_applies_when_is_mapped(self):
        import catalog as C
        for card in C.load_components():
            for s in card["signals"]:
                cond = str(s.get("applies_when") or "").strip()
                if not cond or cond in CC.APPLIES_UNMAPPED or cond in ("cluster", "kubernetes", "prometheus in env") \
                        or cond.startswith("replicas") or CC.version_only_cond(s):
                    continue
                req = CC.feature_requirement(cond, card["id"], s["id"])
                self.assertIsNotNone(req, f"{card['id']}#{s['id']}: applies_when {cond!r} 沒有對應的 feature")
                for g in req:
                    self.assertTrue(g <= set(CC.FEATURES), g)


class RulesToSnapshotTest(unittest.TestCase):
    def test_offline_rule_files_find_gaps(self):
        import tempfile
        import rules_to_snapshot as R2S
        with tempfile.TemporaryDirectory() as d:
            for rid, f in (("prom-a", "rules-prom-a.yml"), ("meta", "rules-meta.yml")):
                os.makedirs(os.path.join(d, rid))
                with open(os.path.join(d, rid, "rules.json"), "w", encoding="utf-8") as fh:
                    import json
                    json.dump(R2S.convert([os.path.join(ROOT, "lab", "coverage", f)]), fh)
            env = CC.load_env(os.path.join(FIX, "env.yaml"))
            items = {(i["component"], i["signal"]): i["status"] for i in CC.evaluate(env, d, {1}, live=False)}
        self.assertEqual(items[("prom-a", "wal-write-failed")], "missing")
        self.assertEqual(items[("prom-a", "rule-failures")], "self_only")


class RulesToSnapshotCliTest(unittest.TestCase):
    def test_help_and_errors(self):
        import subprocess
        tool = os.path.join(ROOT, "tools", "rules_to_snapshot.py")
        p = subprocess.run([sys.executable, tool, "--help"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("--ruler", p.stdout)
        p = subprocess.run([sys.executable, tool, "--ruler", "x", "a.yml"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
