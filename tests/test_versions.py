"""Signal `versions` field, rule annotation alert_trust_min_version, and how check_coverage.py uses env versions."""
import os
import shutil
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import catalog as C  # noqa: E402
import check_coverage as CC  # noqa: E402


class CompareTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(C.parse_version("8.4.13"), (8, 4, 13))
        self.assertEqual(C.parse_version("v3.6.16"), (3, 6, 16))
        self.assertEqual(C.parse_version("2024.2.3.0"), (2024, 2, 3, 0))
        self.assertEqual(C.parse_version("26.8.3.1"), (26, 8, 3, 1))
        self.assertEqual(C.parse_version("11.4.13-MariaDB"), (11, 4, 13))
        self.assertEqual(C.parse_version("11.4.13-MariaDB-ubu2404-log"), (11, 4, 13))
        for v in (None, "", "unknown", "不知道", True):
            self.assertIsNone(C.parse_version(v))

    def test_compare(self):
        self.assertEqual(C.compare_versions("v3.6.16", "3.6"), 1)
        self.assertEqual(C.compare_versions("3.6", "3.6.0"), 0)
        self.assertEqual(C.compare_versions("3.5.21", "3.6"), -1)
        self.assertEqual(C.compare_versions("2.20.12", "2024.2"), -1)        # YugabyteDB old scheme
        self.assertEqual(C.compare_versions("2024.2.3.0", "2024.2"), 1)
        self.assertEqual(C.compare_versions("26.8.3.1", "26.3"), 1)
        self.assertEqual(C.compare_versions("25.8.33.6", "26.3"), -1)
        self.assertEqual(C.compare_versions("10.11.19-MariaDB", "11.4"), -1)
        self.assertEqual(C.compare_versions("11.4.13-MariaDB", "11.4"), 1)
        self.assertEqual(C.compare_versions("8.4.13", "8.10"), -1)          # numeric, not string
        with self.assertRaises(ValueError):
            C.compare_versions("unknown", "3.6")

    def test_range_min_inclusive_max_exclusive(self):
        r = {"min": "3.6", "max": "3.8"}
        self.assertTrue(C.version_in_range("3.6.0", r))
        self.assertTrue(C.version_in_range("v3.7.3", r))
        self.assertFalse(C.version_in_range("3.8", r))
        self.assertFalse(C.version_in_range("3.5.21", r))
        self.assertIsNone(C.version_in_range("unknown", r))
        self.assertEqual(C.version_range_text(r), "≥ 3.6、< 3.8")
        self.assertEqual(C.version_range_text({"min": "8.1"}), "≥ 8.1")


class FieldTest(unittest.TestCase):
    refs = {"ver-a": {}}

    def errs(self, v):
        return C.check_versions_field({"versions": v}, self.refs)

    def test_good(self):
        self.assertEqual(C.check_versions_field({}), [])
        self.assertEqual(self.errs({"min": "3.6", "note": "x", "refs": ["ver-a"]}), [])
        self.assertEqual(self.errs({"max": "17"}), [])
        self.assertEqual(self.errs({"min": "13", "max": "17"}), [])

    def test_bad(self):
        self.assertTrue(any("至少要有 min 或 max" in e for e in self.errs({"note": "x"})))
        self.assertTrue(any("加引號" in e for e in self.errs({"min": 3.6})))
        self.assertTrue(any("加引號" in e for e in self.errs({"min": "latest"})))
        self.assertTrue(any("min 要小於 max" in e for e in self.errs({"min": "17", "max": "13"})))
        self.assertTrue(any("不認得" in e for e in self.errs({"min": "1", "since": "1"})))
        self.assertTrue(any("ver-missing 不存在" in e for e in self.errs({"min": "1", "refs": ["ver-missing"]})))
        self.assertTrue(any("要是 {min" in e for e in self.errs("3.6+")))

    def test_catalog_conversions(self):
        by = {(c["id"], s["id"]): s for c in C.load_components() for s in c.get("signals", [])}
        self.assertEqual(by[("etcd", "quorum")]["versions"]["min"], "3.6")
        self.assertEqual(by[("tidb", "memory-quota")]["versions"]["min"], "8.1")
        self.assertEqual(by[("mariadb", "binlog-growth")]["versions"]["min"], "11.4")
        self.assertNotIn("versions", by[("etcd", "alarms")])  # only EtcdAlarmActive is version-bound
        rules = C.load_rules()
        ann = rules["rules/targets/etcd.yml#EtcdAlarmActive"]["annotations"]
        self.assertEqual(ann[C.RULE_MIN_VERSION], "3.6")
        refs = C.load_refs()
        for (cid, sid), s in by.items():
            self.assertEqual(C.check_versions_field(s, refs), [], f"{cid}#{sid}")


def etcd_rules():
    import rules_to_snapshot as R2S
    snap = R2S.convert([os.path.join(ROOT, "rules", "targets", "etcd.yml")])
    return {"prom": [r for g in snap["data"]["groups"] for r in g["rules"] if r.get("type") == "alerting"]}


