"""Short-window detection uses fit_contracts' formula (dedup, vmalert L − D,
per-job S) and asks keep_range per rule; notify-within uses d = τ on failure paths (up == 0, absent) and flags a
measured d_max above τ; answers.yaml ids that match no question are reported; k's source is 「measured」 only for a
measured longest failure run; the redis_exporter / postgres_exporter cards ask maxmemory-policy / Patroni."""
import json
import os
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import catalog as C  # noqa: E402
import fit_contracts as FC  # noqa: E402
import inventory as INV  # noqa: E402
import review as RV  # noqa: E402

VMAGENT = """global: {scrape_interval: 15s, scrape_timeout: 10s}
scrape_configs:
  - job_name: pg
    static_configs: [{targets: ["pg:9187"]}]
"""
# [1m] on S 15s: (2+1)·15 + 10 = 55s < 60s → not short by the old rule; dedup 30s makes I 30s and L 45s adds 15s
VM_RULES = """groups:
  - name: g
    rules:
      - alert: ErrorsBurstA
        expr: rate(app_errors_a_total{job="pg"}[1m]) > 5
        labels: {severity: critical}
      - alert: ErrorsBurstB
        expr: rate(app_errors_b_total{job="pg"}[1m]) > 0
        labels: {severity: warning}
      - alert: ErrorsSlow
        expr: rate(app_requests_total{job="pg"}[10m]) > 0
        labels: {severity: warning}
"""
PROM = """global: {scrape_interval: 15s, scrape_timeout: 10s, evaluation_interval: 15s}
rule_files: [rules.yml]
alerting: {alertmanagers: [{static_configs: [{targets: ["am:9093"]}]}]}
scrape_configs:
  - job_name: api
    static_configs: [{targets: ["api:8080"]}]
  - job_name: am
    static_configs: [{targets: ["am:9093"]}]
"""
PROM_RULES = """groups:
  - name: g
    rules:
      - alert: ApiDown
        expr: up{job="api"} == 0
        for: 1m
        labels: {severity: critical}
      - alert: ApiErrors
        expr: api_errors{job="api"} > 1
        for: 1m
        labels: {severity: critical}
"""


def write(d, name, data):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    return p


def inventory(t, rules, config, answers=None, kind=None, name="inv.yaml"):
    out = os.path.join(t, name)
    argv = ["--rules", write(t, "rules.yml", rules), "--config", write(t, "cfg.yml", config), "--out", out]
    if kind:
        argv += ["--config-kind", kind]
    if answers is not None:
        argv += ["--answers", write(t, "a-" + name, answers)]
    RV._quiet(INV.main, argv)
    with open(out, encoding="utf-8") as f:
        return yaml.safe_load(f), out


def qids(inv):
    return [q["id"] for q in inv["questions"]]


class ShortWindowConsistencyTest(unittest.TestCase):
    # data latency L 45s > eval_delay 30s: fit_contracts needs (n+k)·30 + 15
    BASE = {"ruler": "vmalert", "dedup_interval": "30s", "latency": "45s", "notify_within": "5m"}

    def test_dedup_makes_window_short_and_fit_agrees(self):
        with tempfile.TemporaryDirectory() as t:
            inv, path = inventory(t, VM_RULES, VMAGENT, dict(self.BASE), kind="vmagent")
            r = next(x for x in inv["rules"] if x["name"] == "ErrorsBurstA")
            self.assertTrue(r["ranges"][0]["short"])
            self.assertEqual(r["ranges"][0]["data_interval"], "30s")
            self.assertIn("dedup", r["ranges"][0]["short_basis"])
            self.assertIn("keep_range.ErrorsBurstA", qids(inv))
            self.assertIn("keep_range.ErrorsBurstB", qids(inv))
            self.assertNotIn("keep_range.ErrorsSlow", qids(inv))
            # fit_contracts calls the same rows short
            o = os.path.join(t, "fit")
            RV._quiet(FC.main, ["--rules", os.path.join(t, "rules.yml"), "--inventory", path, "--stack", "vmalert",
                                "--dedup-interval", "30s", "--latency", "45s", "--out", o])
            with open(os.path.join(o, "contracts-fit.json"), encoding="utf-8") as f:
                js = json.load(f)
            rows = js.get("rows") or js.get("results") or []
            fit_short = {x["rule"] for x in rows if x.get("short")}
            inv_short = {x["name"] for x in inv["rules"] if any(g.get("short") for g in x.get("ranges") or [])}
            self.assertTrue(fit_short)
            self.assertLessEqual(fit_short, inv_short)

    def test_without_dedup_not_short(self):
        with tempfile.TemporaryDirectory() as t:
            inv, _ = inventory(t, VM_RULES, VMAGENT, {"ruler": "vmalert", "latency": "0s"}, kind="vmagent")
        r = next(x for x in inv["rules"] if x["name"] == "ErrorsBurstA")
        self.assertFalse(r["ranges"][0]["short"])

    def test_partial_unknown_keeps_other_rules(self):
        with tempfile.TemporaryDirectory() as t:
            a = dict(self.BASE, keep_range={"ErrorsBurstB": "不知道"})
            inv, _ = inventory(t, VM_RULES, VMAGENT, a, kind="vmagent")
            self.assertNotIn("keep_range.ErrorsBurstB", qids(inv))
            self.assertIn("keep_range.ErrorsBurstA", qids(inv))
            a = dict(self.BASE, unknown=["keep_range.ErrorsBurstA"])
            inv, _ = inventory(t, VM_RULES, VMAGENT, a, kind="vmagent", name="i2.yaml")
            self.assertNotIn("keep_range.ErrorsBurstA", qids(inv))
            self.assertIn("keep_range.ErrorsBurstB", qids(inv))
            self.assertTrue(next(x for x in inv["rules"] if x["name"] == "ErrorsBurstA")["keep_range"]["declined"])
            # exactly one keep_range question is mandatory per round
            self.assertEqual(sum(1 for q in inv["questions"] if q["id"].startswith("keep_range.") and q.get("mandatory")), 1)


