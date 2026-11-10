import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import inventory as I  # noqa: E402

FX = os.path.join(ROOT, "tests", "fixtures", "inventory")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _main(argv):
    with contextlib.redirect_stdout(io.StringIO()):
        I.main(argv)


def run(rules, config=None, kind=None, am=None, answers=None, merge=None, md=False):
    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, "inv.yaml")
    argv = ["--rules", *[os.path.join(FX, r) for r in rules], "--out", out]
    if config:
        argv += ["--config", os.path.join(FX, config)]
    if kind:
        argv += ["--config-kind", kind]
    if am:
        argv += ["--alertmanager", os.path.join(FX, am)]
    if answers:
        argv += ["--answers", answers]
    if merge:
        argv += ["--merge", merge]
    if md:
        argv += ["--md", os.path.join(tmp, "inv.md")]
    _main(argv)
    inv = yaml.safe_load(_read(out))
    text = _read(os.path.join(tmp, "inv.md")) if md else None
    shutil.rmtree(tmp)
    return inv, text


def rule(inv, name):
    return next(r for r in inv["rules"] if r["name"] == name)


def job(inv, name):
    return next(j for j in inv["jobs"] if j["name"] == name)


def kinds(inv):
    return [f["kind"] for f in inv["findings_from_files"]]


class MysqlScenario(unittest.TestCase):
    """sim-mysql: rules use job="mysql", config has mysqld; Alertmanager is not scraped."""

    @classmethod
    def setUpClass(cls):
        cls.inv, cls.md = run(["mysql/mysql-rules.yml"], "mysql/prometheus.yml", md=True)

    def test_job_mismatch_never_and_always_fires(self):
        self.assertEqual(rule(self.inv, "MySQLExporterDown")["jobs_matched"]["value"], [])
        self.assertEqual(rule(self.inv, "MySQLExporterDown")["fires"]["value"], "never")
        self.assertEqual(rule(self.inv, "MySQLExporterMissing")["fires"]["value"], "always")
        f = next(f for f in self.inv["findings_from_files"] if f["kind"] == "job_not_in_config")
        self.assertEqual(f["closest"], "mysqld")
        self.assertEqual(len(f["rules"]), 18)

    def test_jobs_and_defaults(self):
        j = job(self.inv, "mysqld")
        self.assertEqual(j["interval"], {"value": "30s", "from": "file"})
        self.assertEqual(j["dropped_metrics"], ["mysql_info_schema_.*"])
        self.assertEqual(j["exporter"]["value"], "mysqld_exporter")
        self.assertEqual(j["exporter"]["from"], "hint")
        self.assertEqual(j["exporter_deadline"]["value"], "9.8s")  # τ 10s − 0.25s
        self.assertEqual(j["exporter_deadline"]["from"], "default")

    def test_stack_and_unscraped(self):
        st = self.inv["stack"]
        self.assertEqual(st["ruler"]["value"], "prometheus")
        self.assertEqual(st["version"]["from"], "unknown")
        self.assertEqual(st["alertmanagers"]["value"], ["alertmanager:9093"])
        self.assertEqual(st["rules_given_are_loaded"]["value"], "unknown")  # basename matches, path differs
        self.assertEqual([u["component"] for u in self.inv["unscraped_components"]], ["alertmanager"])

    def test_ranges_and_absence(self):
        r = rule(self.inv, "MySQLSlowQueriesBurst")
        self.assertEqual(r["ranges"][0]["func"], "rate")
        self.assertEqual(r["ranges"][0]["samples_needed"], 2)
        self.assertTrue(r["ranges"][0]["short"])
        self.assertEqual(r["keep_range"]["from"], "unknown")
        self.assertIn("absence_covered_by", rule(self.inv, "MySQLNotReachable")["detects_absence"]["via"])
        self.assertIn("unless", rule(self.inv, "MySQLCollectorsMissing")["detects_absence"]["via"])

    def test_questions(self):
        qs = self.inv["questions"]
        self.assertEqual(sum(q["ask_now"] for q in qs), 3)
        self.assertEqual(qs[0]["answer_key"], "job_names")
        self.assertTrue(any(q["answer_key"] == "--alertmanager" for q in qs))

    def test_markdown(self):
        self.assertIn("這是我從檔案理解的環境，哪裡不對？", self.md)
        self.assertIn("永遠不會響", self.md)
        self.assertIn("**未知**", self.md)

    def test_answers_resolve_job_and_measured(self):
        tmp = tempfile.mkdtemp()
        ans = os.path.join(tmp, "a.yaml")
        with open(ans, "w") as f:
            yaml.safe_dump({"job_names": ["mysql"], "version": "3.5.0", "producers": {"mysqld_exporter": ["mysql"]},
                            "measured": {"mysqld": {"d_p99": "3s", "d_max": "8s", "up0_count": 0}},
                            "keep_range": {"MySQLSlowQueriesBurst": True}}, f)
        inv, _ = run(["mysql/mysql-rules.yml"], "mysql/prometheus.yml", answers=ans)
        shutil.rmtree(tmp)
        self.assertNotIn("fires", rule(inv, "MySQLExporterDown"))
        self.assertEqual(rule(inv, "MySQLExporterDown")["jobs_matched"]["value"], ["mysql"])
        self.assertEqual(inv["stack"]["version"], {"value": "3.5.0", "from": "user"})
        self.assertEqual(job(inv, "mysqld")["measured"]["from"], "user")
        self.assertEqual(rule(inv, "MySQLSlowQueriesBurst")["keep_range"], {"value": True, "from": "user"})


