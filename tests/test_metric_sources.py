import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import check_metric_sources as M  # noqa: E402

FX = os.path.join(ROOT, "tests", "fixtures", "metric-sources")


class MetricSourcesTest(unittest.TestCase):
    def test_selectors(self):
        got = M.selectors('histogram_quantile(0.99, sum by (le, job) (rate(x_bucket{job=~"a|b", le!=""}[5m] offset 1m)))'
                          ' / on(instance) group_left(version) build_info and up{job="a"} == 1')
        self.assertEqual([s["metric"] for s in got], ["x_bucket", "build_info", "up"])
        self.assertEqual(got[0]["matchers"][0], ("job", "=~", "a|b"))
        self.assertEqual(M.selectors('{__name__="foo", job="j"}')[0]["metric"], "foo")
        self.assertEqual(M.selectors('label_replace(m, "dst", "$1", "src", "(.*)")')[0]["metric"], "m")
        self.assertEqual(M.selectors("count(up) > 2")[0]["metric"], "up")

    def _run(self, answers):
        rules = M.load_rules([os.path.join(FX, "rules.yml")])
        jobs = M.load_config(os.path.join(FX, "prometheus.yml"))
        return {(r["rule"], r["metric"]): r["status"] for r in M.check(rules, jobs, answers, None, ROOT)}

    def test_never_assumes_without_answers(self):
        st = self._run({})
        self.assertEqual(st[("TableSize", "mysql_info_schema_table_size")], "dropped")
        self.assertEqual(st[("MySQLDown", "job:mysql_up:min")], "recording")
        # job label can be rewritten by relabel_configs: not a mismatch yet, a question
        self.assertEqual(st[("MySQLDownWrongJob", "mysql_up")], "needs_input")
        # the catalog knows mysql_up comes from mysqld_exporter, but that is only a hint
        self.assertEqual(st[("job:mysql_up:min", "mysql_up")], "needs_input")
        self.assertEqual(st[("OrderSlow", "order_latency_seconds_bucket")], "needs_input")

    def test_answers_resolve(self):
        st = self._run({"job_names": ["order-api"], "producers": {"mysqld_exporter": ["mysql"]},
                        "metrics": {"order_latency_seconds_bucket": {"jobs": ["order-api"]}}})
        self.assertEqual(st[("MySQLDownWrongJob", "mysql_up")], "job_mismatch")
        self.assertEqual(st[("PrometheusDown", "up")], "job_mismatch")
        self.assertEqual(st[("job:mysql_up:min", "mysql_up")], "confirmed")
        self.assertEqual(st[("OrderSlow", "order_latency_seconds_bucket")], "confirmed")

    def test_live_snapshot(self):
        rules = M.load_rules([os.path.join(FX, "rules.yml")])
        jobs = M.load_config(os.path.join(FX, "prometheus.yml"))
        live = {"mysql_up": ["mysql"], "order_latency_seconds_bucket": ["app"], "mysql_global_status_threads_connected": []}
        st = {(r["rule"], r["metric"]): r["status"] for r in M.check(rules, jobs, {}, live, ROOT)}
        self.assertEqual(st[("job:mysql_up:min", "mysql_up")], "live")
        self.assertEqual(st[("MySQLDownWrongJob", "mysql_up")], "live_missing")
        self.assertEqual(st[("Ratio", "mysql_global_status_threads_connected")], "live_missing")
        # not queried by collect.py (e.g. only used in a recording rule): ask, do not call it missing
        self.assertEqual(st[("Ratio", "mysql_global_variables_max_connections")], "needs_input")


FX2 = os.path.join(ROOT, "tests", "fixtures", "metric-sources-v2")