class CoverageVersionTest(unittest.TestCase):
    def items(self, version=None, ttype="etcd", job="etcd", features=None):
        t = {"id": "t", "type": ttype, "scraped_by": "prom", "job": job}
        if version is not None:
            t["version"] = version
        if features is not None:
            t["features"] = features
        env = {"components": [{"id": "prom", "type": "prometheus", "version": "3.13.5"}], "observers": [], "targets": [t]}
        got = CC.evaluate(env, None, {1, 2, 3}, live=False, rules_override=etcd_rules())
        return {i["signal"]: i for i in got if i["component"] == "t"}

    def test_old_version_not_applicable(self):
        it = self.items("v3.5.21")
        self.assertEqual(it["quorum"]["status"], "not_applicable")
        self.assertIn("版本 v3.5.21 沒有這個 metric（需要 ≥ 3.6）", it["quorum"]["note"])
        self.assertEqual(it["corruption-check"]["status"], "not_applicable")
        self.assertNotEqual(it["endpoint-up"]["status"], "not_applicable")

    def test_new_version_applies(self):
        it = self.items("3.6.16")
        self.assertNotEqual(it["quorum"]["status"], "not_applicable")
        self.assertNotIn("版本", it["quorum"]["note"])

    def test_unknown_version_asks(self):
        for v in (None, "unknown"):
            it = self.items(v)
            self.assertNotEqual(it["quorum"]["status"], "not_applicable")
            self.assertIn("需要回答：版本", it["quorum"]["note"])
            self.assertNotIn("需要回答：版本", it["endpoint-up"]["note"])  # not version-bound

    def test_rule_level_min_version(self):
        old = self.items("3.5.21")["alarms"]
        self.assertEqual([r["alert"] for r in old["version_excluded"]], ["EtcdAlarmActive"])
        self.assertIn("`EtcdAlarmActive` 在版本 3.5.21 回傳空（需要 ≥ 3.6）", old["note"])
        self.assertNotIn("EtcdAlarmActive", [r["alert"] for r in old["rules"]])
        new = self.items("3.7.3")["alarms"]
        self.assertIn("EtcdAlarmActive", [r["alert"] for r in new["rules"]])
        self.assertNotIn("version_excluded", new)
        unknown = self.items()["alarms"]
        self.assertIn("需要回答：版本", unknown["note"])
        self.assertIn("`EtcdAlarmActive` 需要 ≥ 3.6", unknown["note"])

    def test_version_only_applies_when(self):
        # mariadb binlog-growth: applies_when "MariaDB 11.4 以上（…）" is answered by versions, not features
        old = self.items("10.11.19-MariaDB", ttype="mariadb", job="mysqld", features=["replication-primary"])
        self.assertEqual(old["binlog-growth"]["status"], "not_applicable")
        self.assertIn("需要 ≥ 11.4", old["binlog-growth"]["note"])
        new = self.items("11.4.13-MariaDB", ttype="mariadb", job="mysqld", features=["replication-primary"])
        self.assertNotEqual(new["binlog-growth"]["status"], "not_applicable")
        self.assertNotIn("成立嗎", new["binlog-growth"]["note"])

    def test_fill_versions_from_inventory(self):
        env = {"targets": [{"id": "a", "job": "etcd"}, {"id": "b", "job": "etcd", "version": "3.6.1"},
                           {"id": "c", "job": "pg", "version": "unknown"}]}
        CC.fill_versions(env, {"etcd": "3.5.0", "pg": "16.4"})
        self.assertEqual([t.get("version") for t in env["targets"]], ["3.5.0", "3.6.1", "16.4"])


class PassThroughTest(unittest.TestCase):
    def test_inventory_and_metric_sources(self):
        import inventory_use as IU
        import check_metric_sources as MS
        from test_inventory import run
        tmp = tempfile.mkdtemp()
        ans = os.path.join(tmp, "a.yaml")
        with open(ans, "w") as f:
            yaml.safe_dump({"version": "3.13.5", "target_versions": {"mysqld": "8.4.13"}}, f)
        inv, _ = run(["mysql/mysql-rules.yml"], "mysql/prometheus.yml", answers=ans)
        shutil.rmtree(tmp)
        self.assertEqual(inv["stack"]["target_versions"]["value"], {"mysqld": "8.4.13"})
        self.assertEqual(inv["stack"]["target_versions"]["from"], "user")
        self.assertEqual(IU.target_versions(inv), {"mysqld": "8.4.13"})
        a = IU.answers(inv)
        self.assertEqual(a["target_versions"], {"mysqld": "8.4.13"})
        self.assertEqual(a["version"], "3.13.5")
        self.assertEqual(MS.answer_versions(a), {"ruler": "3.13.5", "targets": {"mysqld": "8.4.13"}})
        self.assertIn("job `mysqld` 8.4.13", MS.report([], [], MS.answer_versions(a)))


if __name__ == "__main__":
    unittest.main()
