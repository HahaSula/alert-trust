"""Intentional short windows, question ranking (score = overturns × cost, must_ask), answers write-back, timing
inputs / sensitivity, vmalert evidence labels, and Patroni jobs (inventory exporter inference, coverage job)."""
import json
import os
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_map as BM  # noqa: E402
import catalog as C  # noqa: E402
import check_coverage as CC  # noqa: E402
import fit_contracts as FC  # noqa: E402
import inventory as INV  # noqa: E402
import inventory_use as IU  # noqa: E402
import review as RV  # noqa: E402

FXI = os.path.join(ROOT, "tests", "fixtures", "inventory")
MYSQL_RULES, MYSQL_PROM = os.path.join(FXI, "mysql", "mysql-rules.yml"), os.path.join(FXI, "mysql", "prometheus.yml")
ETCD_RULES, ETCD_PROM = os.path.join(FXI, "etcd", "etcd-rules.yml"), os.path.join(FXI, "etcd", "prometheus.yml")

PROM = """global: {scrape_interval: 30s, scrape_timeout: 10s, evaluation_interval: 30s}
rule_files: [rules.yml]
alerting: {alertmanagers: [{static_configs: [{targets: ["am:9093"]}]}]}
scrape_configs:
  - job_name: api
    static_configs: [{targets: ["api:8080"]}]
"""
RULES = """groups:
  - name: g
    rules:
      - alert: ErrorsBurst
        expr: rate(http_errors_total{job="api"}[1m]) > 1
        labels: {severity: critical}
      - alert: ErrorsSlow
        expr: rate(http_errors_total{job="api"}[10m]) > 1
        labels: {severity: warning}
"""


def write(d, name, text):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text if isinstance(text, str) else yaml.safe_dump(text, allow_unicode=True))
    return p


def inventory(t, rules, config=None, answers=None, env=None, merge=None, out="inventory.yaml", kind=None):
    out = os.path.join(t, out)
    argv = ["--rules", *rules, "--out", out]
    for flag, v in (("--config", config), ("--answers", answers), ("--env", env), ("--merge", merge),
                    ("--config-kind", kind)):
        if v:
            argv += [flag, v]
    RV._quiet(INV.main, argv)
    with open(out, encoding="utf-8") as f:
        return yaml.safe_load(f), out


def fit(t, argv, out="fit"):
    o = os.path.join(t, out)
    RV._quiet(FC.main, argv + ["--out", o])
    with open(os.path.join(o, "contracts-fit.json"), encoding="utf-8") as f:
        js = json.load(f)
    with open(os.path.join(o, "contracts-fit.md"), encoding="utf-8") as f:
        return js, f.read()


class ShortWindowQuestionTest(unittest.TestCase):
    def test_mandatory_in_first_three(self):
        """Many higher-scored questions (job mismatch, no Alertmanager, version ...): the window question still
        lands in the first 3, with the exact wording."""
        with tempfile.TemporaryDirectory() as t:
            inv, _ = inventory(t, [MYSQL_RULES], MYSQL_PROM)
        q = next(x for x in inv["questions"] if x["answer_key"] == "keep_range")
        self.assertIn("這個視窗是刻意的嗎？", q["q"])
        self.assertTrue(q["mandatory"])
        self.assertTrue(q["ask_now"])
        self.assertLess(inv["questions"].index(q), 3)
        self.assertEqual(sum(x["ask_now"] for x in inv["questions"]), 3)

    def test_answered_is_not_asked_again(self):
        with tempfile.TemporaryDirectory() as t:
            ans = write(t, "a.yaml", {"keep_range": {"MySQLSlowQueriesBurst": True}})
            inv, _ = inventory(t, [MYSQL_RULES], MYSQL_PROM, answers=ans)
        self.assertNotIn("keep_range", [x["answer_key"] for x in inv["questions"]])
        r = next(x for x in inv["rules"] if x["name"] == "MySQLSlowQueriesBurst")
        self.assertEqual(r["keep_range"], {"value": True, "from": "user"})

    def test_annotation_unknown_stays_unknown(self):
        with tempfile.TemporaryDirectory() as t:
            rules = write(t, "rules.yml", RULES.replace("labels: {severity: critical}",
                                                        "labels: {severity: critical}\n        annotations: {keep_range: unknown}"))
            prom = write(t, "prometheus.yml", PROM)
            inv, _ = inventory(t, [rules], prom)
        r = next(x for x in inv["rules"] if x["name"] == "ErrorsBurst")
        self.assertEqual(r["keep_range"]["from"], "unknown")
        self.assertTrue(r["ranges"][0]["short"])
        self.assertIn("keep_range", [x["answer_key"] for x in inv["questions"] if x["ask_now"]])


