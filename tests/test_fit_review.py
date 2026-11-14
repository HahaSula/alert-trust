"""fit_contracts.py calculation fixes found by the four simulated reviews (fixtures: tests/fixtures/fit-review)."""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import fit_contracts as F  # noqa: E402

FX = os.path.join(ROOT, "tests", "fixtures", "fit-review")
RULES = os.path.join(FX, "rules.yml")
PROM = os.path.join(FX, "prometheus.yml")
VMAG = os.path.join(FX, "vmagent.yml")
PG = os.path.join(FX, "pg-rules.yml")
TOOL = os.path.join(ROOT, "tools", "fit_contracts.py")


def args(stack="prometheus", config=PROM, kind=None, **kw):
    a = type("A", (), {})()
    a.stack, a.S, a.d, a.E, a.O, a.k, a.T = stack, None, None, None, None, 1, None
    a.lookback, a.latency, a.eval_delay, a.query_step, a.dedup = 300.0, None, 30.0, 300.0, None
    a.job_interval, a.job_duration, a.job_timeout, a.job_p99, a.exporter_deadline, a.job_failrun = {}, {}, {}, {}, {}, {}
    a.timeout, a.p99, a.keep_range, a.assumptions, a.tau_clamped = None, None, [], [], {}
    a.job_exporter, a.job_exporter_flag, a.sources_empty, a.cfg_jobs = {}, set(), {}, None
    a.prom_version, a.prom_version_text = None, ""
    a.config_kind = kind or ("vmagent" if stack == "vmalert" else "prometheus")
    for k, v in kw.items():
        setattr(a, k, v)
    if config:
        F.apply_config(a, [config])
    F.clamp_timeouts(a)
    if a.O is None:
        a.O = 0.0
    return a


def run(a, files):
    docs = F.load(files)
    F.infer_exporters(a, docs)
    rows, fitted = F.analyze(a, docs)
    skipped = []
    fors = F.for_checks(a, docs, skipped)
    checks = F.env_checks(a)
    md = F.report(a, rows, checks, F.assumptions_for(a, rows, fors), fors, skipped)
    return {r["rule"]: r for r in rows}, rows, fitted, {f["rule"]: f for f in fors}, skipped, checks, md


class AbsenceRulesTest(unittest.TestCase):
    """1. §5 never suggests last_over_time for rules that detect absence."""

    def test_skipped_with_reason(self):
        _, _, _, fors, skipped, _, md = run(args(), [RULES])
        self.assertIn("ThreadsHigh", fors)
        why = {s["rule"]: s["why"] for s in skipped}
        self.assertIn("unless", why["CollectorsMissing"])
        self.assertIn("absence_covered_by", why["DbDownCovered"])
        self.assertIn("名稱", why["ScriptMissing"])
        for r in ("CollectorsMissing", "DbDownCovered", "ScriptMissing", "ExporterDown"):
            self.assertNotIn(r, fors)
        self.assertNotIn("last_over_time(mysql_up", md)
        self.assertIn("**不適用**", md)

    def test_detects_absence(self):
        self.assertTrue(F.detects_absence({"expr": "absent(x{job='a'})"}))
        self.assertTrue(F.detects_absence({"expr": 'up{job="a"} == 0 and x > 1'}))
        self.assertIsNone(F.detects_absence({"alert": "ThreadsHigh", "expr": "x > 1"}))


class VmagentClampTest(unittest.TestCase):
    """2. vmagent clamps τ > S to S silently; the effective τ is used everywhere and reported."""

    def test_clamp_reported_and_used(self):
        a = args("vmalert", VMAG, job_duration={"pg": 18.0})
        self.assertEqual(a.job_timeout["pg"], 15.0)
        self.assertEqual(a.tau_clamped, {"pg": 20.0})
        checks = F.env_checks(a)
        bad = [t for t, ok, *_ in checks if not ok]
        self.assertTrue(any("設定 20s > S 15s" in t and "有效 τ = 15s" in t for t in bad), bad)
        # compared against the effective 15s, not 20s: 18s cannot be one scrape under τ 15s → a contradiction to ask about
        self.assertTrue(any("耗時數據和設定矛盾" in t and "d_max 18s > τ 15s" in t for t in bad), bad)
        self.assertEqual(F.s_floor(F.job_params(a, "pg"))[2], 20)

    def test_prometheus_kind_keeps_tau_and_flags_load_error(self):
        a = args("prometheus", VMAG)
        self.assertEqual(a.job_timeout["pg"], 20.0)
        self.assertEqual(a.tau_clamped, {})
        fixes = [fix for t, ok, fix, *_ in F.env_checks(a) if not ok and "τ 20s > S 15s" in t]
        self.assertTrue(fixes and "拒絕載入" in fixes[0])
        self.assertEqual(F.job_params(a, "pg")["tau"], 15.0)   # min(τ, S) in the window math anyway

    def test_cli_infers_kind_from_stack(self):
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run([sys.executable, TOOL, "--rules", PG, "--stack", "vmalert", "--config", VMAG, "--out", d],
                               capture_output=True, text=True)
            self.assertNotIn("Traceback", p.stderr)
            with open(os.path.join(d, "contracts-fit.md"), encoding="utf-8") as f:
                md = f.read()
        self.assertIn("vmagent", md)
        self.assertIn("設定 20s > S 15s", md)


