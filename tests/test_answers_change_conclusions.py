"""Answers that must change conclusions: must_ask effects (nested / flat facts), the notification requirement
question, where k comes from, the τ re-measure loop, promtool version mismatch and the skill rules for
deliverables (fixtures: tests/fixtures/inventory/etcd)."""
import json
import os
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import catalog as C  # noqa: E402
import check_coverage as CC  # noqa: E402
import fit_contracts as FC  # noqa: E402
import inventory as INV  # noqa: E402
import inventory_use as IU  # noqa: E402
import review as RV  # noqa: E402

FXI = os.path.join(ROOT, "tests", "fixtures", "inventory", "etcd")
RULES, PROM = os.path.join(FXI, "etcd-rules.yml"), os.path.join(FXI, "prometheus.yml")
SLOW = "etcdGRPCRequestsSlow"


def write(d, name, data):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True)
    return p


def inventory(t, answers=None, extra=()):
    out = os.path.join(t, "inv.yaml")
    argv = ["--rules", RULES, "--config", PROM, "--out", out, *extra]
    if answers is not None:
        argv += ["--answers", write(t, "answers.yaml", answers)]
    RV._quiet(INV.main, argv)
    with open(out, encoding="utf-8") as f:
        return yaml.safe_load(f)


def review(t, answers, extra=()):
    out = os.path.join(t, "report")
    RV._quiet(RV.main, ["--rules", RULES, "--config", PROM, "--answers", write(t, "answers.yaml", answers),
                        "--promtool-version", "none", "--out", out, *extra])
    rd = lambda n: read(os.path.join(out, n))  # noqa: E731
    return out, rd


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def rule(inv, name):
    return next(r for r in inv["rules"] if r["name"] == name)


class EffectSchemaTest(unittest.TestCase):
    def card(self, **eff):
        q = {"id": "lvl", "question": "q?", "why": "w", "refs": ["r1"], "effects": [eff]}
        return {"_path": "x.yaml", "signals": [{"id": "sig"}], "must_ask": [q]}

    def test_valid_and_invalid(self):
        refs = {"r1": {}}
        self.assertEqual(C.check_must_ask(self.card(when="basic", verdict="no_data", metrics=["m_bucket"]), refs), [])
        self.assertEqual(C.check_must_ask(self.card(when=["a", "b"], verdict="available", signals=["sig"]), refs), [])
        bad = [self.card(when="basic", verdict="gone", metrics=["m"]),          # unknown verdict
               self.card(when="basic", verdict="no_data"),                      # neither metrics nor signals
               self.card(when="basic", verdict="no_data", signals=["nope"]),    # signal not in card
               self.card(verdict="no_data", metrics=["m"]),                     # no when
               self.card(when="basic", verdict="no_data", metrics=["m"], extra=1)]
        for c in bad:
            self.assertTrue(C.check_must_ask(c, refs), c)
        c = self.card(when="basic", verdict="no_data", metrics=["m"])
        c["must_ask"][0]["answer_key"] = "latency"   # effects only on facts.* answers
        self.assertTrue(any("facts" in e for e in C.check_must_ask(c, refs)))

    def test_catalog_etcd_declares_effect(self):
        etcd = next(c for c in C.load_components() if c["id"] == "etcd")
        q = next(q for q in etcd["must_ask"] if q["id"] == "metrics-level")
        self.assertTrue(any(e["verdict"] == "no_data" and "grpc_server_handling_seconds_bucket" in e["metrics"]
                            for e in q["effects"]))

    def test_when_matching(self):
        self.assertTrue(C.effect_matches("basic", "basic"))
        self.assertTrue(C.effect_matches("basic", "Basic（3.5.9）"))
        self.assertTrue(C.effect_matches(["basic"], "--metrics=basic"))
        self.assertFalse(C.effect_matches("basic", "extensive"))
        self.assertFalse(C.effect_matches("basic", None))


class FactsNestedTest(unittest.TestCase):
    def test_flat_and_nested_are_equal(self):
        self.assertEqual(IU.flat_facts({"etcd": {"metrics-level": "basic"}}), {"etcd.metrics-level": "basic"})
        self.assertEqual(IU.flat_facts({"etcd.metrics-level": "basic"}), {"etcd.metrics-level": "basic"})

    def test_nested_answer_is_read(self):
        for facts in ({"etcd": {"metrics-level": "basic"}}, {"etcd.metrics-level": "basic"}):
            with tempfile.TemporaryDirectory() as t:
                inv = inventory(t, {"facts": facts})
            self.assertEqual(inv["facts"]["etcd.metrics-level"], {"value": "basic", "from": "user"})
            self.assertNotIn("must_ask.etcd.metrics-level", [q["id"] for q in inv["questions"]])
            self.assertEqual(rule(inv, SLOW)["fires"]["value"], "never")
            f = [x for x in inv["findings_from_files"] if x["kind"] == "must_ask_no_data"]
            self.assertTrue(f and SLOW in f[0]["rules"])
            self.assertEqual(inv["must_ask_effects"][0]["rules"], [SLOW])