class FailurePathTest(unittest.TestCase):
    def _args(self, **kw):
        a = type("A", (), {})()
        a.stack, a.S, a.E, a.O, a.d, a.latency, a.eval_delay, a.notify_within = "prometheus", 15.0, 15.0, 0.0, None, None, 30.0, 300.0
        a.job_interval, a.job_duration, a.job_timeout, a.timeout, a.p99, a.job_p99, a.cfg_jobs = \
            {"api": 15.0}, {"api": 0.9}, {"api": 10.0}, 10.0, None, {}, None
        a.k, a.job_failrun, a.src = 1, {}, {}
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    DOCS = [("x", yaml.safe_load(PROM_RULES))]

    def test_up_rule_uses_tau_value_rule_uses_measured(self):
        rows = {n["rule"]: n for n in FC.notify_checks(self._args(), self.DOCS, None)}
        up, val = rows["ApiDown"], rows["ApiErrors"]
        self.assertEqual(up["path"], "failure")
        self.assertEqual(up["d_basis"], "τ")
        self.assertEqual(dict(up["parts"])["d"], 10.0)
        self.assertEqual(val["path"], "value")
        self.assertEqual(dict(val["parts"])["d"], 0.9)
        self.assertAlmostEqual(up["total"] - val["total"], 9.1)
        src = {x["name"]: x["source"] for x in up["inputs"]}
        self.assertIn("失敗路徑", src["d"])
        md = "\n".join(FC.notify_section(self._args(), list(rows.values()), None))
        self.assertIn("d = τ（失敗路徑）", md)
        self.assertIn("值路徑", md)

    def test_absent_is_failure_path(self):
        docs = [("x", {"groups": [{"name": "g", "rules": [{"alert": "Gone", "expr": 'absent(api_errors{job="api"})',
                                                           "labels": {"severity": "critical"}}]}]})]
        n = FC.notify_checks(self._args(), docs, None)[0]
        self.assertEqual(n["path"], "failure")

    def test_dmax_above_tau_is_contradiction(self):
        a = self._args(job_duration={"api": 13.0}, job_timeout={"api": 12.0}, job_interval={"api": 15.0})
        n = next(x for x in FC.notify_checks(a, self.DOCS, None) if x["rule"] == "ApiErrors")
        self.assertIn(FC.D_CONTRADICTION, n["contradiction"])
        a.config_kind, a.tau_clamped, a.exporter_deadline, a.job_exporter, a.lookback, a.T = "vmagent", {}, {}, {}, 300.0, None
        a.measured_tau, a.dedup = {}, None
        bad = [c for c in FC.env_checks(a) if c[0].startswith("耗時數據和設定矛盾")]
        self.assertTrue(bad and bad[0][1] is False)
        self.assertIn(FC.D_CONTRADICTION, bad[0][2])

    def test_inventory_asks_about_contradiction(self):
        cfg = VMAGENT.replace("scrape_timeout: 10s", "scrape_timeout: 20s")  # vmagent clamps to 15s
        with tempfile.TemporaryDirectory() as t:
            inv, _ = inventory(t, VM_RULES, cfg, {"ruler": "vmalert", "measured": {"pg": {"d_max": "16s", "d_p99": "2s",
                                                                                         "up0_count": 3}}}, kind="vmagent")
        self.assertIn("measured_check.pg", qids(inv))
        self.assertTrue(any(f["kind"] == "measured_contradiction" for f in inv["findings_from_files"]))


    def test_dedup_not_added_to_notify_total(self):
        """lab/vm/dedup-notify: a persistent condition is seen as fast with dedup 30s as without (S 15s)."""
        base = self._args(stack="vmalert", latency=15.0, dedup=None)
        with_d = self._args(stack="vmalert", latency=15.0, dedup=30.0)
        t0 = {n["rule"]: n["total"] for n in FC.notify_checks(base, self.DOCS, None)}
        rows = FC.notify_checks(with_d, self.DOCS, None)
        self.assertEqual(t0, {n["rule"]: n["total"] for n in rows})
        src = {x["name"]: x["source"] for x in rows[0]["inputs"]}
        self.assertIn("不加進總時間", src["dedup"])
        self.assertIn("lab/vm/dedup-notify", src["dedup"])