class BaselineTest(unittest.TestCase):
    """3. floors and the vmalert formula."""

    def test_floors(self):
        a = args(config=None, S=30.0, E=30.0, job_interval={"prometheus": 30.0, "mysqld": 30.0},
                 job_duration={"prometheus": 0.2, "mysqld": 9.0}, job_p99={"prometheus": 0.1, "mysqld": 6.0},
                 job_timeout={"mysqld": 15.0}, job_exporter={"mysqld": "mysqld_exporter"})
        out = F.baseline(a)
        prom = [l for l in out.splitlines() if l.startswith("| prometheus")][0]
        self.assertIn("| 2s |", prom)                         # τ never below 2s
        self.assertIn("1s（只有不讀 header", prom)            # X never below 1s
        my = [l for l in out.splitlines() if l.startswith("| mysqld")][0]
        self.assertIn("讀 header：τ − `--timeout-offset` 0.25s", my)

    def test_truncated_dmax_is_not_used_as_is(self):
        a = args(config=None, S=30.0, E=30.0, job_interval={"mysqld": 30.0}, job_duration={"mysqld": 9.8},
                 job_timeout={"mysqld": 10.0})
        row = [l for l in F.baseline(a).splitlines() if l.startswith("| mysqld")][0]
        self.assertIn("被截斷", row)

    def test_s_not_lowered_below_15s(self):
        a = args(config=None, S=60.0, E=60.0, job_interval={"x": 60.0}, job_duration={"x": 1.0})
        self.assertIn("| 1m |", [l for l in F.baseline(a).splitlines() if l.startswith("| x")][0])

    def test_vmalert_formula(self):
        a = args("vmalert", None, S=15.0, E=15.0, job_interval={"pg": 15.0}, job_duration={"pg": 5.0},
                 dedup=30.0, latency=45.0)
        out = F.baseline(a)
        self.assertIn("max(0, L − D)", out)
        row = [l for l in out.splitlines() if l.startswith("| pg")][0]
        self.assertIn("| 30s（dedup 30s > S", row)          # I = max(S, dedup)
        self.assertIn("1m15s（n = 1）", row)                 # (1+1)·30 + (45 − 30)
        self.assertNotIn("query_offset", out)


class UnknownDurationTest(unittest.TestCase):
    """4. d unknown: d = τ when τ is known, else S."""

    def test_d_is_tau(self):
        a = args()
        P = F.job_params(a, "mysqld")
        self.assertEqual((P["d"], P["assumed"]), (10.0, "τ"))
        by, rows, *_ = run(a, [RULES])
        self.assertTrue(any("d = τ" in x for x in F.assumptions_for(a, rows, [])))
        b = args(config=None, S=30.0, E=30.0)
        self.assertEqual(F.job_params(b, "x")["assumed"], "S")


class DedupPlanBTest(unittest.TestCase):
    """5. dedup > S: shortening scrape_interval does not help."""

    def test_dedup_bound(self):
        a = args("vmalert", VMAG, dedup=30.0, latency=45.0)
        by, _, _, _, _, checks, md = run(a, [PG])
        r = by["PGRollbackBurst"]
        self.assertEqual(r["I"], 30.0)
        self.assertTrue(r["opt_b"]["dedup_bound"])
        self.assertIn("縮短 S 沒有用", md)
        self.assertIn("-dedup.minScrapeInterval", md)
        self.assertTrue(any("dedup 30s > S 15s" in t for t, ok, *_ in checks if not ok))


class RegexJobTest(unittest.TestCase):
    """6 + 8. regex job matchers resolve against config jobs; no match / not in config = no data."""

    def test_worst_job_and_no_data(self):
        by, _, _, _, _, _, md = run(args(), [RULES])
        self.assertEqual(by["EtcdRegex"]["job"], "etcd-probe")       # S 1m beats kube-etcd S 30s
        self.assertIn("取最壞", by["EtcdRegex"]["src"])
        for r in ("EtcdNone", "WrongJob"):
            self.assertTrue(by[r]["nodata"], r)
            self.assertFalse(by[r]["ok"], r)
        sec2 = md.split("## 2.")[1].split("### 沒有資料")[0]
        self.assertNotIn("`EtcdNone`", sec2)
        self.assertIn("沒有資料，契約不適用", md)

    def test_sources_json_marks_no_data(self):
        a = args(sources_empty=F.load_sources(os.path.join(FX, "sources.json")))
        _, _, _, fors, *_ = run(a, [RULES])
        self.assertEqual(fors["RedisMemory"]["selectors"], ['redis_memory_used_bytes{job="redis"}'])