class MetricSourcesV2Test(unittest.TestCase):
    def _rows(self, answers=None):
        rules = M.load_rules([os.path.join(FX2, "rules.yml")])
        jobs = M.load_config(os.path.join(FX2, "prometheus.yml"))
        return {r["rule"]: r for r in M.check(rules, jobs, answers or {}, None, ROOT)}

    def test_selector_positions_and_ranges(self):
        e = 'absent(up{job="a"}) or rate(x{a="b"}[5m] offset 1m) > 0'
        s = M.selectors(e)
        self.assertEqual([x["range"] for x in s], [None, "5m"])
        self.assertEqual(s[1]["offset"], "1m")
        self.assertEqual(M.enclosing_funcs(e, s[0]["pos"]), ["absent"])
        self.assertEqual(e[s[1]["pos"]:s[1]["range_end"]], 'x{a="b"}[5m]')

    def test_absent_with_no_job_always_fires(self):
        r = self._rows()
        self.assertEqual(r["ExporterMissing"]["status"], "always_fires")
        self.assertEqual(r["ExporterMissing"]["cause"], "job_mismatch")
        self.assertEqual(r["ExporterMissingOk"]["status"], "builtin")

    def test_name_regex_with_unknown_job_is_job_mismatch(self):
        r = self._rows()
        self.assertEqual(r["ReplicationNotRunning"]["status"], "job_mismatch")
        self.assertEqual(r["ReplicationNotRunning"]["suggest"], "mysqld")
        self.assertEqual(r["ReplicationNameRegex"]["status"], "needs_input")
        self.assertEqual(r["ReplicationNameRegex"]["ask"], "producer")         # merged into the exporter question
        self.assertEqual(r["ReplicationNameRegex"]["hint"], ["mysqld_exporter"])
        r = self._rows({"producers": {"mysqld_exporter": ["mysqld"]}})
        self.assertEqual(r["ReplicationNameRegex"]["status"], "confirmed")

    def test_required_labels(self):
        r = self._rows()
        self.assertEqual(r["PrimaryReadOnly"]["status"], "needs_input")       # exporter label? ask
        self.assertEqual(r["PrimaryReadOnly"]["labels_missing"], ["role"])
        self.assertEqual(r["ClusterReadOnly"]["labels_missing"], [])           # static_configs adds cluster
        self.assertEqual(r["UpRole"]["status"], "label_mismatch")             # up carries target labels only
        self.assertEqual(r["UpRoleFiles"]["status"], "needs_input")           # file_sd: labels unknowable, ask
        self.assertEqual(r["UpRoleFiles"]["labels_missing"], ["role"])
        self.assertEqual(r["OptionalLabel"]["labels_missing"], [])             # =~ matching "" does not require it

    def test_label_answers(self):
        r = self._rows({"labels": {"role": "none"}})
        self.assertEqual(r["PrimaryReadOnly"]["status"], "label_mismatch")
        r = self._rows({"labels": {"role": "exporter"}, "producers": {"mysqld_exporter": ["mysqld"]}})
        self.assertEqual(r["PrimaryReadOnly"]["status"], "confirmed")

    def test_report_is_advice_first(self):
        rules = M.load_rules([os.path.join(FX2, "rules.yml")])
        jobs = M.load_config(os.path.join(FX2, "prometheus.yml"))
        md = M.report(M.check(rules, jobs, {}, None, ROOT), jobs)
        self.assertLess(md.index("## 建議先做"), md.index("| 狀態 | 數量 |"))
        self.assertIn("一直在響", md)
        self.assertIn("建議", md)
        self.assertIn("代價", md)
        self.assertIn("要 `role` label", md)
        self.assertIn("(slave\\|replica)", md)                               # | escaped inside table cells
        self.assertNotIn("不合格", md)


class ExporterLabelsTest(unittest.TestCase):
    """Labels the exporter produces (le, quantile, grpc_*, labels the catalog shows) are not questions."""

    def test_only_identity_or_unknown_labels_are_asked(self):
        fx = os.path.join(ROOT, "tests", "fixtures", "metric-sources-labels")
        rows = M.check(M.load_rules([os.path.join(fx, "rules.yml")]), M.load_config(os.path.join(fx, "prometheus.yml")),
                       {}, None, ROOT)
        miss = {}
        for r in rows:
            miss.setdefault(r["rule"], set()).update(r["labels_missing"])
        self.assertEqual(miss["GrpcErrors"], set())
        self.assertEqual(miss["FsyncSlow"], set())
        self.assertEqual(miss["Summary"], set())          # quantile, and slice on a metric the catalog knows
        self.assertEqual(miss["RedisCmd"], set())         # cmd: non-identity label on a known exporter metric
        self.assertEqual(miss["RedisRole"], {"role"})     # identity label: still a question
        self.assertEqual(miss["AppTier"], {"tier"})       # unknown metric: still a question
        self.assertTrue(any("grpc_type" in l for r in rows for l in r["labels_exporter"]))
        md = M.report(rows, [])
        self.assertNotIn("要 `grpc_type` label", md)
        self.assertNotIn("要 `quantile` label", md)

    def test_non_ascii_text_does_not_break_the_parser(self):
        self.assertEqual([s["metric"] for s in M.selectors("中文 up{job=\"a\"} 說明")], ["up"])


if __name__ == "__main__":
    unittest.main()
