import json
import os
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import check_trace as T  # noqa: E402
import validate as V  # noqa: E402

RUNBOOK = "https://hahasula.github.io/alert-trust/components/etcd/#a"


def _sig(sid, rules=(), status="no", evidence="code", metrics=("m",), evaluate_on="self"):
    return {"id": sid, "tier": 2, "metrics": list(metrics), "evidence": evidence, "rules": list(rules),
            "upstream": {"status": status}, "evaluate_on": evaluate_on}


class TraceFixtureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = self.tmp.name

        def write(rel, data):
            path = os.path.join(r, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(data if isinstance(data, str) else yaml.safe_dump(data, allow_unicode=True))

        card = {"id": "etcd", "family": "database", "kind": "target", "signals": [
            _sig("no-rule-no-up", status="no", metrics=["etcd_server_has_leader"]),
            _sig("no-rule-missing-up", status=None),
            _sig("no-rule-partial", status="partial"),
            _sig("rule-code", rules=["rules/targets/etcd.yml#A"], status="yes",
                 metrics=["etcd_server_proposals_failed_total", "up"]),
            _sig("rule-tested", rules=["rules/targets/etcd.yml#B"], evidence="tested", metrics=["etcd_x"]),
            _sig("ask-arch", status="no", metrics=[], evaluate_on="interview"),
            _sig("ask-inventory", status="no", metrics=["etcd_only_new"], evaluate_on="inventory"),
            _sig("side", rules=["rules/targets/etcd.yml#C"], status="yes", evidence="tested",
                 metrics=["queries_total", "patroni_dcs_last_seen", "pgq_wal_bytes", "chq_parts", "mysql_q_rows"]),
        ], "blind_spots": [{"what": "d1", "category": "database"}, {"what": "e1", "category": "exporter"}]}
        card["signals"][1]["upstream"] = {}
        write("catalog/targets/etcd.yaml", card)
        write("catalog/components/vmagent.yaml", {"id": "vmagent", "family": "victoriametrics", "kind": "infra",
                                                  "signals": [_sig("s", metrics=["vmagent_foo"])]})
        write("rules/targets/etcd.yml", {"groups": [{"name": "g", "rules": [
            {"alert": "A", "expr": "rate(etcd_server_proposals_failed_total[5m]) > 0 and on() job:recorded:sum > 1",
             "labels": {"tier": "2"}, "annotations": {"runbook_url": RUNBOOK}},
            {"alert": "B", "expr": "etcd_event_only_total > 0", "labels": {"tier": "2"},
             "annotations": {"runbook_url": RUNBOOK}},
            {"alert": "C", "expr": "probe_success == 0", "labels": {"tier": "2"},
             "annotations": {"runbook_url": RUNBOOK}}]}]})
        # a ':' name and only-after-event counter; `etcd_server_proposals_failed` is saved without _total
        write("lab/targets/etcd/results/names-etcd-1.txt", "etcd_server_has_leader\netcd_server_proposals_failed\netcd_x\netcd_p\netcd_q\n")
        # a second version: one name only there, one name only in the first
        write("lab/targets/etcd/results/names-etcd-2.0.txt",
              "etcd_server_has_leader\netcd_server_proposals_failed\netcd_only_new\ngo_goroutines\nprocess_x\n")
        write("catalog/targets/tidb.yaml", {"id": "tidb", "family": "database", "kind": "target", "signals": [
            _sig("t", metrics=["tidb_a", "tidb_missing"])]})
        write("lab/targets/tidb/results/names-tidb-8.5.8.txt", "\n".join(f"tidb_{c}" for c in "abcdef") + "\n")
        self.root = r
        self.result = {t["card"]: t for t in T.trace(r)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_g3_g5_g6(self):
        t = self.result["etcd"]
        self.assertEqual([g["signal"] for g in t["G3"]], ["no-rule-no-up", "no-rule-missing-up"])
        self.assertEqual(t["G3"][1]["upstream"], "missing")
        self.assertEqual([g["signal"] for g in t["G5"]], ["rule-code"])
        self.assertEqual([g["what"] for g in t["G6"]], ["d1"])

    def test_g4_classes(self):
        t = self.result["etcd"]
        names = {k: [g["metric"] for g in v] for k, v in t["G4"].items()}
        self.assertEqual(names["confirmed"], ["etcd_only_new", "etcd_server_has_leader",
                                              "etcd_server_proposals_failed_total", "etcd_x"])
        self.assertEqual(names["not_in_list"], ["etcd_event_only_total", "m"])
        self.assertEqual(names["external"], ["job:recorded:sum", "up"])
        self.assertEqual(t["lists"], ["lab/targets/etcd/results/names-etcd-1.txt",
                                      "lab/targets/etcd/results/names-etcd-2.0.txt"])
        v = self.result["vmagent"]
        self.assertEqual([g["metric"] for g in v["G4"]["no_list"]], ["vmagent_foo"])
        self.assertEqual(v["lists"], [])

    def test_g3_interview_is_separate(self):
        t = self.result["etcd"]
        self.assertEqual([(g["signal"], g["evaluate_on"]) for g in t["G3.interview"]],
                         [("ask-arch", "interview"), ("ask-inventory", "inventory")])
        self.assertNotIn("ask-arch", [g["signal"] for g in t["G3"]])
        c = T.counts(t)
        self.assertEqual((c["G3"], c["G3.interview"]), (2, 2))
        text = T.render([t])
        self.assertIn("  G3.interview etcd#ask-arch tier 2 evaluate_on interview", text)

    def test_g4_other_source(self):
        t = self.result["etcd"]
        got = {g["metric"]: g["source"] for g in t["G4"]["other_source"]}
        self.assertEqual(sorted(got), ["chq_parts", "mysql_q_rows", "patroni_dcs_last_seen", "pgq_wal_bytes",
                                       "probe_success", "queries_total"])
        self.assertIn("Patroni", got["patroni_dcs_last_seen"])
        self.assertEqual(got["queries_total"], "query-exporter 本身")
        self.assertEqual(got["pgq_wal_bytes"], "query-exporter 自訂查詢")
        self.assertFalse({g["metric"] for g in t["G4"]["not_in_list"]} & set(got))
        self.assertEqual(T.other_source("yugabytedb", "pg_up"), "postgres_exporter（接 YSQL）")
        self.assertIsNone(T.other_source("postgres_exporter", "pg_up"))

    def test_g4_versions_are_kept_per_list(self):
        t = self.result["etcd"]
        by = {g["metric"]: g for g in t["G4"]["confirmed"]}
        self.assertEqual(by["etcd_server_has_leader"]["versions"], ["2.0", "etcd-1"])
        self.assertNotIn("not_in_versions", by["etcd_server_has_leader"])
        self.assertEqual((by["etcd_x"]["versions"], by["etcd_x"]["not_in_versions"]), (["etcd-1"], ["2.0"]))
        self.assertEqual((by["etcd_only_new"]["versions"], by["etcd_only_new"]["not_in_versions"]), (["2.0"], ["etcd-1"]))
        self.assertIn("  G4.confirmed etcd: etcd_x 只在 etcd-1 看到（2.0 的清單沒有）", T.render([t]))
        self.assertEqual(T.list_version("lab/targets/mysql/ha/results/names-my1.txt"), "MySQL 8.4.7")
        self.assertEqual(T.list_version("lab/targets/tidb/results/names-pd-8.5.8.txt"), "8.5.8")
        self.assertEqual(T.list_version("lab/targets/x/results/names-foo.txt"), "foo")

    def test_partial_lists(self):
        info = {x["file"].rsplit("/", 1)[1]: x for x in self.result["etcd"]["list_info"]}
        # names-etcd-1: every name starts with etcd_ (prefix filter); both lack the side exporters the card uses
        self.assertTrue(any("`etcd_`" in w for w in info["names-etcd-1.txt"]["why"]))
        self.assertFalse(any("`etcd_`" in w for w in info["names-etcd-2.0.txt"]["why"]))
        self.assertTrue(all(any("其他 exporter" in w for w in x["why"]) and x["partial"] for x in info.values()))
        self.assertTrue(self.result["etcd"]["lists_partial"])
        td = self.result["tidb"]
        self.assertEqual([(x["partial"], x["version"]) for x in td["list_info"]], [(True, "8.5.8")])
        self.assertIn("`tidb_`", td["list_info"][0]["why"][0])
        self.assertEqual([g["metric"] for g in td["G4"]["not_in_list"]], ["tidb_missing"])
        self.assertIn("部分：只存了 `tidb_` 開頭的名稱", T.render([td]))
        self.assertEqual(T.prefix_filter({"a_x", "b_y", "a_z", "a_1", "a_2"}), None)
        self.assertFalse(self.result["vmagent"]["lists_partial"])
        import unittest.mock as mock
        rel = "lab/targets/etcd/results/names-etcd-2.0.txt"
        with mock.patch.dict(T.PARTIAL_LISTS, {rel: "手動標記"}):
            again = {t["card"]: t for t in T.trace(self.root)}["etcd"]
        self.assertIn("手動標記", next(x for x in again["list_info"] if x["file"] == rel)["why"])

    def test_lab_file_mapping(self):
        cards = {"mysql", "mariadb", "redis_exporter", "postgres_exporter", "tidb"}
        f = T._card_of_names_file
        self.assertEqual(f("lab/targets/mysql/ha/results/names-my1.txt", cards), "mysql")
        self.assertEqual(f("lab/targets/mysql/ha/results/names-mdb2.txt", cards), "mariadb")
        self.assertEqual(f("lab/targets/mysql/results/metric-names-normal.txt", cards), "mariadb")
        self.assertEqual(f("lab/targets/valkey/results/names-e-kg.txt", cards), "redis_exporter")
        self.assertEqual(f("lab/targets/postgres/target/results/names-e-p16.txt", cards), "postgres_exporter")
        self.assertEqual(f("lab/targets/tidb/results/names-pd-8.5.8.txt", cards), "tidb")
        self.assertIsNone(f("lab/targets/unknown/results/names-x.txt", cards))


class E1Test(unittest.TestCase):
    REFS = {"c1": {"kind": "code"}, "x1": {"kind": "experiment"}}

    def card(self, **item):
        return {"_path": "t.yaml", "id": "t", "signals": [dict({"id": "s"}, **item)]}

    def test_code_only_fails(self):
        errs = V.check_tested_refs([self.card(evidence="tested", refs=["c1"])], {}, self.REFS)
        self.assertEqual(len(errs), 1)
        self.assertIn("E1", errs[0])
        self.assertIn("t.yaml.signals[s]", errs[0])

    def test_experiment_ok_and_other_items(self):
        self.assertEqual(V.check_tested_refs([self.card(evidence="tested", refs=["c1", "x1"])], {}, self.REFS), [])
        self.assertEqual(V.check_tested_refs([self.card(evidence="code", refs=["c1"])], {}, self.REFS), [])
        card = {"_path": "t.yaml", "id": "t", "signals": [],
                "usage": [{"id": "u", "evidence": "tested", "refs": []}],
                "silent_failures": [{"evidence": "tested"}]}
        self.assertEqual(len(V.check_tested_refs([card], {"k": {"evidence": "tested", "refs": ["c1"]}}, self.REFS)), 3)

    def test_real_catalog_has_no_violations(self):
        import catalog as C
        self.assertEqual(V.check_tested_refs(C.load_components(), C.load_contracts(), C.load_refs()), [])


class RealCatalogTest(unittest.TestCase):
    def test_counts_match_listing(self):
        result = T.trace()
        text = T.render(result)
        tot = T.totals(result)
        for g in ("G1", "G2", "G3", "G5", "G6"):
            self.assertEqual(tot[g], sum(len(t[g]) for t in result))
            self.assertEqual(tot[g], sum(1 for line in text.splitlines() if line.startswith(f"  {g} ")))
        self.assertEqual(tot["G3.interview"], sum(1 for line in text.splitlines() if line.startswith("  G3.interview ")))
        for cls in ("not_in_list", "no_list", "other_source"):
            self.assertEqual(tot[f"G4.{cls}"], sum(1 for line in text.splitlines() if line.startswith(f"  G4.{cls} ")))
        self.assertEqual(tot["signals"], sum(t["signals"] for t in result))
        row = next(line for line in text.splitlines() if line.startswith("| **合計**"))
        self.assertEqual(row, "| **合計** | " + " | ".join(str(tot[k]) for k in T.COLS) + " |  |")
        json.dumps(result, ensure_ascii=False)  # --json must serialise

    def test_real_lists(self):
        by = {t["card"]: t for t in T.trace()}
        self.assertTrue(by["mysql"]["lists_partial"])  # ha/run.sh saves `__name__=~"mysql_.*"` only
        self.assertEqual({x["version"] for x in by["mysql"]["list_info"]}, {"MySQL 8.4.7"})
        self.assertEqual({x["version"] for x in by["redis_exporter"]["list_info"]},
                         {"Valkey 9.1.3", "Redis 7.2.17", "Redis 8.10.3"})
        self.assertFalse(any(x["partial"] for x in by["prometheus"]["list_info"]))  # check_metrics results
        self.assertEqual({x["version"] for x in by["prometheus"]["list_info"]}, {"2.51.2", "3.13.5"})
        self.assertFalse([g for g in by["postgres_exporter"]["G4"]["not_in_list"]
                          if g["metric"].startswith(("patroni_", "pgq_", "queries_"))])
        self.assertEqual([g["signal"] for g in by["observer"]["G3"] if g["signal"] == "failure-domain"], [])
        self.assertIn("failure-domain", [g["signal"] for g in by["observer"]["G3.interview"]])


class BaselineRatchetTest(unittest.TestCase):
    """catalog/trace-baseline.json: gap counts per card and class may go down, never up."""

    def test_no_gap_count_above_baseline(self):
        worse = T.ratchet(T.trace(), T.load_baseline())
        self.assertEqual(worse, [], "check_trace 的缺口比 catalog/trace-baseline.json 多：" + "；".join(worse)
                         + f"。把缺口修掉（補 rule、上游狀態、tested 證據或已存的 metric 清單），或確認是刻意的之後更新基準："
                           f"`{T.UPDATE_BASELINE}`（把 diff 一起 commit）")

    def test_baseline_covers_every_card_and_class(self):
        base = T.load_baseline()["cards"]
        result = T.trace()
        self.assertEqual(sorted(base), sorted(t["card"] for t in result))
        for t in result:
            self.assertEqual(sorted(base[t["card"]]), sorted(k for k in T.counts(t) if k != "signals"))
        self.assertIn(T.UPDATE_BASELINE, T.load_baseline()["note"])

    def test_ratchet_reports_increase_only(self):
        result = T.trace()
        base = T.baseline_of(result)
        self.assertEqual(T.ratchet(result, base), [])
        card = result[0]["card"]
        base["cards"][card]["G5"] -= 1
        base["cards"][card]["G4.confirmed"] += 5  # not a gap class
        base["cards"][card]["G6"] += 3            # fewer gaps than the baseline is fine
        self.assertEqual(T.ratchet(result, base), [f"{card} G5: {base['cards'][card]['G5']} → {base['cards'][card]['G5'] + 1}"])
        del base["cards"][card]
        self.assertTrue(all(x.startswith(card + " ") for x in T.ratchet(result, base)))
        self.assertTrue(T.ratchet(result, base))

    def test_write_baseline_command(self):
        import io
        import contextlib
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "catalog"))
            with mock.patch.object(T.C, "ROOT", d), mock.patch.object(sys, "argv", ["check_trace.py", "--write-baseline"]), \
                    mock.patch.object(T, "trace", lambda: [T.trace_card({"id": "x", "signals": []}, {}, {})]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(T.main(), 0)
            with open(os.path.join(d, T.BASELINE), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["cards"]["x"]["G3"], 0)


class CiTraceStepTest(unittest.TestCase):
    def test_only_one_way_to_ignore_failure(self):
        with open(os.path.join(ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8") as f:
            wf = yaml.safe_load(f)
        step = next(s for j in wf["jobs"].values() for s in j["steps"] if "check_trace.py" in str(s.get("run", "")))
        ways = int(bool(step.get("continue-on-error"))) + int("|| true" in step["run"])
        self.assertEqual(ways, 1, step)


if __name__ == "__main__":
    unittest.main()
