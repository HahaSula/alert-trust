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


if __name__ == "__main__":
    unittest.main()