class PgVmagentScenario(unittest.TestCase):
    """sim-pg-vm: vmagent config with scrape_timeout > scrape_interval, job pg vs rules using postgres."""

    @classmethod
    def setUpClass(cls):
        cls.inv, _ = run(["pg-vm/pg-rules.yml", "pg-vm/vm-meta-rules.yml"], "pg-vm/vmagent-scrape.yml", kind="vmagent")

    def test_timeout_clamped(self):
        t = job(self.inv, "pg")["timeout"]
        self.assertEqual((t["value"], t["effective"]), ("20s", "15s"))
        self.assertIn("timeout_gt_interval", kinds(self.inv))

    def test_ruler_and_unscraped(self):
        self.assertEqual(self.inv["stack"]["ruler"]["value"], "vmalert")
        self.assertEqual(self.inv["stack"]["ruler"]["from"], "hint")
        self.assertIn("vmalert", [u["component"] for u in self.inv["unscraped_components"]])
        self.assertNotIn("config_missing", kinds(self.inv))  # vmagent has no rule_files / alerting

    def test_static_label_and_deadline(self):
        self.assertEqual(job(self.inv, "pg")["target_labels"]["from_static"], {"cluster": ["main"]})
        self.assertEqual(job(self.inv, "pg")["exporter_deadline"]["value"], "1m")
        self.assertIn("exporter_deadline_ge_timeout", kinds(self.inv))
        self.assertEqual(rule(self.inv, "PostgresCollectorsMissing")["fires"]["value"], "never")


class ValkeyScenario(unittest.TestCase):
    """sim-valkey: config without rule_files / alerting, rules mixing job redis / valkey / sentinel."""

    def test_config_missing_and_jobs(self):
        inv, _ = run(["valkey/valkey-rules.yml", "valkey/prom-meta.yml"], "valkey/prometheus.yml")
        self.assertEqual(kinds(inv).count("config_missing"), 2)
        bad = {f["job"] for f in inv["findings_from_files"] if f["kind"] == "job_not_in_config"}
        self.assertEqual(bad, {'job="valkey"', 'job="sentinel"'})
        self.assertEqual(job(inv, "redis")["exporter_deadline"]["value"], "15s")
        self.assertEqual(job(inv, "redis")["targets"], 3)
        self.assertEqual(inv["stack"]["rules_given_are_loaded"]["value"], False)


class EtcdScenario(unittest.TestCase):
    """sim-etcd: regex job matcher, subquery range; and the same rules with no config at all."""

    def test_regex_job_and_subquery(self):
        inv, _ = run(["etcd/etcd-rules.yml"], "etcd/prometheus.yml")
        self.assertEqual(rule(inv, "etcdNoLeader")["jobs_matched"]["value"], ["kube-etcd"])
        sq = rule(inv, "etcdHighNumberOfLeaderChanges")["ranges"]
        self.assertEqual((sq[0]["func"], sq[0]["range"], sq[0]["subquery_step"]), ("increase", "15m", "1m"))
        self.assertEqual(rule(inv, "etcdDatabaseQuotaLowSpace")["ranges"][0]["samples_needed"], 1)
        self.assertEqual(job(inv, "kube-etcd")["exporter"]["value"], "etcd")

    def test_no_config(self):
        inv, md = run(["etcd/etcd-rules.yml"], md=True)
        self.assertEqual(inv["jobs"][0]["name"], ".*etcd.*")
        self.assertTrue(inv["jobs"][0]["pattern"])
        self.assertEqual(rule(inv, "etcdNoLeader")["jobs_matched"]["from"], "unknown")
        self.assertEqual(inv["unscraped_components"]["from"], "unknown")
        self.assertEqual(inv["alertmanager"]["from"], "unknown")
        self.assertEqual(kinds(inv), ["no_watchdog_rule"])  # files alone: no Watchdog rule
        self.assertIn("--config", [q["answer_key"] for q in inv["questions"] if q["ask_now"]])


