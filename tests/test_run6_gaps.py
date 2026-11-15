"""Blind run 6 tool gaps: question ranking (at most one keep_range in the first 3, settled job names are not asked
again), one source for the required window / notify-within in inventory.py and fit_contracts.py (ruler-aware n),
short windows under unknown dedup / L on vmalert, 「未確認，先保留原視窗」 for a keep_range answered 「不知道」, and
the Prometheus version question (first 3 within two rounds; < 2.53 has no query_offset)."""
import json
import os
import re
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import fit_contracts as FC  # noqa: E402
import inventory as INV  # noqa: E402
import inventory_use as IU  # noqa: E402
import review as RV  # noqa: E402

VMAGENT = """global: {scrape_interval: 15s, scrape_timeout: 10s}
scrape_configs:
  - job_name: pg
    static_configs: [{targets: ["pg:9187"]}]
"""
VM_RULES = """groups:
  - name: g
    rules:
      - alert: BurstA
        expr: rate(app_errors_a_total{job="pg"}[1m]) > 5
        labels: {severity: critical}
      - alert: Slow
        expr: rate(app_requests_total{job="pg"}[10m]) > 0
        labels: {severity: warning}
      - alert: M1
        expr: rate(app_x_total{job="pg"}[20s]) > 0
        labels: {severity: warning}
      - alert: M2
        expr: rate(app_y_total{job="pg"}[20s]) > 0
        labels: {severity: warning}
      - alert: M3
        expr: increase(app_z_total{job="pg"}[25s]) > 0
        labels: {severity: warning}
      - alert: M4
        expr: avg_over_time(app_w{job="pg"}[20s]) > 0
        labels: {severity: warning}
"""
PROM = """global: {scrape_interval: 15s, scrape_timeout: 10s, evaluation_interval: 15s}
rule_files: [rules.yml]
alerting: {alertmanagers: [{static_configs: [{targets: ["am:9093"]}]}]}
scrape_configs:
  - job_name: api
    static_configs: [{targets: ["api:8080"]}]
"""
PROM_RULES = """groups:
  - name: g
    rules:
      - alert: Burst
        expr: rate(api_errors_total{job="api"}[30s]) > 1
        labels: {severity: critical}
      - alert: Avg
        expr: avg_over_time(api_latency{job="api"}[20s]) > 1
        labels: {severity: warning}
      - alert: Wide
        expr: rate(api_requests_total{job="api"}[10m]) > 1
        labels: {severity: warning}
"""


def write(d, name, data):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    return p


def inventory(t, rules, config, answers=None, kind=None, name="inv.yaml", merge=None):
    out = os.path.join(t, name)
    argv = ["--rules", write(t, "rules.yml", rules), "--config", write(t, "cfg.yml", config), "--out", out]
    if kind:
        argv += ["--config-kind", kind]
    if answers is not None:
        argv += ["--answers", write(t, "a-" + name, answers)]
    if merge:
        argv += ["--merge", merge]
    RV._quiet(INV.main, argv)
    with open(out, encoding="utf-8") as f:
        return yaml.safe_load(f), out


def fit(t, inv_path, extra=(), out="fit"):
    o = os.path.join(t, out)
    RV._quiet(FC.main, ["--rules", os.path.join(t, "rules.yml"), "--inventory", inv_path, "--promtool-version", "none",
                        *extra, "--out", o])
    with open(os.path.join(o, "contracts-fit.json"), encoding="utf-8") as f:
        js = json.load(f)
    with open(os.path.join(o, "contracts-fit.md"), encoding="utf-8") as f:
        return js, f.read()


def top(inv):
    return [q["id"] for q in inv["questions"] if q["ask_now"]]


def is_keep(qid):
    return qid.startswith("keep_range.")


