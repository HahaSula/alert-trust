"""--inventory in check_metric_sources / fit_contracts / check_coverage, the card fields key_metrics / metrics_any,
and the one-shot tools/review.py."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import catalog as C  # noqa: E402
import check_coverage as CC  # noqa: E402
import check_metric_sources as MS  # noqa: E402
import fit_contracts as FC  # noqa: E402
import inventory as I  # noqa: E402
import inventory_use as IU  # noqa: E402
import review as RV  # noqa: E402

FX = os.path.join(ROOT, "tests", "fixtures")
INVFX = os.path.join(FX, "inventory")


def quiet(fn, argv):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(argv)


def build_inv(tmp, rules, config=None, kind=None, am=None):
    out = os.path.join(tmp, "inv.yaml")
    argv = ["--rules", *[os.path.join(INVFX, r) for r in rules], "--out", out]
    if config:
        argv += ["--config", os.path.join(INVFX, config)]
    if kind:
        argv += ["--config-kind", kind]
    if am:
        argv += ["--alertmanager", os.path.join(INVFX, am)]
    quiet(I.main, argv)
    return out


def fit(tmp, argv):
    out = os.path.join(tmp, "fit")
    rc = quiet(FC.main, argv + ["--out", out])
    with open(os.path.join(out, "contracts-fit.json"), encoding="utf-8") as f:
        return rc, json.load(f)


class MetricSourcesInventoryTest(unittest.TestCase):
    def test_same_statuses_as_config(self):
        tmp = tempfile.mkdtemp()
        st = lambda rows: {(r["rule"], r["metric"]): r["status"] for r in rows}  # noqa: E731
        seen = set()
        for rf, cf in (("mysql/mysql-rules.yml", "mysql/prometheus.yml"),
                       ("../metric-sources/rules.yml", "../metric-sources/prometheus.yml"),
                       ("routing/rules/shop.yml", "routing/prometheus.yml")):
            inv = IU.load(build_inv(tmp, [rf], cf))
            rules = MS.load_rules([os.path.join(INVFX, rf)])
            by_cfg = MS.check(rules, MS.load_config(os.path.join(INVFX, cf)), {}, None, ROOT)
            by_inv = MS.check(rules, IU.ms_jobs(inv), IU.answers(inv), None, ROOT)
            self.assertEqual(st(by_inv), st(by_cfg), rf)
            seen |= set(st(by_inv).values())
        shutil.rmtree(tmp)
        self.assertIn("dropped", seen)  # dropped_metrics carried over from the inventory
        self.assertIn("needs_input", seen)  # relabel rewriting job keeps the job set open

    def test_user_exporter_becomes_producer(self):
        tmp = tempfile.mkdtemp()
        path = build_inv(tmp, ["mysql/mysql-rules.yml"], "mysql/prometheus.yml")
        inv = IU.load(path)
        j = next(x for x in inv["jobs"] if x["name"] == "mysqld")
        j["exporter"] = {"value": "mysqld_exporter", "from": "user", "confirmed": True}
        self.assertEqual(IU.answers(inv)["producers"], {"mysqld_exporter": ["mysqld"]})
        self.assertEqual(IU.answers(inv, {"producers": {"x": ["y"]}})["producers"], {"x": ["y"]})  # --answers wins
        out = os.path.join(tmp, "ms")
        rc = quiet(MS.main, ["--inventory", path, "--out", out])  # rules taken from the inventory
        self.assertEqual(rc, 1)
        self.assertTrue(os.path.exists(os.path.join(out, "metric-sources.json")))
        shutil.rmtree(tmp)


class FitInventoryTest(unittest.TestCase):
    def test_no_interval_flags_uses_defaults(self):
        tmp = tempfile.mkdtemp()
        rc, data = fit(tmp, ["--rules", os.path.join(INVFX, "mysql/mysql-rules.yml")])
        shutil.rmtree(tmp)
        self.assertIn(rc, (0, 1))
        self.assertEqual((data["stack"], data["S"], data["E"]), ("prometheus", 60.0, 60.0))
        text = "；".join(data["assumptions"])
        self.assertIn("`scrape_interval` 1m、`scrape_timeout` 10s", text)
        self.assertIn("`evaluation_interval`：用 Prometheus 預設 1m", text)
        self.assertIn("沒有 --stack", text)

    def test_inventory_gives_stack_and_effective_timeout(self):
        tmp = tempfile.mkdtemp()
        inv = build_inv(tmp, ["pg-vm/pg-rules.yml", "pg-vm/vm-meta-rules.yml"], "pg-vm/vmagent-scrape.yml", kind="vmagent")
        _, data = fit(tmp, ["--inventory", inv])
        shutil.rmtree(tmp)
        self.assertEqual(data["stack"], "vmalert")  # stack.ruler (hint) from the inventory
        texts = " ".join(c["text"] for c in data["env_checks"])
        self.assertIn("20s > S 15s（job pg）", texts)  # vmagent clamp recorded
        self.assertTrue(any(r.get("nodata") for r in data["rows"] if r["rule"] == "PostgresCollectorsMissing") or
                        not [r for r in data["rows"] if r["rule"] == "PostgresCollectorsMissing"])

    def test_precedence(self):
        tmp = tempfile.mkdtemp()
        path = build_inv(tmp, ["mysql/mysql-rules.yml"], "mysql/prometheus.yml")
        inv = C._load(path)
        j = next(x for x in inv["jobs"] if x["name"] == "mysqld")
        j["interval"] = {"value": "1m", "from": "user", "confirmed": True}
        inv["rules"][0]["keep_range"] = {"value": True, "from": "user"}
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(inv, f, allow_unicode=True)
        cfg = os.path.join(INVFX, "mysql/prometheus.yml")
        _, data = fit(tmp, ["--inventory", path, "--config", cfg])
        self.assertIn("mysqld S 1m", " ".join(data["assumptions"]))  # from: user beats --config (30s)
        _, data = fit(tmp, ["--inventory", path, "--job-interval", "mysqld=15s", "--eval-interval", "2m"])
        self.assertIn("mysqld S 15s", " ".join(data["assumptions"]))  # flags beat the inventory
        self.assertEqual(data["E"], 120.0)
        shutil.rmtree(tmp)

    def test_never_firing_rules_have_no_data(self):
        tmp = tempfile.mkdtemp()
        _, data = fit(tmp, ["--inventory", build_inv(tmp, ["mysql/mysql-rules.yml"], "mysql/prometheus.yml")])
        shutil.rmtree(tmp)
        burst = [r for r in data["rows"] if r["rule"] == "MySQLSlowQueriesBurst"]
        self.assertTrue(burst and all(r["nodata"] for r in burst))


class CardFieldsTest(unittest.TestCase):
    def test_key_metrics_and_alternatives(self):
        sig = {"metrics": ["up", "probe_success", "x_leader"], "key_metrics": ["probe_success"]}
        self.assertEqual(CC.missing_metrics(sig, [{"query": "x_leader == 0"}]), ["probe_success"])
        self.assertEqual(CC.missing_metrics(sig, [{"query": "x_leader == 0"}, {"query": "probe_success == 0"}]), [])
        alt = {"metrics": ["a", "b", "c"], "metrics_any": [["a", "b"]]}
        self.assertEqual(CC.missing_metrics(alt, [{"query": "b > 1"}]), [])
        self.assertEqual(CC.missing_metrics(alt, [{"query": "c > 1"}]), ["a 或 b"])
        self.assertEqual(CC.missing_metrics({"metrics": ["a", "b"]}, [{"query": "a > 1"}]), ["b"])  # legacy: all

    def test_validate_fields(self):
        ok = {"metrics": ["a", "b", "c"], "key_metrics": ["a"], "metrics_any": [["b", "c"]]}
        self.assertEqual(C.check_metric_fields(ok), [])
        self.assertTrue(C.check_metric_fields({"metrics": ["a"], "key_metrics": ["z"]}))
        self.assertTrue(C.check_metric_fields({"metrics": ["a", "b"], "metrics_any": [["a"]]}))
        self.assertTrue(C.check_metric_fields({"metrics": ["a", "b"], "key_metrics": ["a"], "metrics_any": [["a", "b"]]}))

    def test_native_cards_mark_probe_success(self):
        cards = {c["id"]: c for c in C.load_components()}
        for cid in ("etcd", "tidb", "yugabytedb", "clickhouse"):
            s = next(x for x in cards[cid]["signals"] if x["id"] == "health-second-path")
            self.assertEqual(s["key_metrics"], ["probe_success"], cid)

    def test_probe_rule_counts_for_component(self):
        env = {"components": [{"id": "prom", "type": "prometheus", "job": "prometheus"}], "observers": [],
               "targets": [{"id": "etcd-1", "type": "etcd", "scraped_by": "prom", "job": "kube-etcd"}]}
        rules = {"prom": [{"type": "alerting", "name": "etcdNoLeader", "query": 'etcd_server_has_leader{job="kube-etcd"} == 0'},
                          {"type": "alerting", "name": "EtcdReadyz", "query": 'probe_success{job="etcd-readyz"} == 0'}]}
        items = CC.evaluate(env, None, {1}, False, None, rules_override=rules)
        h = next(i for i in items if i["component"] == "etcd-1" and i["signal"] == "health-second-path")
        self.assertEqual(h["status"], "covered")
        rules["prom"].pop()
        items = CC.evaluate(env, None, {1}, False, None, rules_override=rules)
        h = next(i for i in items if i["component"] == "etcd-1" and i["signal"] == "health-second-path")
        self.assertEqual(h["status"], "partial")
        self.assertIn("key_metrics", h["note"])


class CoverageInventoryTest(unittest.TestCase):
    def test_without_snapshot(self):
        tmp = tempfile.mkdtemp()
        cr = os.path.join(FX, "coverage-review")
        inv = os.path.join(tmp, "inv.yaml")
        quiet(I.main, ["--rules", os.path.join(cr, "prom-rules.yml"), "--config", os.path.join(cr, "prometheus.yml"), "--out", inv])
        out = os.path.join(tmp, "cov")
        quiet(CC.main, ["--env", os.path.join(cr, "env.yaml"), "--inventory", inv, "--out", out, "--tiers", "1,2"])
        with open(os.path.join(out, "coverage.json"), encoding="utf-8") as f:
            items = {(i["component"], i["signal"]): i for i in json.load(f)["items"]}
        shutil.rmtree(tmp)
        self.assertEqual(items[("etcd-1", "has-leader")]["status"], "covered")  # rule files from the inventory
        self.assertEqual(items[("etcd-1", "health-second-path")]["status"], "partial")


class ReviewTest(unittest.TestCase):
    def test_summary(self):
        tmp = tempfile.mkdtemp()
        d = os.path.join(INVFX, "route-labels")
        quiet(RV.main, ["--rules", os.path.join(d, "rules.yml"), "--config", os.path.join(d, "prometheus.yml"),
                        "--alertmanager", os.path.join(d, "alertmanager.yml"), "--out", tmp])
        with open(os.path.join(tmp, "summary.md"), encoding="utf-8") as f:
            md = f.read()
        for name in ("inventory.yaml", "metric-sources.json", "lint.md", "contracts-fit.json"):
            self.assertTrue(os.path.exists(os.path.join(tmp, name)), name)
        shutil.rmtree(tmp)
        top = md.split("## 建議先處理的 3 件事")[1].split("## ")[0]
        self.assertLess(top.index("監控體系本身"), top.index("critical alert 不會 page"))  # stack before paging
        self.assertIn("**建議**", top)
        self.assertIn("**代價**", top)
        self.assertIn("## 這是我從檔案理解的環境，哪裡不對？", md)
        self.assertIn("## 假設", md)
        qs = md.split("## 接下來想問（最多 3 題）")[1].split("## ")[0]
        self.assertLessEqual(sum(1 for l in qs.splitlines() if l[:2] in ("1.", "2.", "3.", "4.")), 3)

    def test_rank_order(self):
        inv = {"rules": [{"name": "A", "kind": "alert", "fires": {"value": "never", "from": "file"}, "labels_out": {}}],
               "findings_from_files": [{"kind": "no_watchdog_rule", "fact": "x"}], "unscraped_components": []}
        items = RV.rank(inv, [], {"env_checks": [{"text": "t", "ok": False, "contract": "c"}], "rows": [], "for_checks": []}, None)
        self.assertEqual([i["key"] for i in items], ["firing", "stack", "contracts"])


if __name__ == "__main__":
    unittest.main()