class RoutingScenario(unittest.TestCase):
    """Prometheus config with relabel / honor_labels; alertmanager.yml with nested routes, continue,
    an empty receiver and inhibit rules."""

    @classmethod
    def setUpClass(cls):
        cls.inv, cls.md = run(["routing/rules/shop.yml"], "routing/prometheus.yml", am="routing/alertmanager.yml", md=True)

    def test_relabel_and_honor_labels(self):
        self.assertEqual(job(self.inv, "shop")["target_labels"]["from_relabel"], ["host"])
        self.assertTrue(job(self.inv, "federate")["target_labels"]["honor_labels"])
        self.assertTrue(job(self.inv, "federate")["rewrites_job"])
        # honor_labels can bring other job values: the unmatched job is reported but not as never/always fires
        self.assertNotIn("fires", rule(self.inv, "ClusterMissing"))
        self.assertIn("relabel 會改寫 job", next(f["fact"] for f in self.inv["findings_from_files"] if f["kind"] == "job_not_in_config"))

    def test_timeout_gt_interval_prometheus(self):
        self.assertIn("載入設定會失敗", job(self.inv, "mysqld")["timeout"]["note"])
        self.assertTrue(rule(self.inv, "TableGrowth")["ranges"])

    def test_static_label_value_mismatch(self):
        r = rule(self.inv, "ShopStaging")
        self.assertEqual(r["labels_required"][0]["status"], "missing")
        self.assertEqual(r["fires"]["value"], "never")

    def test_continue_and_inheritance(self):
        rt = rule(self.inv, "ShopDown")["route"]
        self.assertEqual(rt["receivers"], ["pager", "shop"])
        self.assertNotIn("depends_on_runtime_labels", rt)  # env / instance known from static_configs
        shop = next(m for m in rt["matches"] if m["receiver"] == "shop")
        self.assertEqual((shop["repeat_interval"], shop["group_wait"]), ("1h", "30s"))
        pager = next(m for m in rt["matches"] if m["receiver"] == "pager")
        self.assertEqual(pager["group_wait"], "10s")
        nested = next(r for r in self.inv["alertmanager"]["routes"] if r["path"] == "route.routes[1].routes[0]")
        self.assertEqual(nested["repeat_interval"], {"value": "1h", "from": "file", "inherited": True})

    def test_runtime_labels(self):
        rt = rule(self.inv, "EdgeDown")["route"]
        self.assertIsNone(rt["receiver"])
        self.assertIn("instance", rt["depends_on_runtime_labels"])

    def test_empty_receiver(self):
        self.assertEqual({r["name"] for r in self.inv["alertmanager"]["receivers"] if r["empty"]}, {"blackhole"})
        sure = {x["rule"] for x in self.inv["alertmanager"]["unrouted_or_blackholed_rules"] if x["certain"]}
        self.assertEqual(sure, {"ShopStaging", "NoiseCheck"})
        self.assertEqual(rule(self.inv, "NoiseCheck")["route"]["receiver"], "blackhole")

    def test_inhibit(self):
        ih = self.inv["alertmanager"]["inhibit"][0]
        self.assertEqual(ih["source_rules"], ["ShopDown"])
        self.assertEqual({p["target"] for p in ih["suppressed"]}, {"ShopSlow", "ShopStaging"})
        self.assertIn("always_inhibited", kinds(self.inv))

    def test_unscraped_alertmanagers(self):
        self.assertEqual([u["target"] for u in self.inv["unscraped_components"]], ["am-1:9093", "am-2:9093"])
        self.assertTrue(self.inv["stack"]["rules_given_are_loaded"]["value"])  # rule_files relative to the config

    def test_matchers_parsing(self):
        self.assertEqual(I.am_matchers({"matchers": ['{a="b", c=~"d|e"}', "f!=g"]}),
                         [("a", "=", "b"), ("c", "=~", "d|e"), ("f", "!=", "g")])