class RankingTest(unittest.TestCase):
    def test_rank_top_caps_keep_range(self):
        qs = [{"id": f"keep_range.R{i}", "mandatory": i == 0} for i in range(5)] + \
             [{"id": "must_ask.vmalert.data-latency"}, {"id": "must_ask.victoriametrics.dedup-interval"}]
        out = [q["id"] for q in INV.rank_top(qs)]
        self.assertEqual(out[:3], ["keep_range.R0", "must_ask.vmalert.data-latency",
                                   "must_ask.victoriametrics.dedup-interval"])
        self.assertEqual(len(out), len(qs))

    def test_rank_top_fills_with_keep_range_when_nothing_else(self):
        qs = [{"id": f"keep_range.R{i}", "mandatory": i == 0} for i in range(4)] + [{"id": "version"}]
        out = [q["id"] for q in INV.rank_top(qs)]
        self.assertEqual(sorted(out[:3]), ["keep_range.R0", "keep_range.R1", "version"])

    def test_must_ask_not_crowded_out_by_short_windows(self):
        """Four definitely short windows plus a conditional one: one keep_range in the first 3, and the dedup / L
        must_ask questions get in (round 0, then round 1 once the first answers are in)."""
        with tempfile.TemporaryDirectory() as t:
            inv, p = inventory(t, VM_RULES, VMAGENT, {"ruler": "vmalert"}, kind="vmagent")
            self.assertEqual(inv["round"], 0)
            self.assertGreaterEqual(sum(is_keep(q["id"]) for q in inv["questions"]), 5)
            self.assertEqual(sum(is_keep(x) for x in top(inv)), 1)
            self.assertTrue(any(x.startswith("must_ask.") for x in top(inv)), top(inv))
            first = next(x for x in top(inv) if is_keep(x))
            ans = {"ruler": "vmalert", "notify_within": "5m", "unknown": [first]}
            inv2, _ = inventory(t, VM_RULES, VMAGENT, ans, kind="vmagent", name="i2.yaml", merge=p)
            self.assertEqual(inv2["round"], 1)
            self.assertNotIn(first, [q["id"] for q in inv2["questions"]])
            self.assertEqual(sum(is_keep(x) for x in top(inv2)), 1)
            self.assertGreaterEqual(sum(x.startswith("must_ask.") for x in top(inv2)), 1)

    def test_settled_job_names_not_asked_again(self):
        rules = PROM_RULES.replace('rate(api_requests_total{job="api"}[10m])', 'rate(api_requests_total{job="web"}[10m])')
        with tempfile.TemporaryDirectory() as t:
            inv, _ = inventory(t, rules, PROM)
            self.assertIn("job_names", [q["id"] for q in inv["questions"]])
            inv, _ = inventory(t, rules, PROM, {"job_names": ["api"]}, name="i2.yaml")
            self.assertNotIn("job_names", [q["id"] for q in inv["questions"]])
            f = next(x for x in inv["findings_from_files"] if x["kind"] == "job_not_in_config")
            self.assertIn("使用者已回答 job 名稱", f["fact"])
            self.assertIn("或刪掉這條 rule", f["fact"])