class EffectsReviewTest(unittest.TestCase):
    def test_basic_rejudges_every_report(self):
        with tempfile.TemporaryDirectory() as t:
            out, rd = review(t, {"facts": {"etcd": {"metrics-level": "basic"}}})
            ms = json.loads(read(os.path.join(out, "metric-sources.json")))["rows"]
            fit = json.loads(read(os.path.join(out, "contracts-fit.json")))
            summary = rd("summary.md")
        row = next(r for r in ms if r["rule"] == SLOW)
        self.assertEqual(row["status"], "setting_absent")
        self.assertIn("這個設定下沒有資料", row["detail"])
        self.assertTrue(all(r["nodata"] for r in fit["rows"] if r["rule"] == SLOW))
        self.assertIn("這個設定下沒有資料", summary)
        self.assertIn(SLOW, summary.split("## 建議先處理的 3 件事")[1])

    def test_extensive_confirms(self):
        with tempfile.TemporaryDirectory() as t:
            out, _ = review(t, {"facts": {"etcd.metrics-level": "extensive"}})
            ms = json.loads(read(os.path.join(out, "metric-sources.json")))["rows"]
        row = next(r for r in ms if r["rule"] == SLOW)
        self.assertNotEqual(row["status"], "setting_absent")
        self.assertNotEqual(row["status"], "needs_input")

    def test_coverage_signal_no_data(self):
        expr = next(r["expr"] for r in __import__("check_metric_sources").load_rules([RULES]) if r["name"] == SLOW)
        with tempfile.TemporaryDirectory() as t:
            envp = write(t, "env.yaml", {"name": "x", "components": [{"id": "prom", "type": "prometheus"}],
                                          "targets": [{"id": "etcd1", "type": "etcd", "job": "kube-etcd", "scraped_by": "prom"}]})
            env = CC.load_env(envp)
        rules = {"prom": [{"name": SLOW, "query": expr, "type": "alerting"}]}
        eff = IU.applied_effects({"etcd.metrics-level": "basic"})

        def status(effects):
            got = CC.evaluate(env, None, {1, 2, 3}, False, None, rules, None, None, effects)
            return next(i for i in got if i["component"] == "etcd1" and i["signal"] == "request-latency")
        self.assertNotEqual(status(None)["status"], "rule_no_data")
        it = status(eff)
        self.assertEqual(it["status"], "rule_no_data")
        self.assertIn("這個設定下沒有資料", it["note"])


class NotifyWithinTest(unittest.TestCase):
    def test_question_when_critical_and_no_requirement(self):
        with tempfile.TemporaryDirectory() as t:
            inv = inventory(t, {})
        q = next(x for x in inv["questions"] if x["id"] == "notify_within")
        self.assertIn(INV.NOTIFY_Q, q["q"])
        self.assertTrue(q["mandatory"] and q["ask_now"])
        self.assertEqual(inv["stack"]["notify_within"]["from"], "unknown")

    def test_answer_compared_in_summary(self):
        with tempfile.TemporaryDirectory() as t:
            inv = inventory(t, {"notify_within": "1m"})
            self.assertNotIn("notify_within", [x["id"] for x in inv["questions"]])
            self.assertEqual(inv["stack"]["notify_within"]["value"], "1m")
            _, rd = review(t, {"notify_within": "1m"})
            s = rd("summary.md")
        self.assertIn("在 1m 內通知到人", s)
        self.assertIn("超過 1m", s)

    def test_no_requirement_said_in_summary(self):
        with tempfile.TemporaryDirectory() as t:
            _, rd = review(t, {})
            s = rd("summary.md")
        self.assertIn("沒有通知時限", s)
        self.assertIn(INV.NOTIFY_Q, s)

    def test_flag_counts_as_answer(self):
        with tempfile.TemporaryDirectory() as t:
            inv = inventory(t, {}, extra=["--notify-within", "5m"])
        self.assertNotIn("notify_within", [x["id"] for x in inv["questions"]])


