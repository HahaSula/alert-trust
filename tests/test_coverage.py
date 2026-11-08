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
        # without --live the job-name mismatch cannot be seen; the rule is found on the observer
        self.assertEqual(self.items[("prom-a", "prometheus-down")]["status"], "covered")
        self.assertEqual(self.items[("prom-a", "prometheus-down")]["rules"][0]["placement"], "independent")

    def test_canaries_belong_on_the_component(self):
        self.assertEqual(self.items[("prom-a", "watchdog")]["status"], "covered")

    def test_observer_questions_from_env(self):
        self.assertEqual(self.items[("observers", "separate-alert-path")]["status"], "covered")
        self.assertEqual(self.items[("observers", "scrapes-all-infra")]["status"], "covered")

    def test_job_matcher(self):
        self.assertTrue(CC.job_matches('up{job=~".*prometheus.*"}', "my-prometheus"))
        self.assertFalse(CC.job_matches('up{job=~".*prometheus.*"}', "prom-a-self"))
        self.assertTrue(CC.job_matches("prometheus_tsdb_wal_writes_failed_total", "anything"))


if __name__ == "__main__":
    unittest.main()