class SingleSourceTest(unittest.TestCase):
    def test_no_duplicated_formula_in_inventory(self):
        with open(os.path.join(ROOT, "tools", "inventory.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("FC.required_window(", src)
        self.assertIn("FC.samples_for(", src)
        for own in ("FC.ONE_SAMPLE", "FC.TWO_SAMPLES", "FC.window_need(", "和 fit_contracts.py 同一個公式"):
            self.assertNotIn(own, src)
        self.assertIsNone(re.search(r"\(\s*n\s*\+\s*k\s*\)\s*\*", src))  # (n + k) * I is only in fit_contracts.py
        self.assertIsNone(re.search(r"group_wait\s*\+|\+\s*group_wait", src))  # notify-within is only added up there

    def test_samples_for(self):
        self.assertEqual(FC.samples_for("vmalert", "rate", 15, 1), 1)
        self.assertEqual(FC.samples_for("vmalert", "rate", 150, 1), 2)  # (1+k)·I reaches the query step
        self.assertEqual(FC.samples_for("vmalert", "rate_prometheus", 15, 1), 2)
        self.assertEqual(FC.samples_for("prometheus", "rate", 15, 1), 2)
        self.assertEqual(FC.samples_for("prometheus", "avg_over_time", 15, 1), 1)
        self.assertEqual(FC.samples_for(None, None, 15, 1), 2)
        self.assertEqual(FC.required_window("vmalert", "rate", 30, 10, 0, 1, 45, 30), (1, 75.0))
        self.assertEqual(FC.required_window("prometheus", "rate", 15, 10, 0, 1), (2, 55.0))

    def test_notify_parts(self):
        self.assertEqual(sum(v for _, v in FC.notify_parts("prometheus", 15, 10, 30, 0, None, None, 60)), 115)
        self.assertEqual(sum(v for _, v in FC.notify_parts("prometheus", 15, 10, 30, 20, None, None, 0)), 75)
        vm = FC.notify_parts("vmalert", 15, 10, 30, 0, 45, 30, 50)
        self.assertEqual([k for k, _ in vm], ["S", "d", "E", "max(L, D)", "⌈for/E⌉·E"])
        self.assertEqual(sum(v for _, v in vm), 15 + 10 + 30 + 45 + 60)

    def _same_windows(self, inv, js):
        """Every window length the inventory quotes (ranges[].required_window and the keep_range question text) is
        the one contracts-fit has for that rule."""
        need = {}
        for r in js["rows"]:
            need.setdefault(r["rule"], set()).add((FC.fmt(r["need"]), r["n"]))
        seen = 0
        for r in inv["rules"]:
            for g in r.get("ranges") or []:
                self.assertEqual({(g["required_window"], g["samples_needed"])}, need[r["name"]], r["name"])
                seen += 1
        for q in inv["questions"]:
            if is_keep(q["id"]):
                (w, n), = need[q["rule"]]
                self.assertRegex(q["q"], rf"= {re.escape(w)}（n {n}、")
        return seen

    def test_vmalert_fixture(self):
        with tempfile.TemporaryDirectory() as t:
            for i, a in enumerate(({"ruler": "vmalert"},
                                   {"ruler": "vmalert", "dedup_interval": "30s", "latency": "45s"})):
                inv, p = inventory(t, VM_RULES, VMAGENT, a, kind="vmagent", name=f"i{i}.yaml")
                js, _ = fit(t, p, out=f"fit{i}")
                self.assertEqual(js["stack"], "vmalert")
                self.assertEqual(self._same_windows(inv, js), 6)
                burst = next(r for r in inv["rules"] if r["name"] == "BurstA")["ranges"][0]
                self.assertEqual((burst["required_window"], burst["samples_needed"]), (("30s", "1m15s")[i], 1))  # MetricsQL n = 1

    def test_prometheus_fixture(self):
        with tempfile.TemporaryDirectory() as t:
            inv, p = inventory(t, PROM_RULES, PROM)
            js, _ = fit(t, p)
            self.assertEqual(self._same_windows(inv, js), 3)
            by = {r["name"]: r["ranges"][0] for r in inv["rules"]}
            self.assertEqual((by["Burst"]["required_window"], by["Burst"]["samples_needed"]), ("55s", 2))
            self.assertEqual((by["Avg"]["required_window"], by["Avg"]["samples_needed"]), ("40s", 1))
            self.assertEqual({r["rule"] for r in js["rows"] if r["short"]},
                             {r["name"] for r in inv["rules"] if r["ranges"][0]["short"]})


class UnknownDedupLatencyTest(unittest.TestCase):
    def test_conditional_short_window_asked_in_round_0(self):
        with tempfile.TemporaryDirectory() as t:
            inv, p = inventory(t, VM_RULES, VMAGENT, {"ruler": "vmalert"}, kind="vmagent")
            g = next(r for r in inv["rules"] if r["name"] == "BurstA")["ranges"][0]
            self.assertTrue(g["short"])
            self.assertTrue(g["short_conditional"])
            self.assertEqual((g["required_window"], g["required_window_plausible"]), ("30s", "1m15s"))
            self.assertEqual(g["short_assumption"], "dedup = 2·S = 30s、L = 3·S = 45s")
            q = next(x for x in inv["questions"] if x["id"] == "keep_range.BurstA")
            self.assertIn("假設 dedup = 2·S = 30s、L = 3·S = 45s 時需要 1m15s", q["q"])
            self.assertIn("保守假設", q["q"])
            # the definitely short windows come before the conditional one
            keeps = [x["id"] for x in inv["questions"] if is_keep(x["id"])]
            self.assertEqual(keeps[-1], "keep_range.BurstA")
            # the wide window is not asked about, and the verdict itself does not use the plausible values
            self.assertNotIn("keep_range.Slow", keeps)
            js, _ = fit(t, p)
            self.assertTrue(next(r for r in js["rows"] if r["rule"] == "BurstA")["ok"])
            # dedup / L decide those windows: their must_ask questions count it
            for qid in ("must_ask.victoriametrics.dedup-interval", "must_ask.vmalert.data-latency"):
                m = next(x for x in inv["questions"] if x["id"] == qid)
                self.assertTrue(any("視窗夠不夠長" in c for c in m["changes"]), m["changes"])

    def test_known_values_decide(self):
        with tempfile.TemporaryDirectory() as t:
            a = {"ruler": "vmalert", "dedup_interval": "15s", "latency": "0s"}
            inv, _ = inventory(t, VM_RULES, VMAGENT, a, kind="vmagent")
            g = next(r for r in inv["rules"] if r["name"] == "BurstA")["ranges"][0]
            self.assertFalse(g["short"])
            self.assertNotIn("short_conditional", g)
            a = {"ruler": "vmalert", "dedup_interval": "30s", "latency": "45s"}
            inv, _ = inventory(t, VM_RULES, VMAGENT, a, kind="vmagent", name="i2.yaml")
            g = next(r for r in inv["rules"] if r["name"] == "BurstA")["ranges"][0]
            self.assertTrue(g["short"])
            self.assertNotIn("short_conditional", g)

    def test_prometheus_has_no_conditional(self):
        with tempfile.TemporaryDirectory() as t:
            inv, _ = inventory(t, PROM_RULES, PROM)
        self.assertFalse(any(g.get("short_conditional") for r in inv["rules"] for g in r["ranges"]))
        self.assertIsNone(FC.plausible_window("rate", 15, 10, 1, 30.0, 45.0, 30.0))


class KeepRangeUnknownWordingTest(unittest.TestCase):
    def test_unknown_is_not_intentional(self):
        with tempfile.TemporaryDirectory() as t:
            a = {"keep_range": {"Burst": "不知道", "Avg": True}}
            inv, p = inventory(t, PROM_RULES, PROM, a)
            self.assertEqual(IU.keep_declined(inv), ["Burst"])
            self.assertEqual(IU.keep_range(inv), ["Avg"])
            js, md = fit(t, p)
            self.assertEqual(js["keep_declined"], ["Burst"])
            burst = [line for line in md.splitlines() if line.startswith("| `Burst`")]
            self.assertTrue(any(FC.KEEP_DECLINED_TEXT in line for line in burst), burst)
            self.assertFalse(any("使用者說是刻意的" in line for line in burst))
            avg = [line for line in md.splitlines() if line.startswith("| `Avg`")]
            self.assertTrue(any("使用者說是刻意的" in line for line in avg), avg)
            self.assertFalse(any(FC.KEEP_DECLINED_TEXT in line for line in avg))
            # the window is still left alone
            self.assertTrue(next(r for r in js["rows"] if r["rule"] == "Burst")["keep"])

    def test_unknown_list_and_answers_flag(self):
        with tempfile.TemporaryDirectory() as t:
            inv, p = inventory(t, PROM_RULES, PROM, {"unknown": ["keep_range.Burst"]})
            _, md = fit(t, p)
            self.assertIn(f"—（{FC.KEEP_DECLINED_TEXT}）", md)
            self.assertNotIn("使用者說是刻意的）", md)
            ans = write(t, "only.yaml", {"keep_range": {"Burst": "unknown"}})
            o = os.path.join(t, "f2")
            RV._quiet(FC.main, ["--rules", os.path.join(t, "rules.yml"), "--config", os.path.join(t, "cfg.yml"),
                                "--answers", ans, "--promtool-version", "none", "--out", o])
            with open(os.path.join(o, "contracts-fit.md"), encoding="utf-8") as f:
                md = f.read()
            self.assertIn(f"—（{FC.KEEP_DECLINED_TEXT}）", md)
        self.assertIn("未確認，先保留原視窗", IU.declined_assumption("keep_range.Burst"))


class PrometheusVersionTest(unittest.TestCase):
    def test_version_in_first_three_within_two_rounds(self):
        with tempfile.TemporaryDirectory() as t:
            inv, p = inventory(t, PROM_RULES, PROM)
            self.assertEqual(inv["stack"]["ruler"]["value"], "prometheus")
            self.assertEqual(inv["round"], 0)
            v = next(q for q in inv["questions"] if q["id"] == "version")
            self.assertEqual(v["cost"], 3)
            self.assertIn("2.53", v["q"])
            # round 1: mandatory, whatever was answered meanwhile, with merge or with answers only
            inv1, _ = inventory(t, PROM_RULES, PROM, {"notify_within": "5m"}, name="i1.yaml", merge=p)
            inv2, _ = inventory(t, PROM_RULES, PROM, {"notify_within": "5m"}, name="i2.yaml")
            inv3, _ = inventory(t, PROM_RULES, PROM, None, name="i3.yaml", merge=p)
            for x in (inv1, inv2, inv3):
                self.assertGreaterEqual(x["round"], 1)
                self.assertIn("version", top(x))
                self.assertTrue(next(q for q in x["questions"] if q["id"] == "version")["mandatory"])
            # answered or 「不知道」: not asked again
            inv4, _ = inventory(t, PROM_RULES, PROM, {"version": "2.48.1"}, name="i4.yaml", merge=p)
            self.assertNotIn("version", [q["id"] for q in inv4["questions"]])
            inv5, _ = inventory(t, PROM_RULES, PROM, {"unknown": ["version"]}, name="i5.yaml", merge=p)
            self.assertNotIn("version", [q["id"] for q in inv5["questions"]])

    def test_vmalert_version_is_not_forced(self):
        with tempfile.TemporaryDirectory() as t:
            inv, _ = inventory(t, VM_RULES, VMAGENT, {"ruler": "vmalert", "notify_within": "5m"}, kind="vmagent")
        v = next(q for q in inv["questions"] if q["id"] == "version")
        self.assertFalse(v.get("mandatory"))

    def test_old_version_report_says_no_query_offset(self):
        with tempfile.TemporaryDirectory() as t:
            inv, p = inventory(t, PROM_RULES, PROM, {"version": "2.48.1"})
            js, md = fit(t, p)
            self.assertIn("**版本**：Prometheus 2.48.1 沒有 `query_offset`", md)
            self.assertIn("2.48.1", js["query_offset_unavailable"])
            self.assertNotIn(FC.QO_UNKNOWN, md)
            out = os.path.join(t, "rep")
            RV._quiet(RV.main, ["--rules", os.path.join(t, "rules.yml"), "--config", os.path.join(t, "cfg.yml"),
                                "--answers", write(t, "a.yaml", {"version": "2.48.1"}), "--promtool-version", "none",
                                "--out", out])
            with open(os.path.join(out, "summary.md"), encoding="utf-8") as f:
                self.assertIn("Prometheus 2.48.1 沒有 `query_offset`", f.read())
            inv, p = inventory(t, PROM_RULES, PROM, {"version": "2.54.1"}, name="new.yaml")
            js, md = fit(t, p, out="fit-new")
            self.assertIsNone(js["query_offset_unavailable"])
            self.assertNotIn("沒有 `query_offset`", md)


if __name__ == "__main__":
    unittest.main()
