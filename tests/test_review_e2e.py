"""End-to-end answer, notify-within, version gating, deployment fit, Watchdog route and no_source
(fixtures: tests/fixtures/deploy, tests/fixtures/inventory/pg-vm)."""
import json
import os
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import check_coverage as CC  # noqa: E402
import check_metric_sources as MS  # noqa: E402
import fit_contracts as FC  # noqa: E402
import inventory as INV  # noqa: E402
import review as RV  # noqa: E402

FX = os.path.join(ROOT, "tests", "fixtures", "deploy")
RULES, PROM = os.path.join(FX, "rules.yml"), os.path.join(FX, "prometheus.yml")
AM, AM_DMS = os.path.join(FX, "alertmanager.yml"), os.path.join(FX, "alertmanager-dms.yml")


def inventory(am=AM, answers=None, tmp=None):
    out = os.path.join(tmp, "inventory.yaml")
    argv = ["--rules", RULES, "--config", PROM, "--alertmanager", am, "--out", out]
    if answers:
        p = os.path.join(tmp, "answers.yaml")
        with open(p, "w", encoding="utf-8") as f:
            yaml.safe_dump(answers, f)
        argv += ["--answers", p]
    RV._quiet(INV.main, argv)
    with open(out, encoding="utf-8") as f:
        return yaml.safe_load(f), out


def kinds(inv, kind):
    return [f for f in inv["findings_from_files"] if f["kind"] == kind]


class DeploymentFitTest(unittest.TestCase):
    def test_from_config(self):
        with tempfile.TemporaryDirectory() as t:
            inv, _ = inventory(tmp=t)
        na = {(f["basis"], n): f for f in kinds(inv, "rule_not_applicable") for n in f["rules"]}
        self.assertIn(("feature", "DbProbeExporterDown"), na)  # per-host exporter, no __param_target
        self.assertIn("直接抓每台的 exporter", na[("feature", "DbProbeExporterDown")]["fact"])
        self.assertTrue(na[("feature", "DbProbeExporterDown")]["fact"].startswith("這條 rule 不適用這個部署"))
        self.assertIn(("label", "DbNoWritable"), na)  # by (cluster), targets have no cluster label
        self.assertNotIn(("feature", "DbHeartbeatLag"), na)  # not in config: a question, not a verdict
        q = [x for x in inv["questions"] if x["answer_key"] == "features"]
        self.assertTrue(q and "heartbeat" in q[0]["q"] and "semi-sync" in q[0]["q"])

    def test_from_answers_features(self):
        with tempfile.TemporaryDirectory() as t:
            inv, _ = inventory(answers={"features": {"mysqld": ["replication-replica"]}}, tmp=t)
        na = {n: f for f in kinds(inv, "rule_not_applicable") for n in f["rules"]}
        self.assertIn("heartbeat", na["DbHeartbeatLag"]["fact"])
        self.assertIn("semi-sync", na["DbSemiSyncOff"]["fact"])
        self.assertNotIn("DbDown", na)

    def test_coverage_uses_env_features(self):
        with tempfile.TemporaryDirectory() as t:
            inv, _ = inventory(tmp=t)
        env = {"components": [], "targets": [{"id": "db-1", "type": "mysql", "job": "mysqld", "features": ["heartbeat"]}]}
        rows = {r["rule"]: r for r in CC.deployment_rules(env, inv)}
        self.assertEqual(rows["DbSemiSyncOff"]["status"], "rule_not_applicable")
        self.assertNotIn("DbHeartbeatLag", rows)
        self.assertIn("這條 rule 不適用這個部署", CC.render_deployment(list(rows.values())))


class WatchdogRouteTest(unittest.TestCase):
    def test_inherits_root(self):
        with tempfile.TemporaryDirectory() as t:
            inv, _ = inventory(tmp=t)
        wr = inv["alertmanager"]["watchdog_route"]
        self.assertFalse(wr["dedicated"])
        self.assertEqual(wr["repeat_interval"], "3h")
        self.assertEqual(wr["dms_min_wait"], "3h9m")  # 4 × max(1m, 15s) + 3h + 5m
        self.assertIn("https://dms.example.invalid/ping", wr["template"])
        self.assertTrue(kinds(inv, "watchdog_no_dedicated_route"))

    def test_dedicated(self):
        with tempfile.TemporaryDirectory() as t:
            inv, _ = inventory(am=AM_DMS, tmp=t)
        wr = inv["alertmanager"]["watchdog_route"]
        self.assertTrue(wr["dedicated"])
        self.assertEqual(wr["dms_min_wait"], "6m")  # 4 × 1m + 1m + 1m
        self.assertFalse(kinds(inv, "watchdog_no_dedicated_route"))

    def test_template_is_valid_yaml(self):
        doc = yaml.safe_load(INV.DMS_ROUTE_TEMPLATE)
        self.assertTrue(doc["receivers"][0]["webhook_configs"][0]["url"].startswith("https://"))