class RouteLabelScenario(unittest.TestCase):
    """route-labels: routes need service / cluster that the rules and targets do not have; a Watchdog route
    without a Watchdog rule; an inhibit rule without equal."""

    @classmethod
    def setUpClass(cls):
        d = "route-labels/"
        cls.inv, cls.md = run([d + "rules.yml"], d + "prometheus.yml", am=d + "alertmanager.yml", md=True)
        cls.f = cls.inv["findings_from_files"]

    def gap(self, label, certain):
        return next(f for f in self.f if f["kind"] == "route_label_missing" and f["label"] == label and f["certain"] is certain)

    def test_dropped_label_is_certain(self):
        g = self.gap("service", True)
        self.assertEqual(g["rules"], ["AppDown", "AppErrorsHigh"])  # up only carries target labels; sum by (instance)
        self.assertEqual(g["fallback"], ["mail"])
        self.assertTrue(g["fact"].startswith("route 要求 `service`，這些 rule 都沒有，實際會送到 mail"))
        rt = rule(self.inv, "AppErrorsHigh")["route"]
        self.assertEqual(rt["receiver"], "mail")
        self.assertNotIn("depends_on_runtime_labels", rt)

    def test_series_label_stays_runtime_and_asks(self):
        g = self.gap("service", False)
        self.assertEqual(g["rules"], ["AppQueueDeep"])
        self.assertIn("service", rule(self.inv, "AppQueueDeep")["route"]["depends_on_runtime_labels"])
        q = next(q for q in self.inv["questions"] if q["answer_key"] == "labels.service")
        self.assertIn("exporter 會輸出 `service` 嗎", q["q"])
        self.assertEqual(q["affects"], 1)

    def test_labels_set_elsewhere_are_not_gaps(self):
        self.assertEqual(rule(self.inv, "AppWithService")["route"]["receiver"], "pager")
        self.assertNotIn("AppLabelReplaced", self.gap("service", True)["rules"])  # label_replace writes it
        # cluster="other": the series carry cluster=main, so that route is not for these rules
        self.assertFalse([f for f in self.f if f["kind"] == "route_label_missing" and f["label"] == "cluster"])
        self.assertFalse([f for f in self.f if f["kind"] == "route_label_value_mismatch"])

    def test_critical_not_paging(self):
        f = next(f for f in self.f if f["kind"] == "critical_not_paging")
        self.assertEqual((f["rules"], f["receivers"]), (["AppDown", "AppErrorsHigh"], ["mail"]))

    def test_watchdog_route_without_rule(self):
        f = next(f for f in self.f if f["kind"] == "dms_route_without_rule")
        self.assertEqual(f["receiver"], "dms")
        self.assertIn("no_watchdog_rule", kinds(self.inv))

    def test_inhibit_without_equal(self):
        f = next(f for f in self.f if f["kind"] == "inhibit_no_equal")
        self.assertEqual((f["source_rules"], f["target_rules"]), (["AppDown"], ["AppSlow"]))

    def test_answer_exporter_label(self):
        tmp = tempfile.mkdtemp()
        ans = os.path.join(tmp, "a.yaml")
        with open(ans, "w") as fh:
            yaml.safe_dump({"labels": {"service": "exporter"}}, fh)
        d = "route-labels/"
        inv, _ = run([d + "rules.yml"], d + "prometheus.yml", am=d + "alertmanager.yml", answers=ans)
        shutil.rmtree(tmp)
        gaps = [f for f in inv["findings_from_files"] if f["kind"] == "route_label_missing"]
        self.assertTrue(all(f["certain"] for f in gaps))  # the exporter answer removes the conditional one only
        self.assertIn("service", rule(inv, "AppQueueDeep")["route"]["depends_on_runtime_labels"])

    def test_value_mismatch_close_names(self):
        # route job="etcd", the series' job is kube-etcd (names close: looks like a typo) -> finding
        tmp = tempfile.mkdtemp()
        files = {"p.yml": {"scrape_configs": [{"job_name": "kube-etcd", "static_configs": [{"targets": ["e1:2379"]}]}]},
                 "r.yml": {"groups": [{"name": "g", "rules": [
                     {"alert": "EtcdNoLeader", "expr": 'etcd_server_has_leader{job=~".*etcd.*"} == 0',
                      "labels": {"severity": "critical"}}]}]},
                 "am.yml": {"route": {"receiver": "mail", "routes": [
                     {"matchers": ['severity="critical"', 'job="etcd"'], "receiver": "pager"}]},
                     "receivers": [{"name": "mail", "email_configs": [{"to": "x"}]}, {"name": "pager", "webhook_configs": [{"url": "y"}]}]}}
        for n, d in files.items():
            with open(os.path.join(tmp, n), "w") as fh:
                yaml.safe_dump(d, fh)
        out = os.path.join(tmp, "inv.yaml")
        _main(["--rules", os.path.join(tmp, "r.yml"), "--config", os.path.join(tmp, "p.yml"),
               "--alertmanager", os.path.join(tmp, "am.yml"), "--out", out])
        inv = yaml.safe_load(_read(out))
        shutil.rmtree(tmp)
        f = next(f for f in inv["findings_from_files"] if f["kind"] == "route_label_value_mismatch")
        self.assertEqual((f["label"], f["values"], f["fallback"]), ("job", ["kube-etcd"], ["mail"]))
        self.assertIn("critical_not_paging", kinds(inv))