class AnswerIdsTest(unittest.TestCase):
    def test_unmatched_ids_and_failrun_alias(self):
        a = {"ruler": "vmalert", "measured": {"pg": {"d_max": "2s", "d_p99": "1s", "up0_count": 4}},
             "unknown": ["max_consecutive_failures", "keep_range", "flags"], "deferred": ["failrun.pgg"]}
        with tempfile.TemporaryDirectory() as t:
            inv, out = inventory(t, VM_RULES, VMAGENT, a, kind="vmagent")
            md = INV.markdown(inv)
        self.assertNotIn("failrun.pg", qids(inv))                # bare alias honoured
        self.assertIn("failrun.pg", [d["id"] for d in inv.get("declined") or []])
        um = {x["id"]: x for x in inv.get("ids_unmatched") or []}
        self.assertIn("flags", um)
        self.assertIn("failrun.pgg", um)
        self.assertIn("failrun.pg", um["failrun.pgg"]["near"])
        self.assertNotIn("max_consecutive_failures", um)
        self.assertIn("keep_range.ErrorsBurstA", um["keep_range"]["near"])   # per rule now: not applied to all
        self.assertIn("沒有對到題目的 id", md)

    def test_k_source(self):
        a = type("A", (), {})()
        a.k, a.job_failrun, a.job_failrun_assumed, a.job_fail_counts = 1, {"j": 0}, {}, {}
        self.assertEqual(FC.k_source(a, "j", 1), "measured（最長連續失敗 0 次，取下限 k = 1）")
        a.job_failrun = {"j": 2}
        self.assertEqual(FC.k_source(a, "j", 2), "measured（最長連續失敗 2 次）")
        a.job_failrun, a.job_failrun_assumed = {"j": 3}, {"j": 3}
        self.assertTrue(FC.k_source(a, "j", 3).startswith("assumed"))
        a.job_failrun, a.job_failrun_assumed, a.job_fail_counts = {}, {}, {"j": "up0_count 4"}
        self.assertTrue(FC.k_source(a, "j", 1).startswith("assumed"))
        a.answers_data = {"unknown": ["failrun.j"]}
        self.assertIn("不再問", FC.k_label(a, "j"))
        a.job_fail_counts = {}
        self.assertEqual(FC.k_source(a, "j", 1), "default（k = 1）")


class CardMustAskTest(unittest.TestCase):
    def _card(self, cid):
        return next(c for c in C.load_components() if c["id"] == cid)

    def test_redis_maxmemory_policy(self):
        q = next(x for x in self._card("redis_exporter")["must_ask"] if x["id"] == "maxmemory-policy")
        self.assertIn("noeviction", q["why"])
        self.assertIn("redis_up", q["why"])
        eff = next(e for e in q["effects"] if e["when"] == "noeviction")
        self.assertEqual(eff["verdict"], "no_data")
        self.assertIn("redis_evicted_keys_total", eff["metrics"])

    def test_postgres_patroni_presence(self):
        q = next(x for x in self._card("postgres_exporter")["must_ask"] if x["id"] == "ha-manager")
        self.assertNotIn("applies_when", q)  # asked whether Patroni exists, not only when it is known to
        self.assertIn("REST API", q["why"])
        self.assertIn("etcd", q["why"])


if __name__ == "__main__":
    unittest.main()