class NotifyWithinTest(unittest.TestCase):
    def test_formula_and_dms(self):
        with tempfile.TemporaryDirectory() as t:
            _, inv_path = inventory(tmp=t)
            RV._quiet(FC.main, ["--inventory", inv_path, "--notify-within", "2m30s", "--out", t])
            with open(os.path.join(t, "contracts-fit.json"), encoding="utf-8") as f:
                fit = json.load(f)
            with open(os.path.join(t, "contracts-fit.md"), encoding="utf-8") as f:
                md = f.read()
        n = {x["rule"]: x for x in fit["notify"]}
        # S 15s + d (= τ) 10s + E 15s + ⌈1m/15s⌉·15s + group_wait 30s
        self.assertEqual(n["DbDown"]["total"], 130.0)
        self.assertTrue(n["DbDown"]["ok"])
        self.assertFalse(n["DbProbeExporterDown"]["ok"])
        self.assertNotIn("DbHeartbeatLag", n)  # warning, not critical
        self.assertIn("推算", md)
        self.assertIn("S 15s + d 10s + E 15s + ⌈for/E⌉·E 1m + group_wait 30s = 2m10s", md)
        self.assertFalse(fit["dms"]["ok"])
        self.assertIn("外部觀察者", md)

    def test_vmalert_formula(self):
        a = type("A", (), {})()
        a.stack, a.S, a.E, a.O, a.d, a.latency, a.eval_delay, a.notify_within = "vmalert", 30.0, 30.0, 0.0, None, 45.0, 30.0, 300.0
        a.job_interval, a.job_duration, a.job_timeout, a.timeout, a.p99, a.job_p99, a.cfg_jobs = {}, {}, {}, 10.0, None, {}, None
        docs = [("x", {"groups": [{"name": "g", "rules": [{"alert": "A", "expr": "x > 1", "for": "1m",
                                                           "labels": {"severity": "critical"}}]}]})]
        n = FC.notify_checks(a, docs, None)[0]
        self.assertEqual(n["total"], 30 + 10 + 30 + 45 + 60 + 30)  # max(L 45s, D 30s)
        self.assertIn("max(L, D) 45s", n["formula"])


class VersionGatingTest(unittest.TestCase):
    def _md(self, version):
        with tempfile.TemporaryDirectory() as t:
            argv = ["--rules", os.path.join(ROOT, "tests", "fixtures", "fit-review", "rules.yml"), "--config",
                    os.path.join(ROOT, "tests", "fixtures", "fit-review", "prometheus.yml"), "--out", t]
            RV._quiet(FC.main, argv + (["--prom-version", version] if version else []))
            with open(os.path.join(t, "contracts-fit.md"), encoding="utf-8") as f:
                return f.read()

    def test_unknown_version_marks_options(self):
        md = self._md(None)
        self.assertIn(FC.QO_UNKNOWN, md)
        self.assertNotIn(FC.QO_UNKNOWN, self._md("2.55.1"))
        self.assertIn("沒有 `query_offset`", self._md("2.48.1"))

    def test_review_advice(self):
        self.assertIn(FC.QO_UNKNOWN, RV.offset_options({"stack": "prometheus", "prom_version": None}))
        self.assertIn(FC.ANCHORED_UNKNOWN, RV.offset_options({"stack": "prometheus"}))
        self.assertIn("沒有 `query_offset`", RV.offset_options({"stack": "prometheus", "prom_version": "2.48.1"}))
        self.assertNotIn("版本未知", RV.offset_options({"stack": "prometheus", "prom_version": "3.5.0"}))