class ResultLabelTest(unittest.TestCase):
    def test_result_label(self):
        R = I.result_label
        self.assertEqual(R('sum by (instance) (rate(x{job="a"}[5m])) > 1', "service")[0], "absent")
        self.assertEqual(R('sum by (instance, service) (rate(x[5m])) > 1', "service")[0], "series")
        self.assertEqual(R('sum(up{job=~".*etcd.*"} == bool 1) without (instance) < 2', "job")[0], "series")
        self.assertEqual(R('sum(x) without (service)', "service")[0], "absent")
        self.assertEqual(R('count(x)', "service")[0], "absent")
        self.assertEqual(R('topk(3, x)', "service")[0], "series")
        self.assertEqual(R('absent(up{job="a", service="db"})', "service"), ("value", "db"))
        self.assertEqual(R('absent(up{job="a"})', "service")[0], "absent")
        self.assertEqual(R('label_replace(up == 0, "service", "x", "", "")', "service")[0], "made")
        self.assertEqual(R('histogram_quantile(0.99, sum by (le) (rate(h_bucket[5m])))', "service")[0], "absent")
        self.assertEqual(R('1 - avg by (service) (x)', "service")[0], "series")
        self.assertEqual(R('vector(1)', "service")[0], "absent")
        self.assertEqual(R('x > 1 or sum(y) > 2', "service")[0], "series")
        self.assertEqual(R('a * on (instance) group_left (service) b', "service")[0], "made")


class MergeTest(unittest.TestCase):
    def test_keeps_user_values(self):
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, "inv.yaml")
        args = ["--rules", os.path.join(FX, "mysql/mysql-rules.yml"), "--config", os.path.join(FX, "mysql/prometheus.yml")]
        _main(args + ["--out", out])
        inv = yaml.safe_load(_read(out))
        inv["stack"]["version"] = {"value": "2.53.0", "from": "user", "confirmed": True}
        j = next(x for x in inv["jobs"] if x["name"] == "mysqld")
        j["exporter_deadline"]["value"] = "8s"  # edited without marking: kept because the sources did not change
        with open(out, "w", encoding="utf-8") as f:
            yaml.safe_dump(inv, f, allow_unicode=True, sort_keys=False)
        _main(args + ["--merge", out, "--out", out])
        new = yaml.safe_load(_read(out))
        shutil.rmtree(tmp)
        self.assertEqual(new["stack"]["version"]["value"], "2.53.0")
        nj = next(x for x in new["jobs"] if x["name"] == "mysqld")
        self.assertEqual(nj["exporter_deadline"], {"value": "8s", "from": "user", "basis": nj["exporter_deadline"]["basis"],
                                                   "ref": "docs/tools/baseline.md#defaults", "confirmed": True})
        self.assertEqual(nj["interval"]["value"], "30s")  # refreshed

    def test_refreshes_when_sources_change(self):
        new = {"a": {"value": 1, "from": "file"}, "b": {"value": 2, "from": "file"}}
        old = {"a": {"value": 9, "from": "file"}, "b": {"value": 7, "from": "user"}}
        self.assertEqual(I.merge(new, old, same_sources=False), {"a": {"value": 1, "from": "file"}, "b": {"value": 7, "from": "user"}})


if __name__ == "__main__":
    unittest.main()
