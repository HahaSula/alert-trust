import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import fit_contracts as F  # noqa: E402

RULES = os.path.join(ROOT, "tests", "fixtures", "fit", "rules.yml")
FOR_RULES = os.path.join(ROOT, "tests", "fixtures", "fit", "for-rules.yml")


class FitContractsTest(unittest.TestCase):
    def _args(self, **kw):
        a = type("A", (), {})()
        a.stack, a.S, a.d, a.E, a.O, a.k = "prometheus", 30.0, 10.0, 30.0, 0.0, 1
        a.T, a.lookback, a.latency, a.eval_delay = 5.0, 300.0, 0.0, 30.0
        a.job_interval, a.job_duration = {"mysql": 60.0}, {"mysql": 75.0}
        a.dedup, a.query_step, a.keep_range = None, 300.0, []
        a.timeout, a.p99, a.job_timeout, a.job_p99, a.exporter_deadline, a.job_failrun = None, None, {}, {}, {}, {}
        a.config_kind, a.tau_clamped, a.job_exporter, a.job_exporter_flag = "prometheus", {}, {}, set()
        a.prom_version, a.prom_version_text, a.sources_empty, a.cfg_jobs, a.assumptions = None, "", {}, None, []
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    def test_durations(self):
        self.assertEqual(F.dur("1m30s"), 90)
        self.assertEqual(F.fmt(450), "7m30s")
        self.assertEqual(F.fmt_max(16.75), "16.7s")      # upper bounds round down, one decimal
        self.assertEqual(F.fmt(9.75), "9.8s")
        self.assertEqual(F.fmt(9.8), "9.8s")
        self.assertEqual(F.fmt(10), "10s")
        self.assertEqual(F.fmt(160.5), "2m40.5s")
        self.assertEqual(F.fmt(0.02), "0.02s")
        self.assertEqual(F.fmt(0.3), "0.3s")
        self.assertEqual(F.fmt(0), "0s")

    def test_prometheus_window_and_options(self):
        rows, fitted = F.analyze(self._args(), F.load([RULES]))
        by = {r["rule"]: r for r in rows}
        self.assertTrue(by["job:http_requests:rate5m"]["ok"])           # 5m >= 3*30+10
        self.assertFalse(by["ErrorsHigh"]["ok"])                        # 1m < 100s
        self.assertEqual(by["ErrorsHigh"]["fit_R"], 105)
        self.assertAlmostEqual(by["ErrorsHigh"]["opt_b"]["scrape_interval"], 50 / 3)
        self.assertEqual(by["MySQLSlow"]["I"], 120)                     # d 75s > S 60s -> I = 2S
        self.assertEqual(by["RecordedWindow"]["S"], 60)                 # recording rule: group interval
        self.assertEqual(by["RecordedWindow"]["n"], 1)                  # max_over_time needs 1 sample
        self.assertTrue(by["RecordedWindow"]["ok"])                     # (1+1)*60 + 0 = 2m
        self.assertEqual(by["ErrorsHigh"]["func"], "rate")
        exprs = [r["expr"] for _, d in fitted for g in d["groups"] for r in g["rules"]]
        # nobody said whether [1m] is intentional: not lengthened, marked keep_range: unknown
        self.assertIn('rate(http_errors_total{job="api"}[1m]) > 1', exprs)
        eh = next(r for _, d in fitted for g in d["groups"] for r in g["rules"] if r.get("alert") == "ErrorsHigh")
        self.assertEqual(eh["annotations"]["keep_range"], "unknown")
        self.assertIsNone(by["ErrorsHigh"]["keep"])
        self.assertTrue(by["ErrorsHigh"]["short"])
        # the user said it is not intentional: option A is written, with the original window recorded
        _, fitted = F.analyze(self._args(change_range=["ErrorsHigh"]), F.load([RULES]))
        eh = next(r for _, d in fitted for g in d["groups"] for r in g["rules"] if r.get("alert") == "ErrorsHigh")
        self.assertEqual(eh["expr"], 'rate(http_errors_total{job="api"}[1m45s]) > 1')
        self.assertEqual(eh["annotations"][F.ORIGINAL_RANGE_ANN], "[1m]")

    def test_offset_removes_d(self):
        rows, _ = F.analyze(self._args(O=10.0), F.load([RULES]))
        self.assertEqual({r["rule"]: r for r in rows}["ErrorsHigh"]["need"], 90)
        rows, _ = F.analyze(self._args(O=5.0), F.load([RULES]))           # partial offset: max(0, d - O)
        self.assertEqual({r["rule"]: r for r in rows}["ErrorsHigh"]["need"], 95)

    def test_enclosing_func(self):
        e = 'histogram_quantile(0.9, sum by (le) (rate(x_bucket[5m]))) and max_over_time(up[2m])'
        self.assertEqual(F.enclosing_func(e, e.index("x_bucket")), "rate")
        self.assertEqual(F.enclosing_func(e, e.index("up[")), "max_over_time")

    def test_env_check_flags_slow_scrape(self):
        checks = F.env_checks(self._args())
        self.assertTrue(any("job mysql" in t and not ok for t, ok, *_ in checks))

    def test_timeout_checks(self):
        checks = F.env_checks(self._args(timeout=10.0, d=10.0, E=15.0, job_timeout={"mysql": 60.0},
                                         exporter_deadline={"mysql": 70.0}))
        bad = [t for t, ok, *_ in checks if not ok]
        self.assertTrue(any("截斷" in t and "預設" in t for t in bad))      # d_max == τ
        self.assertTrue(any("τ 1m = S 1m" in t for t in bad))              # τ must be < S; text states the real relation
        self.assertTrue(any("X 1m10s ≥ τ 1m" in t for t in bad))            # exporter deadline >= τ, stated as such
        self.assertTrue(any("evaluation_interval" in t for t in bad))      # E < S

    def test_failrun_raises_k(self):
        rows, _ = F.analyze(self._args(job_failrun={"api": 3}), F.load([RULES]))
        self.assertEqual({r["rule"]: r for r in rows}["ErrorsHigh"]["need"], 5 * 30 + 10)

    def test_baseline(self):
        out = F.baseline(self._args(d=12.0, p99=8.0, job_interval={}, job_duration={"redis": 4.0},
                                    job_p99={"redis": 3.0}))
        row = [l for l in out.splitlines() if l.startswith("| redis")][0]
        self.assertIn("| 30s |", row)            # S kept
        self.assertIn("| 5s |", row)             # tau = max(4/0.9, 3/0.8) -> 5s

    def test_vmalert(self):
        rows, _ = F.analyze(self._args(stack="vmalert", latency=40.0), F.load([RULES]))
        r = {x["rule"]: x for x in rows}["ErrorsHigh"]
        self.assertEqual(r["n"], 1)                 # MetricsQL uses the sample before the window
        self.assertEqual(r["need"], 70)             # (1+1)*30 + (40-30)
        rows, _ = F.analyze(self._args(stack="vmalert", latency=40.0, query_step=60.0), F.load([RULES]))
        self.assertEqual({x["rule"]: x for x in rows}["ErrorsHigh"]["n"], 2)   # (1+k)*I >= step

    def test_vm_dedup(self):
        rows, _ = F.analyze(self._args(stack="vmalert", latency=40.0, dedup=60.0), F.load([RULES]))
        r = {x["rule"]: x for x in rows}["ErrorsHigh"]
        self.assertEqual(r["I"], 60)            # dedup 60s > S 30s
        self.assertEqual(r["need"], 130)        # (1+1)*60 + (40-30)
        self.assertFalse(r["ok"])

    def test_keep_range(self):
        rows, fitted = F.analyze(self._args(keep_range=["ErrorsHigh"]), F.load([RULES]))
        r = {x["rule"]: x for x in rows}["ErrorsHigh"]
        self.assertTrue(r["keep"])
        self.assertEqual(r["need_anchored"], 70)                 # (1+1)*30 + 10
        exprs = [x["expr"] for _, d in fitted for g in d["groups"] for x in g["rules"]]
        self.assertIn('rate(http_errors_total{job="api"}[1m]) > 1', exprs)   # not rewritten

    def test_cli_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "fit_contracts.py"), "--rules", RULES,
                                "--stack", "prometheus", "--scrape-interval", "30s", "--scrape-duration", "10s",
                                "--eval-interval", "30s", "--out", d], capture_output=True, text=True)
            self.assertEqual(p.returncode, 1, p.stderr)
            with open(os.path.join(d, "contracts-fit.md"), encoding="utf-8") as f:
                self.assertIn("方案 A", f.read())

    # ---- sim-mysql findings ----
    def _db(self, **kw):
        base = dict(S=30.0, d=None, E=30.0, O=0.0, k=1, T=None, timeout=10.0, job_interval={"mysqld": 30.0},
                    job_duration={"mysqld": 9.8}, job_p99={"mysqld": 6.0}, job_timeout={"mysqld": 10.0},
                    job_failrun={"mysqld": 3}, assumptions=[], cfg_jobs={"mysqld"})
        base.update(kw)
        return self._args(**base)

    def test_default_used_is_said_per_row(self):
        a = self._db()
        rows, _ = F.analyze(a, F.load([FOR_RULES]))
        r = {x["rule"]: x for x in rows}["WrongJob"]
        self.assertEqual(r["job"], "mysql")
        self.assertTrue(r["nodata"])                     # job not in the config: no data, never 符合
        self.assertFalse(r["ok"])
        self.assertIn("job mysql 不在 config 裡", r["notes"])
        ass = F.assumptions_for(a, rows, [])
        self.assertTrue(any("不在 config" in x for x in ass))
        md = F.report(a, rows, F.env_checks(a), ass, [])
        self.assertIn("沒有資料，契約不適用", md)
        sec2 = md.split("## 2.")[1].split("## 3.")[0].split("### 沒有資料")[0]
        self.assertNotIn("`WrongJob`", sec2)
        # without a config the same job falls back to the defaults, said per row
        a2 = self._db(cfg_jobs=None)
        rows2, _ = F.analyze(a2, F.load([FOR_RULES]))
        r2 = {x["rule"]: x for x in rows2}["WrongJob"]
        self.assertIn("用預設值", r2["src"])
        self.assertIn("d = τ", r2["defaults"])          # τ 10s known: worst case d = τ, not S
        self.assertTrue(any("用預設值：mysql" in x for x in F.assumptions_for(a2, rows2, [])))
        ok = {x["rule"]: x for x in rows}["SlowBurst"]
        self.assertEqual(ok["defaults"], [])
        self.assertFalse(any("d 沒給" in x and "mysqld" in x for x in ass))

    def test_option_b_feasibility(self):
        a = self._db()
        rows, _ = F.analyze(a, F.load([FOR_RULES]))
        r = {x["rule"]: x for x in rows}["SlowBurst"]
        self.assertFalse(r["ok"])                                   # 1m < (2+3)*30 + 9.8
        smin, why, tau = F.s_floor(r["params"])
        self.assertEqual(tau, 11)                                   # ceil(9.8 / 0.9)
        self.assertEqual(smin, 12)                                  # S > τ
        cell = F.b_text(r["opt_b"]["scrape_interval"], r["params"])   # (60 - 9.8) / 5 = 10.04 < 12
        self.assertTrue(cell.startswith("做不到"), cell)
        self.assertIn("d_max 9.8s", cell)
        ok = F.b_text(r["opt_b"]["scrape_interval_with_offset"], r["params"])   # 60 / 5 = 12
        self.assertTrue(ok.startswith("12s"), ok)

    def test_for_reset_recommendation(self):
        a = self._db()
        fors = {f["rule"]: f for f in F.for_checks(a, F.load([FOR_RULES]))}
        self.assertIn("ThreadsHigh", fors)
        self.assertNotIn("ExporterDown", fors)        # up is written on a failed scrape
        self.assertNotIn("Missing", fors)             # absent(): opposite direction
        self.assertNotIn("NoFor", fors)
        self.assertNotIn("SlowBurst", fors)           # has a range: checked by range-window
        f = fors["ThreadsHigh"]
        self.assertEqual(f["k"], 3)
        self.assertEqual(f["need"], 4 * 30 + 9.8)     # (1+k)·I + d
        self.assertEqual(f["R"], 150)                 # nice_up: 30s steps above 2m
        md = F.report(a, [], F.env_checks(a), [], list(fors.values()))
        self.assertIn("last_over_time(mysql_global_status_threads_running{job=\"mysqld\"}[2m30s])", md)
        self.assertIn("推論", md)
        self.assertIn("keep_firing_for", md)
        self.assertEqual(F.for_checks(self._db(stack="vmalert"), F.load([FOR_RULES])), [])

    def test_report_wording_is_advice_first(self):
        a = self._db()
        rows, _ = F.analyze(a, F.load([FOR_RULES]))
        md = F.report(a, rows, F.env_checks(a), [], F.for_checks(a, F.load([FOR_RULES])))
        self.assertNotIn("不合格", md)
        self.assertNotIn("規格不合", md)
        self.assertIn("不符合契約", md)
        self.assertLess(md.index("## 建議先做"), md.index("## 1. 環境本身"))
        self.assertIn("**建議**", md)
        self.assertIn("**代價**", md)
        self.assertIn("d_max 9.8s", md)

    def test_keep_range_row_says_kept(self):
        a = self._db(keep_range=["SlowBurst"])
        rows, _ = F.analyze(a, F.load([FOR_RULES]))
        md = F.report(a, rows, F.env_checks(a), [], [])
        row = [l for l in md.splitlines() if l.startswith("| `SlowBurst`") and "range-window" in l][0]
        self.assertIn("保留", row)
        self.assertNotIn("`[3m]`", row)

    def test_config_reads_intervals(self):
        a = self._args(S=None, E=None, O=None, timeout=None, job_interval={"mysqld": 15.0}, job_timeout={},
                       assumptions=[])
        F.apply_config(a, [os.path.join(ROOT, "tests", "fixtures", "fit", "prometheus.yml")])
        self.assertEqual((a.S, a.timeout, a.E, a.O), (30.0, 10.0, 30.0, 0.0))
        self.assertEqual(a.job_interval, {"mysqld": 15.0, "fast": 5.0, "slow": 120.0})   # explicit flag wins
        self.assertEqual(a.job_timeout["fast"], 5.0)        # global τ capped at the job's interval
        self.assertEqual(a.job_timeout["slow"], 60.0)
        self.assertEqual(a.cfg_jobs, {"mysqld", "fast", "slow"})

    def test_cli_config_without_scrape_flags(self):
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "fit_contracts.py"), "--rules", FOR_RULES,
                                "--stack", "prometheus", "--config", os.path.join(ROOT, "tests", "fixtures", "fit", "prometheus.yml"),
                                "--job-duration", "mysqld=9.8s", "--out", d], capture_output=True, text=True)
            self.assertEqual(p.returncode, 1, p.stderr)
            self.assertNotIn("不合格", p.stdout)
            with open(os.path.join(d, "contracts-fit.md"), encoding="utf-8") as f:
                md = f.read()
            self.assertIn("由 config 讀入", md)


if __name__ == "__main__":
    unittest.main()