class KSourceTest(unittest.TestCase):
    def a(self, **kw):
        class A:
            pass
        a = A()
        a.k, a.job_failrun, a.job_failrun_assumed, a.job_fail_counts = 1, {}, {}, {}
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    def test_labels(self):
        self.assertEqual(FC.k_label(self.a(job_failrun={"j": 2}), "j"), "觀察到的連續失敗")
        lab = FC.k_label(self.a(job_fail_counts={"j": "failures_per_window 3"}), "j")
        self.assertIn("上限／假設", lab)
        self.assertNotIn("觀察到", lab)
        self.assertIn("假設", FC.k_label(self.a(), "j"))
        a = self.a(job_failrun={"j": 3}, job_failrun_assumed={"j": 3})
        self.assertNotEqual(FC.k_label(a, "j"), "觀察到的連續失敗")

    def test_answers_counts_vs_consecutive(self):
        rules = os.path.join(ROOT, "tests", "fixtures", "inventory", "etcd", "etcd-rules.yml")
        for meas, want, not_want in (({"up0_count": 14, "failures_per_window": 3}, "上限／假設", "觀察到的連續失敗"),
                                     ({"max_consecutive_failures": 2}, "觀察到的連續失敗", "上限／假設")):
            with tempfile.TemporaryDirectory() as t:
                ans = write(t, "a.yaml", {"measured": {"kube-etcd": meas}})
                RV._quiet(FC.main, ["--rules", rules, "--config", PROM, "--answers", ans, "--promtool-version", "none",
                                    "--out", t])
                md = read(os.path.join(t, "contracts-fit.md"))
            sec = md.split("## 5.")[1] if "## 5." in md else md
            self.assertIn(want, sec)
            self.assertNotIn(not_want, sec)

    def test_consecutive_query_and_question(self):
        q = FC.consecutive_query("mysqld", "15s", 2)
        self.assertIn('up{job=~"mysqld"} == bool 0', q)
        self.assertIn("[22.5s:15s]", q)   # (n − ½)·S: n steps whether the window is left-open or closed
        self.assertIn("[7d:15s]", q)
        with tempfile.TemporaryDirectory() as t:
            inv = inventory(t, {"measured": {"kube-etcd": {"d_max": "2s", "up0_count": 4}}})
        fq = [x for x in inv["questions"] if x["id"] == "failrun.kube-etcd"]
        self.assertTrue(fq and INV.FAILRUN_Q in fq[0]["q"] and "== bool 0" in fq[0]["q"])
        with tempfile.TemporaryDirectory() as t:
            inv = inventory(t, {"measured": {"kube-etcd": {"d_max": "2s", "up0_count": 4, "max_consecutive_failures": 1}}})
        self.assertFalse([x for x in inv["questions"] if x["id"] == "failrun.kube-etcd"])


class RemeasureLoopTest(unittest.TestCase):
    def P(self, d_measured, remeasure, tau=12.0):
        return {"S": 15.0, "d": tau, "measured": True, "assumed": "τ", "tau": tau, "p99": None, "defaults": [],
                "remeasure": remeasure, "d_measured": d_measured}

    def test_cut_off_d_does_not_feed_tau(self):
        P = self.P(19.5, 20.0, tau=25.0)   # cut off at the old τ 20s, proposal τ 25s → d = τ until re-measured
        smin, why, tau_i = FC.s_floor(P)
        self.assertIsNone(tau_i)   # no τ ≥ 25 / 0.9 = 28 requirement
        self.assertIn("需要重新量", why)
        b = FC.b_text(30.0, P)
        self.assertIn("需要重新量", b)
        self.assertNotIn("做不到", b)

    def test_uncut_old_measurement_used(self):
        smin, why, tau_i = FC.s_floor(self.P(8.0, 10.0))
        self.assertEqual(tau_i, 9.0)   # ⌈8 / 0.9⌉, from the real measurement

    def test_baseline_stops(self):
        with tempfile.TemporaryDirectory() as t:
            ans = write(t, "a.yaml", {"measured": {"db": {"tau": "20s"}}})
            RV._quiet(FC.main, ["--baseline", "--stack", "prometheus", "--job-interval", "db=30s", "--job-timeout",
                                "db=25s", "--job-duration", "db=19.5s", "--answers", ans, "--promtool-version", "none",
                                "--out", t])
            md = read(os.path.join(t, "contracts-fit.md"))
        row = next(l for l in md.splitlines() if l.startswith("| db "))
        self.assertIn("需要重新量", row)
        self.assertNotIn("28s", row)


class PromtoolVersionTest(unittest.TestCase):
    def test_warning(self):
        w = FC.promtool_warning("2.54.1", "3.1.0")
        self.assertIn("版本不同", w)
        self.assertIn("左開", w)
        self.assertIsNone(FC.promtool_warning("3.0.1", "3.1.0"))
        self.assertIn("版本未知", FC.promtool_warning("2.54.1", None))
        self.assertIsNone(FC.promtool_warning("2.54.1", "3.1.0", "vmalert"))
        self.assertIsNone(FC.promtool_warning(None, "3.1.0"))

    def test_in_reports(self):
        with tempfile.TemporaryDirectory() as t:
            out = os.path.join(t, "r")
            RV._quiet(RV.main, ["--rules", RULES, "--config", PROM, "--answers", write(t, "a.yaml", {"version": "3.1.0"}),
                                "--promtool-version", "2.54.1", "--out", out])
            fit = json.loads(read(os.path.join(out, "contracts-fit.json")))
            s = read(os.path.join(out, "summary.md"))
        self.assertIn("版本不同", fit["promtool_warning"])
        self.assertIn("版本不同", s.split("## 假設")[1])


class SkillGuidanceTest(unittest.TestCase):
    def test_rules_written(self):
        for p in ("plugins/core/skills/rule-review/SKILL.md", "plugins/core/references/five-sections.md"):
            s = read(os.path.join(ROOT, p))
            for want in ("proposed/", "expect-fail", "預期失敗", "前 3 題", "理由", INV.NOTIFY_Q, "上限／假設",
                         "這個設定下沒有資料"):
                self.assertIn(want, s, (p, want))
        s = read(os.path.join(ROOT, "plugins/core/skills/rule-review/SKILL.md"))
        self.assertIn("需要重新量", s)
        self.assertIn("主版本", s)


if __name__ == "__main__":
    unittest.main()
