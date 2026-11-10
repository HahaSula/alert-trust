import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import fit_contracts as F  # noqa: E402

RULES = os.path.join(ROOT, "tests", "fixtures", "fit", "rules.yml")


class FitContractsTest(unittest.TestCase):
    def _args(self, **kw):
        a = type("A", (), {})()
        a.stack, a.S, a.d, a.E, a.O, a.k = "prometheus", 30.0, 10.0, 30.0, 0.0, 1
        a.T, a.lookback, a.latency, a.eval_delay = 5.0, 300.0, 0.0, 30.0
        a.job_interval, a.job_duration = {"mysql": 60.0}, {"mysql": 75.0}
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    def test_durations(self):
        self.assertEqual(F.dur("1m30s"), 90)
        self.assertEqual(F.fmt(450), "7m30s")
        self.assertEqual(F.fmt_max(16.7), "16s")

    def test_prometheus_window_and_options(self):
        rows, fitted = F.analyze(self._args(), F.load([RULES]))
        by = {r["rule"]: r for r in rows}
        self.assertTrue(by["job:http_requests:rate5m"]["ok"])           # 5m >= 3*30+10
        self.assertFalse(by["ErrorsHigh"]["ok"])                        # 1m < 100s
        self.assertEqual(by["ErrorsHigh"]["fit_R"], 105)
        self.assertAlmostEqual(by["ErrorsHigh"]["opt_b"]["scrape_interval"], 50 / 3)
        self.assertEqual(by["MySQLSlow"]["I"], 120)                     # d 75s > S 60s -> I = 2S
        self.assertEqual(by["RecordedWindow"]["S"], 60)                 # recording rule: group interval
        exprs = [r["expr"] for _, d in fitted for g in d["groups"] for r in g["rules"]]
        self.assertIn('rate(http_errors_total{job="api"}[1m45s]) > 1', exprs)

    def test_offset_removes_d(self):
        rows, _ = F.analyze(self._args(O=10.0), F.load([RULES]))
        self.assertEqual({r["rule"]: r for r in rows}["ErrorsHigh"]["need"], 90)

    def test_env_check_flags_slow_scrape(self):
        checks = F.env_checks(self._args())
        self.assertTrue(any("job mysql" in t and not ok for t, ok, _ in checks))

    def test_vmalert(self):
        rows, _ = F.analyze(self._args(stack="vmalert", latency=40.0), F.load([RULES]))
        self.assertTrue({r["rule"]: r for r in rows}["ErrorsHigh"]["ok"])   # 1m >= 30+40-30

    def test_cli_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "fit_contracts.py"), "--rules", RULES,
                                "--stack", "prometheus", "--scrape-interval", "30s", "--scrape-duration", "10s",
                                "--eval-interval", "30s", "--out", d], capture_output=True, text=True)
            self.assertEqual(p.returncode, 1, p.stderr)
            self.assertIn("方案 A", open(os.path.join(d, "contracts-fit.md"), encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()