class EndToEndSummaryTest(unittest.TestCase):
    def test_first_sentence_and_four_parts(self):
        with tempfile.TemporaryDirectory() as t:
            RV._quiet(RV.main, ["--rules", RULES, "--config", PROM, "--alertmanager", AM, "--notify-within", "2m", "--out", t])
            with open(os.path.join(t, "summary.md"), encoding="utf-8") as f:
                md = f.read()
        sec = md.split("## 會不會在 2m 內通知到人（端到端）", 1)[1]
        first = sec.strip().splitlines()[0]
        self.assertTrue(first.startswith("**2m 內，"), first)
        self.assertIn("page", first)
        for part in ("偵測（rule 的邏輯）", "載入（`rule_files`／`-rule`）", "路由（Alertmanager）", "自我監控（Watchdog／DMS／觀察者）"):
            self.assertIn(part, sec)
        self.assertIn("推算", sec)
        self.assertLess(md.index("端到端"), md.index("建議先處理的 3 件事"))

    def test_without_time(self):
        with tempfile.TemporaryDirectory() as t:
            RV._quiet(RV.main, ["--rules", RULES, "--config", PROM, "--out", t])
            with open(os.path.join(t, "summary.md"), encoding="utf-8") as f:
                md = f.read()
        self.assertIn("## 會不會通知到人（端到端）", md)
        self.assertIn("路由（Alertmanager） | 未知", md)


class NoSourceTest(unittest.TestCase):
    def test_vmalert_metrics_without_vmalert_job(self):
        rules = MS.load_rules([RULES])
        rows = {(r["rule"], r["metric"]): r for r in MS.check(rules, MS.load_config(PROM), {}, None, ROOT)}
        r = rows[("RulerGroupSlow", "vmalert_iteration_duration_seconds")]
        self.assertEqual(r["status"], "no_source")
        self.assertIn("沒有來源（config 沒有抓 vmalert）", r["detail"])
        self.assertIn("no_source", MS.EMPTY_STATUSES)

    def test_vmalert_job_present(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "prometheus.yml")
            with open(PROM, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            doc["scrape_configs"].append({"job_name": "meta", "static_configs": [{"targets": ["ruler-1:8880"]}]})
            with open(p, "w", encoding="utf-8") as f:
                yaml.safe_dump(doc, f)
            rules = MS.load_rules([RULES])
            rows = {(r["rule"], r["metric"]): r for r in MS.check(rules, MS.load_config(p), {}, None, ROOT)}
        self.assertEqual(rows[("RulerGroupSlow", "vmalert_iteration_duration_seconds")]["status"], "needs_input")

    def test_via_inventory(self):
        with tempfile.TemporaryDirectory() as t:
            _, inv_path = inventory(tmp=t)
            RV._quiet(MS.main, ["--inventory", inv_path, "--out", t])
            with open(os.path.join(t, "metric-sources.json"), encoding="utf-8") as f:
                rows = json.load(f)["rows"]
        self.assertIn("no_source", {r["status"] for r in rows if r["rule"] == "RulerGroupSlow"})


class QuestionsTest(unittest.TestCase):
    def test_version_question_asks_for_api_output(self):
        with tempfile.TemporaryDirectory() as t:
            inv, _ = inventory(tmp=t)
        q = next(x for x in inv["questions"] if x["answer_key"] == "version")
        self.assertIn("/api/v1/status/buildinfo", q["q"])
        self.assertIn("/api/v1/status/flags", q["q"])
        self.assertLessEqual(sum(1 for x in inv["questions"] if x["ask_now"]), 3)


class TemplateRuleTest(unittest.TestCase):
    def test_group_slow_is_per_group_ratio(self):
        with open(os.path.join(ROOT, "rules", "victoriametrics", "meta.yml"), encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        r = next(x for g in doc["groups"] for x in g["rules"] if x.get("alert") == "MetaGroupSlow")
        self.assertIn("vmalert_iteration_interval_seconds", r["expr"])
        self.assertIn("> 0.8", r["expr"])

    def test_baseline_clamp_cites_ref(self):
        with open(os.path.join(ROOT, "docs", "tools", "baseline.md"), encoding="utf-8") as f:
            row = next(x for x in f if x.startswith("| τ > S |"))
        self.assertIn("[^bl-vmag-timeout-clamp]", row)
        with open(os.path.join(ROOT, "catalog", "refs.d", "baseline.yaml"), encoding="utf-8") as f:
            ref = yaml.safe_load(f)["refs"]["bl-vmag-timeout-clamp"]
        self.assertEqual(ref["line"], 931)


if __name__ == "__main__":
    unittest.main()