class FitKeepRangeTest(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.rules = write(self.t, "rules.yml", RULES)
        self.prom = write(self.t, "prometheus.yml", PROM)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.t)

    def test_unknown_keep_options_first_and_not_written(self):
        fitted = os.path.join(self.t, "fitted.yml")
        js, md = fit(self.t, ["--rules", self.rules, "--config", self.prom, "--write-fitted", fitted])
        self.assertEqual(js["keep_unknown"], ["ErrorsBurst"])
        adv = md.split("## 1.")[0]
        self.assertLess(adv.index("這個視窗是刻意的嗎？"), adv.index("不符合契約"))
        self.assertIn("### 保留視窗", md)
        sec3 = md.split("## 3.")[1].split("## 4.")[0]
        self.assertLess(sec3.index("保留：`query_offset`"), sec3.index("改視窗（只在不是刻意時）"))
        self.assertIn(FC.QO_UNKNOWN, sec3)          # version unknown: query_offset gated
        self.assertIn(FC.ANCHORED_UNKNOWN, sec3)
        with open(fitted, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        r = next(x for g in doc["groups"] for x in g["rules"] if x["alert"] == "ErrorsBurst")
        self.assertIn("[1m]", r["expr"])                       # not lengthened
        self.assertEqual(r["annotations"]["keep_range"], "unknown")

    def test_changed_window_is_semantic_changed_until_confirmed(self):
        fitted = os.path.join(self.t, "fitted.yml")
        fit(self.t, ["--rules", self.rules, "--config", self.prom, "--change-range", "ErrorsBurst",
                     "--write-fitted", fitted])
        with open(fitted, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        r = next(x for g in doc["groups"] for x in g["rules"] if x["alert"] == "ErrorsBurst")
        self.assertNotIn("[1m]", r["expr"])
        self.assertEqual(r["annotations"][FC.ORIGINAL_RANGE_ANN], "[1m]")
        # a later run on the proposal: the contract holds, but the meaning changed and nobody confirmed it
        js, md = fit(self.t, ["--rules", fitted, "--config", self.prom], out="fit2")
        row = next(x for x in js["rows"] if x["rule"] == "ErrorsBurst")
        self.assertTrue(row["ok"])
        self.assertEqual(row["status"], FC.SEMANTIC_CHANGED)
        sec2 = md.split("## 2.")[1].split("## 3.")[0]
        self.assertIn(f"### {FC.SEMANTIC_CHANGED}", sec2)
        self.assertNotIn("`ErrorsBurst`", sec2.split(f"### {FC.SEMANTIC_CHANGED}")[0])  # not listed as 符合
        # --original works without the annotation, and a confirmation (keep_range: false) makes it 符合
        plain = write(self.t, "plain.yml", RULES.replace("[1m]", "[2m]"))
        js, _ = fit(self.t, ["--rules", plain, "--config", self.prom, "--original", self.rules], out="fit3")
        self.assertEqual(next(x for x in js["rows"] if x["rule"] == "ErrorsBurst")["status"], FC.SEMANTIC_CHANGED)
        ans = write(self.t, "a.yaml", {"keep_range": {"ErrorsBurst": False}})
        js, _ = fit(self.t, ["--rules", fitted, "--config", self.prom, "--answers", ans], out="fit4")
        self.assertEqual(next(x for x in js["rows"] if x["rule"] == "ErrorsBurst")["status"], "符合")

    def test_inventory_merge_keeps_original_rules(self):
        _, inv_path = inventory(self.t, [self.rules], self.prom)
        plain = write(self.t, "plain.yml", RULES.replace("[1m]", "[2m]"))
        inv, inv2 = inventory(self.t, [plain], self.prom, merge=inv_path, out="inv2.yaml")
        self.assertEqual(inv["sources"]["original_rules"], [self.rules])
        js, _ = fit(self.t, ["--inventory", inv2])
        self.assertEqual(next(x for x in js["rows"] if x["rule"] == "ErrorsBurst")["status"], FC.SEMANTIC_CHANGED)

    def test_review_summary(self):
        out = os.path.join(self.t, "report")
        RV._quiet(RV.main, ["--rules", self.rules, "--config", self.prom, "--out", out])
        with open(os.path.join(out, "summary.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("先問「這個視窗是刻意的嗎？」", md)
        self.assertIn("不要先改視窗", md)
        self.assertIn("--merge", md)


class RankingTest(unittest.TestCase):
    def test_must_ask_validation(self):
        refs = C.load_refs(C.ROOT)
        ok = {"_path": "x", "must_ask": [{"id": "a-b", "question": "q?", "why": "w", "refs": ["etcd-exp-raw"]}]}
        self.assertEqual(C.check_must_ask(ok, refs, set(CC.FEATURES)), [])
        bad = {"_path": "x", "must_ask": [{"id": "A", "question": "", "refs": ["nope"], "applies_when": "zzz", "x": 1},
                                          {"id": "a", "question": "q", "why": "w", "refs": []}]}
        errs = "\n".join(C.check_must_ask(bad, refs, set(CC.FEATURES)))
        for frag in ("kebab-case", "缺少 question", "缺少 why", "'nope' 不存在", "applies_when", "不認得 'x'", "要有 refs"):
            self.assertIn(frag, errs)
        self.assertEqual(C.check_must_ask({"_path": "x"}, refs), [])

    def test_cards_and_map(self):
        cards = {c["id"]: c for c in C.load_components()}
        for cid in ("etcd", "postgres_exporter", "redis_exporter", "mongodb_exporter", "vmalert", "victoriametrics"):
            self.assertTrue(cards[cid].get("must_ask"), cid)
        m = BM.build_map(list(cards.values()))
        self.assertEqual(m["cards"]["etcd"]["must_ask"][0]["id"], "metrics-level")
        with open(BM.MAP_PATH, encoding="utf-8") as f:
            self.assertIn("metrics-level", f.read())

    def test_must_ask_ranked_high_and_by_score(self):
        with tempfile.TemporaryDirectory() as t:
            inv, _ = inventory(t, [ETCD_RULES], ETCD_PROM)
        q = next(x for x in inv["questions"] if x["id"] == "must_ask.etcd.metrics-level")
        self.assertIn("--metrics", q["q"])
        self.assertEqual(q["affects"], 1)
        self.assertTrue(q["ask_now"])
        self.assertEqual(q["score"], q["overturns"] * q["cost"])
        scores = [x["score"] for x in inv["questions"] if not x.get("mandatory")]
        self.assertEqual(scores, sorted(scores, reverse=True))
        # a question touching more rules but overturning less ranks below
        many = [x for x in inv["questions"] if x["affects"] > 1 and x["score"] < q["score"]]
        for x in many:
            self.assertGreater(inv["questions"].index(x), inv["questions"].index(q))

    def test_must_ask_answered_in_answers_or_inventory(self):
        with tempfile.TemporaryDirectory() as t:
            ans = write(t, "a.yaml", {"facts": {"etcd.metrics-level": "basic"}})
            inv, _ = inventory(t, [ETCD_RULES], ETCD_PROM, answers=ans)
            self.assertNotIn("must_ask.etcd.metrics-level", [x["id"] for x in inv["questions"]])
            self.assertEqual(inv["facts"]["etcd.metrics-level"], {"value": "basic", "from": "user"})
            # written into the inventory instead, then --merge
            inv, p = inventory(t, [ETCD_RULES], ETCD_PROM, out="i1.yaml")
            inv["facts"]["etcd.metrics-level"] = {"value": "extensive", "from": "user", "confirmed": True}
            with open(p, "w", encoding="utf-8") as f:
                yaml.safe_dump(inv, f, allow_unicode=True)
            inv, _ = inventory(t, [ETCD_RULES], ETCD_PROM, merge=p, out="i2.yaml")
            self.assertNotIn("must_ask.etcd.metrics-level", [x["id"] for x in inv["questions"]])
            # answered: [id] (the user does not know) is not asked again either
            ans = write(t, "b.yaml", {"answered": ["version"]})
            inv, _ = inventory(t, [ETCD_RULES], ETCD_PROM, answers=ans, out="i3.yaml")
            self.assertNotIn("version", [x["id"] for x in inv["questions"]])

    def test_vmalert_cards_present(self):
        vm = os.path.join(FXI, "pg-vm")
        with tempfile.TemporaryDirectory() as t:
            inv, _ = inventory(t, [os.path.join(vm, "pg-rules.yml")], os.path.join(vm, "vmagent-scrape.yml"), kind="vmagent")
        ids = [x["id"] for x in inv["questions"]]
        self.assertIn("must_ask.vmalert.data-latency", ids)
        self.assertIn("must_ask.victoriametrics.dedup-interval", ids)
        q = next(x for x in inv["questions"] if x["id"] == "must_ask.vmalert.data-latency")
        self.assertEqual(q["answer_key"], "latency")


class FactsTest(unittest.TestCase):
    ENV = {"components": [{"id": "vma", "type": "vmalert", "version": "v1.155.0", "latency": "45s"}],
           "targets": [{"id": "db", "type": "postgres_exporter", "job": "postgres", "version": "16.4"}]}

    def test_facts_reading(self):
        f = IU.facts({}, self.ENV)
        self.assertEqual((f["ruler"], f["version"], f["latency"]), ("vmalert", "v1.155.0", "45s"))
        self.assertEqual(f["target_versions"], {"postgres": "16.4"})
        f = IU.facts({"version": "v1.150.0", "target_versions": {"postgres": "15"}}, self.ENV)
        self.assertEqual(f["version_src"], "answers.version")
        self.assertTrue(f["conflicts"])
        self.assertEqual(f["target_versions"], {"postgres": "16.4"})    # env.yaml wins for targets
        self.assertEqual(IU.facts({"version": "不知道"}, {}).get("version"), None)

    def test_inventory_reads_env_and_does_not_ask_version(self):
        vm = os.path.join(FXI, "pg-vm")
        with tempfile.TemporaryDirectory() as t:
            env = write(t, "env.yaml", self.ENV)
            inv, _ = inventory(t, [os.path.join(vm, "pg-rules.yml")], os.path.join(vm, "vmagent-scrape.yml"), env=env,
                               kind="vmagent")
        self.assertEqual(inv["stack"]["version"]["value"], "v1.155.0")
        self.assertEqual(inv["stack"]["version"]["from"], "user")
        self.assertNotIn("version", [x["id"] for x in inv["questions"]])
        self.assertEqual(inv["stack"]["latency"]["value"], "45s")
        self.assertNotIn("must_ask.vmalert.data-latency", [x["id"] for x in inv["questions"]])

    def test_fit_contracts_reads_answers_and_env(self):
        with tempfile.TemporaryDirectory() as t:
            rules, prom = write(t, "rules.yml", RULES), write(t, "prometheus.yml", PROM)
            env = write(t, "env.yaml", {"components": [{"id": "p", "type": "prometheus", "version": "2.48.1"}]})
            js, md = fit(t, ["--rules", rules, "--config", prom, "--env", env])
            self.assertEqual(js["prom_version"], "2.48.1")
            self.assertNotIn(FC.QO_UNKNOWN, md)
            ans = write(t, "a.yaml", {"version": "3.5.0"})
            js, _ = fit(t, ["--rules", rules, "--config", prom, "--answers", ans], out="f2")
            self.assertEqual(js["prom_version"], "3.5.0")

    def test_check_coverage_ruler_version_from_inventory(self):
        env = {"components": [{"id": "p", "type": "prometheus"}]}
        inv = {"stack": {"ruler": {"value": "prometheus", "from": "file"}, "version": {"value": "3.5.0", "from": "user"}}}
        CC.fill_ruler_versions(env, inv)
        self.assertEqual(env["components"][0]["version"], "3.5.0")


class NotifyInputsTest(unittest.TestCase):
    def _args(self, **kw):
        a = type("A", (), {})()
        a.stack, a.S, a.E, a.O, a.d, a.latency, a.eval_delay, a.notify_within = "prometheus", 30.0, 30.0, 0.0, None, None, 30.0, 180.0
        a.job_interval, a.job_duration, a.job_timeout, a.timeout, a.p99, a.job_p99, a.cfg_jobs = {}, {}, {}, 10.0, None, {}, None
        a.k, a.job_failrun, a.src = 1, {}, {"S": "file（--config）", "E": "default（Prometheus／vmagent 預設）"}
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    DOCS = [("x", {"groups": [{"name": "g", "rules": [{"alert": "A", "expr": "x > 1", "for": "1m",
                                                       "labels": {"severity": "critical"}}]}]})]

    def test_inputs_sources_sensitivity(self):
        n = FC.notify_checks(self._args(), self.DOCS, None)[0]
        src = {x["name"]: x["source"] for x in n["inputs"]}
        self.assertTrue(src["S"].startswith("file"))
        self.assertTrue(src["d"].startswith("assumed"))           # d = τ
        self.assertTrue(src["E"].startswith("default"))
        self.assertTrue(src["group_wait"].startswith("Alertmanager 預設"))
        self.assertIn("k", src)
        steps = {x["step"].split()[0]: x["delta"] for x in n["sensitivity"]}
        self.assertEqual(steps["d"], 5.0)
        self.assertEqual(steps["k"], 30.0)
        # total 30 + 10 + 30 + 60 + 30 = 160s, requirement 3m: 20s margin -> warning
        self.assertEqual(n["total"], 160.0)
        self.assertTrue(n["margin_warn"])
        md = "\n".join(FC.notify_section(self._args(), [n], None))
        self.assertIn("警告", md)
        self.assertIn("assumed", md)

    def test_vmalert_latency_step(self):
        a = self._args(stack="vmalert", latency=None, src={"L": "assumed（L = 該 job 的 S）"})
        n = FC.notify_checks(a, self.DOCS, None)[0]
        src = {x["name"]: x["source"] for x in n["inputs"]}
        self.assertTrue(src["L"].startswith("assumed"))
        st = next(x for x in n["sensitivity"] if x["step"].startswith("L"))
        self.assertEqual(st["delta"], 30.0)   # max(30 + 30, 30) - max(30, 30)

    def test_tau_changed_remeasure(self):
        with tempfile.TemporaryDirectory() as t:
            rules = write(t, "rules.yml", RULES)
            prom = write(t, "prometheus.yml", PROM)
            js, md = fit(t, ["--rules", rules, "--config", prom, "--job-duration", "api=9.8s", "--job-timeout", "api=20s",
                             "--notify-within", "5m"])
        self.assertIn("需要重新量", md)
        self.assertTrue(any(c["remeasure"] for c in js["env_checks"]))
        self.assertTrue(any(n["remeasure"] for n in js["notify"]))
        self.assertNotIn("耗時數據是否被逾時截斷（job api）：d_max 9.8s < 0.95", md)


class VmalertEvidenceTest(unittest.TestCase):
    def test_labels(self):
        with tempfile.TemporaryDirectory() as t:
            rules = write(t, "rules.yml", RULES)
            _, md = fit(t, ["--rules", rules, "--stack", "vmalert", "--scrape-interval", "30s"])
            self.assertIn("Prometheus 語意", md)
            self.assertIn("只驗語法", md)
            vm = os.path.join(FXI, "pg-vm")
            out = os.path.join(t, "r")
            RV._quiet(RV.main, ["--rules", os.path.join(vm, "pg-rules.yml"), "--config", os.path.join(vm, "vmagent-scrape.yml"),
                                "--config-kind", "vmagent", "--out", out])
            with open(os.path.join(out, "summary.md"), encoding="utf-8") as f:
                s = f.read()
        self.assertIn("Prometheus 語意", s)
        self.assertIn("只驗語法", s)


class PatroniTest(unittest.TestCase):
    PROM = """global: {scrape_interval: 15s, scrape_timeout: 10s}
scrape_configs:
  - job_name: postgres
    static_configs: [{targets: ["db:9187"]}]
  - job_name: patroni
    static_configs: [{targets: ["db:8008"]}]
"""
    RULES = """groups:
  - name: g
    rules:
      - alert: PgDown
        expr: pg_up{job="postgres"} == 0
      - alert: PatroniNoLeader
        expr: max(patroni_primary{job="patroni"}) == 0
      - alert: PatroniDcsStale
        expr: time() - patroni_dcs_last_seen{job="patroni"} > 60
"""

    def test_inventory_exporter_from_prefix(self):
        with tempfile.TemporaryDirectory() as t:
            inv, _ = inventory(t, [write(t, "r.yml", self.RULES)], write(t, "p.yml", self.PROM))
        jobs = {j["name"]: j for j in inv["jobs"]}
        self.assertEqual(jobs["patroni"]["exporter"]["value"], "patroni")
        self.assertIn("Patroni REST API", jobs["patroni"]["exporter"]["basis"])
        self.assertEqual(jobs["postgres"]["exporter"]["value"], "postgres_exporter")
        self.assertEqual(jobs["patroni"]["exporter_deadline"]["from"], "unknown")
        ids = [q["id"] for q in inv["questions"]]
        self.assertIn("must_ask.postgres_exporter.patroni-modes", ids)   # applies_when patroni: a patroni job

    def test_coverage_matches_the_patroni_job(self):
        card = {c["id"]: c for c in C.load_components()}["postgres_exporter"]
        sig = next(s for s in card["signals"] if s.get("applies_when") == "patroni" and "patroni_primary" in s.get("metrics", []))
        comp = {"id": "db", "type": "postgres_exporter", "job": "postgres"}
        rule = {"name": "PatroniNoLeader", "query": 'max(patroni_primary{job="patroni"}) == 0'}
        self.assertEqual(CC.rule_match(sig, rule, comp), "ok")                         # producing job unknown
        self.assertEqual(CC.rule_match(sig, rule, comp, {"patroni": ["patroni"]}), "ok")
        self.assertEqual(CC.rule_match(sig, rule, dict(comp, feature_jobs={"patroni": "patroni"})), "ok")
        self.assertEqual(CC.rule_match(sig, rule, comp, {"patroni": ["patroni-ha"]}), "job_mismatch")
        # a postgres_exporter signal is still checked against the target's job
        own = next(s for s in card["signals"] if "pg_up" in s.get("metrics", []) and not s.get("applies_when"))
        self.assertEqual(CC.rule_match(own, {"name": "x", "query": 'pg_up{job="pg"} == 0'}, comp), "job_mismatch")


if __name__ == "__main__":
    unittest.main()