class SubqueryTest(unittest.TestCase):
    """7. subqueries are ranges."""

    def test_rows(self):
        by, _, fitted, fors, *_ = run(args(change_range=["ShortSubquery"]), [RULES])
        r = by["LeaderLostRecently"]
        self.assertEqual((r["R"], r["n"], r["I"], r["func"]), (900, 2, 60, "increase"))
        self.assertTrue(r["ok"])
        self.assertNotIn("LeaderLostRecently", fors)                 # not "no range"
        s = by["ShortSubquery"]
        self.assertEqual((s["n"], s["I"], s["need"]), (1, 30, 70))
        self.assertFalse(s["ok"])
        exprs = [x["expr"] for _, d in fitted for g in d["groups"] for x in g["rules"]]
        self.assertTrue(any("[1m15s:30s]" in e for e in exprs), exprs)

    def test_parser(self):
        e = "max_over_time(count by (job) (x{job='a'})[1d:5m]) and increase((y)[15m:])"
        subs = F.subqueries(e)
        self.assertEqual([(s["R"], s["res"], s["func"]) for s in subs], [("1d", "5m", "max_over_time"), ("15m", None, "increase")])


class ExporterDeadlineTest(unittest.TestCase):
    """9 + 12. exporter defaults, X ≥ τ flagged with the real relation."""

    def test_inferred_redis(self):
        a = args()
        _, _, _, _, _, checks, _ = run(a, [RULES])
        self.assertEqual(a.job_exporter.get("redis"), "redis_exporter")
        self.assertEqual(a.job_exporter.get("mysqld"), "mysqld_exporter")
        bad = [(t, fix) for t, ok, fix, *_ in checks if not ok and "exporter 期限" in t]
        t, fix = [x for x in bad if "job redis" in x[0]][0]
        self.assertIn("X 15s ≥ τ 10s", t)
        self.assertIn("--connection-timeout", fix)
        self.assertNotIn("X 15s < τ", "".join(x[0] for x in bad))
        ok = [t for t, good, *_ in checks if good and "exporter 期限（job mysqld）" in t]
        self.assertTrue(ok and "X 9.8s < τ 10s" in ok[0], ok)

    def test_flag_and_explicit_value(self):
        a = args(job_exporter={"pg": "postgres_exporter"}, job_exporter_flag={"pg"}, config=VMAG, kind="vmagent")
        x, src = F.exporter_deadline(a, "pg", 15.0)
        self.assertEqual(x, 60.0)
        self.assertIn("--job-exporter", src)
        a.exporter_deadline = {"pg": 12.0}
        self.assertEqual(F.exporter_deadline(a, "pg", 15.0)[0], 12.0)

    def test_cli_rejects_unknown_exporter(self):
        p = subprocess.run([sys.executable, TOOL, "--rules", PG, "--stack", "prometheus", "--config", PROM,
                            "--job-exporter", "pg=foo_exporter", "--out", tempfile.gettempdir()], capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)


class VersionTest(unittest.TestCase):
    """10. --prom-version below 2.53: query_offset unavailable."""

    def test_old_prometheus(self):
        a = args(prom_version=(2, 48, 1), prom_version_text="2.48.1")
        *_, md = run(a, [RULES])
        self.assertIn("不可用：Prometheus 2.48.1 沒有 `query_offset`", md)
        self.assertNotIn("`query_offset` 10s，", md)
        b = args()
        *_, md2 = run(b, [RULES])
        self.assertNotIn("不可用：Prometheus", md2)


class VmalertLatencyTest(unittest.TestCase):
    """11. L unknown: L = S, not 0."""

    def test_l_equals_s(self):
        a = args("vmalert", VMAG)
        by, *_ = run(a, [PG])
        r = by["PostgresDeadlocks"]
        self.assertEqual(r["need"], (1 + 1) * 15 + max(0, 15 - 30))
        a2 = args("vmalert", None, S=60.0, E=60.0)
        self.assertEqual(F.need_window(a2, 60.0, 0, 0, 1, 1, 60.0), 2 * 60 + 30)    # L = S 60s > D 30s
        with tempfile.TemporaryDirectory() as d:
            subprocess.run([sys.executable, TOOL, "--rules", PG, "--stack", "vmalert", "--config", VMAG, "--out", d],
                           capture_output=True, text=True)
            with open(os.path.join(d, "contracts-fit.md"), encoding="utf-8") as f:
                md = f.read()
        self.assertIn("L = 該 job 的 S", md)
        self.assertIn("dedup.minScrapeInterval` 沒給", md)


if __name__ == "__main__":
    unittest.main()
