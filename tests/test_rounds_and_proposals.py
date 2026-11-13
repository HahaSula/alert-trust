"""Across rounds and for proposed files: a put-off must_ask question comes back first and its card default applies
as an assumption until answered; 「不知道」 answers are recorded and not asked again; approving a proposal is not
keep_range; placeholders, scrape_interval vs group interval, and new rules in proposals (tests, upstream copies).
Fixtures: tests/fixtures/inventory/etcd."""
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
import inventory_use as IU  # noqa: E402
import proposal_check as PC  # noqa: E402
import review as RV  # noqa: E402

FXI = os.path.join(ROOT, "tests", "fixtures", "inventory", "etcd")
RULES, PROM = os.path.join(FXI, "etcd-rules.yml"), os.path.join(FXI, "prometheus.yml")
SLOW, MA = "etcdGRPCRequestsSlow", "must_ask.etcd.metrics-level"


def write(d, name, data):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        if isinstance(data, str):
            f.write(data)
        else:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    return p


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def inventory(t, answers=None, merge=None, name="inv.yaml", rules=RULES, config=PROM):
    out = os.path.join(t, name)
    argv = ["--rules", rules, "--config", config, "--out", out]
    if answers is not None:
        argv += ["--answers", write(t, "answers-" + name, answers)]
    if merge:
        argv += ["--merge", merge]
    RV._quiet(INV.main, argv)
    with open(out, encoding="utf-8") as f:
        return yaml.safe_load(f)


def q(inv, qid):
    return next((x for x in inv["questions"] if x["id"] == qid), None)


def rule(inv, name):
    return next(r for r in inv["rules"] if r["name"] == name)


class DefaultAssumptionTest(unittest.TestCase):
    def test_card_default_is_valid_and_exported(self):
        etcd = next(c for c in C.load_components() if c["id"] == "etcd")
        self.assertEqual(next(x for x in etcd["must_ask"] if x["id"] == "metrics-level")["default"], "basic")
        card = {"_path": "x", "signals": [], "must_ask": [{"id": "a", "question": "q", "why": "w", "refs": ["r"], "default": True}]}
        self.assertTrue(C.check_must_ask(card, {"r": {}}))
        card["must_ask"][0]["default"] = "basic"
        self.assertEqual(C.check_must_ask(card, {"r": {}}), [])
        m = yaml.safe_load(read(os.path.join(ROOT, "catalog", "map.yaml")))
        self.assertIn("default", json.dumps(m["cards"]["etcd"]["must_ask"]))

    def test_unanswered_applies_default(self):
        with tempfile.TemporaryDirectory() as t:
            inv = inventory(t, {})
        node = inv["facts"]["etcd.metrics-level"]
        self.assertEqual((node["value"], node["from"], node["assumed"]), ("basic", "default", True))
        self.assertIn(IU.ASSUMED_DEFAULT, node["note"])
        self.assertEqual(rule(inv, SLOW)["fires"]["value"], "never")
        self.assertTrue(rule(inv, SLOW)["fires"].get("assumed"))
        f = next(x for x in inv["findings_from_files"] if x["kind"] == "must_ask_no_data")
        self.assertTrue(f["assumed"])
        self.assertIn(IU.ASSUMED_DEFAULT, f["fact"])
        self.assertTrue(q(inv, MA))  # still asked: the default is an assumption, not an answer

    def test_reports_count_default_no_data(self):
        with tempfile.TemporaryDirectory() as t:
            out = os.path.join(t, "r")
            RV._quiet(RV.main, ["--rules", RULES, "--config", PROM, "--promtool-version", "none", "--out", out])
            fit = json.loads(read(os.path.join(out, "contracts-fit.json")))
            ms = json.loads(read(os.path.join(out, "metric-sources.json")))["rows"]
            s = read(os.path.join(out, "summary.md"))
        self.assertTrue(any(r["nodata"] for r in fit["rows"] if r["rule"] == SLOW))
        self.assertIn("沒有資料 1 條", s)
        row = next(r for r in ms if r["rule"] == SLOW)
        self.assertEqual(row["status"], "setting_absent")
        self.assertIn(IU.ASSUMED_DEFAULT, row["detail"])
        self.assertIn(IU.ASSUMED_DEFAULT, s.split("## 假設")[1])

    def test_answer_overrides_default(self):
        with tempfile.TemporaryDirectory() as t:
            inv = inventory(t, {"facts": {"etcd": {"metrics-level": "extensive"}}})
        self.assertEqual(inv["facts"]["etcd.metrics-level"]["from"], "user")
        self.assertNotEqual((rule(inv, SLOW).get("fires") or {}).get("value"), "never")
        vals = IU.fact_values(inv)
        self.assertEqual(vals.assumed, set())

    def test_coverage_sees_default(self):
        with tempfile.TemporaryDirectory() as t:
            inv = inventory(t, {})
        eff = IU.applied_effects(IU.fact_values(inv))
        self.assertTrue(eff and all(e["assumed"] for e in eff))
        self.assertIn(IU.ASSUMED_DEFAULT, IU.effect_text(eff[0]))


class DeferredTest(unittest.TestCase):
    def test_put_off_must_ask_comes_back_first(self):
        with tempfile.TemporaryDirectory() as t:
            r0 = inventory(t, {}, name="r0.yaml")
            self.assertTrue(q(r0, MA)["ask_now"])
            # round 1: the assistant asked other questions; must_ask unanswered → mandatory and first
            r1 = inventory(t, {"notify_within": "5m"}, merge=os.path.join(t, "r0.yaml"), name="r1.yaml")
            self.assertEqual(r1["questions"][0]["id"], MA)
            self.assertTrue(r1["questions"][0]["mandatory"] and r1["questions"][0]["deferred"])
            self.assertIn(MA, r1["deferred"])
            # round 2: still unanswered → still first
            r2 = inventory(t, {"notify_within": "5m"}, merge=os.path.join(t, "r1.yaml"), name="r2.yaml")
            self.assertEqual(r2["questions"][0]["id"], MA)
            # answered → gone, and no longer deferred
            r3 = inventory(t, {"notify_within": "5m", "facts": {"etcd.metrics-level": "basic"}},
                           merge=os.path.join(t, "r2.yaml"), name="r3.yaml")
        self.assertIsNone(q(r3, MA))
        self.assertNotIn(MA, r3["deferred"])

    def test_explicit_deferred_list(self):
        with tempfile.TemporaryDirectory() as t:
            inv = inventory(t, {"deferred": ["producers"]})
        p = q(inv, "producers")
        self.assertTrue(p["deferred"] and p["ask_now"])
        self.assertEqual(inv["questions"][0]["id"], "producers")


class UnknownAnswerTest(unittest.TestCase):
    def test_unknown_list_and_value_are_not_asked_again(self):
        with tempfile.TemporaryDirectory() as t:
            inv = inventory(t, {"unknown": ["external_observer"], "version": "不知道"})
        ids = [x["id"] for x in inv["questions"]]
        self.assertNotIn("external_observer", ids)
        self.assertNotIn("version", ids)
        d = {x["id"]: x["assumption"] for x in inv["declined"]}
        self.assertIn("沒有外部觀察者", d["external_observer"])
        self.assertIn("版本未知", d["version"])
        self.assertEqual(sorted(inv["unknown"]), ["external_observer", "version"])

    def test_unknown_must_ask_uses_default(self):
        with tempfile.TemporaryDirectory() as t:
            inv = inventory(t, {"facts": {"etcd": {"metrics-level": "沒權限"}}})
        self.assertIsNone(q(inv, MA))
        node = inv["facts"]["etcd.metrics-level"]
        self.assertTrue(node["declined"] and node["assumed"])
        self.assertEqual(rule(inv, SLOW)["fires"]["value"], "never")
        self.assertIn(IU.ASSUMED_DEFAULT, next(x["assumption"] for x in inv["declined"] if x["id"] == MA))

    def test_unknown_survives_merge_and_summary(self):
        with tempfile.TemporaryDirectory() as t:
            inventory(t, {"unknown": ["external_observer"]}, name="r0.yaml")
            r1 = inventory(t, {}, merge=os.path.join(t, "r0.yaml"), name="r1.yaml")
            self.assertIsNone(q(r1, "external_observer"))
            out = os.path.join(t, "r")
            RV._quiet(RV.main, ["--rules", RULES, "--config", PROM, "--answers", write(t, "a.yaml", {"unknown": ["external_observer"]}),
                                "--promtool-version", "none", "--out", out])
            s = read(os.path.join(out, "summary.md"))
        self.assertIn("使用者答「不知道」：`external_observer`", s)


class ApprovalIsNotKeepRangeTest(unittest.TestCase):
    def test_no_range_rule_never_gets_keep_range(self):
        with tempfile.TemporaryDirectory() as t:
            inv = inventory(t, {"keep_range": {"etcdNoLeader": False}})
        self.assertNotIn("keep_range", rule(inv, "etcdNoLeader"))
        self.assertEqual(inv["answers_ignored"][0]["key"], "keep_range.etcdNoLeader")
        self.assertNotIn("etcdNoLeader", IU.change_range(inv))

    def test_approved_recorded_separately(self):
        appr = [{"rule": SLOW, "change": "改成 last_over_time", "round": 2}]
        with tempfile.TemporaryDirectory() as t:
            inv = inventory(t, {"approved": appr})
        self.assertEqual(inv["approved"], appr)
        self.assertEqual(rule(inv, SLOW)["keep_range"]["from"], "unknown")
        self.assertNotIn(SLOW, IU.change_range(inv))

    def test_keep_range_unknown_is_kept(self):
        with tempfile.TemporaryDirectory() as t:
            inv = inventory(t, {"keep_range": {SLOW: "不知道"}})
        self.assertTrue(rule(inv, SLOW)["keep_range"]["declined"])
        self.assertIn(SLOW, IU.keep_range(inv))


PROPOSED_PROM = """global:
  scrape_interval: 15s
  evaluation_interval: 30s
rule_files: [rules/*.yml]
alerting:
  alertmanagers:
    - static_configs: [{ targets: ["alertmanager.example.invalid:9093"] }]
scrape_configs:
  - job_name: kube-etcd
    static_configs: [{ targets: ["10.0.0.11:2379"] }]
  - job_name: prometheus
    static_configs: [{ targets: ["localhost:9090"] }]
"""
PROPOSED_AM = """route: { receiver: pager }
receivers:
  - name: pager
    webhook_configs: [{ url: "https://dms.example.invalid/ping" }]
"""


class PlaceholderTest(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(IU.placeholder("https://dms.example.invalid/ping"), "dms.example.invalid")
        self.assertEqual(IU.placeholder("smtp.example.com:25"), "smtp.example.com")
        self.assertIsNone(IU.placeholder("10.0.0.1:9093"))
        self.assertIsNone(IU.placeholder("env/env.example.yaml"))

    def test_not_a_finding_and_listed(self):
        with tempfile.TemporaryDirectory() as t:
            cfg, am = write(t, "prometheus.yml", PROPOSED_PROM), write(t, "alertmanager.yml", PROPOSED_AM)
            inv = inventory(t, {}, config=cfg)
            self.assertFalse([u for u in inv["unscraped_components"] if u["component"] == "alertmanager"])
            self.assertIn("待填", inv["placeholders"][0]["fact"])
            out = os.path.join(t, "r")
            RV._quiet(RV.main, ["--rules", RULES, "--config", cfg, "--alertmanager", am, "--promtool-version", "none", "--out", out])
            s = read(os.path.join(out, "summary.md"))
        top = s.split("## 建議先處理的 3 件事")[1].split("##")[0]
        self.assertNotIn("alertmanager.example.invalid", top)
        self.assertIn(PC.REPLACE_BEFORE, s)
        self.assertIn("dms.example.invalid", s)
        self.assertIn("alertmanager.example.invalid", s)


GROUP_RULES = """groups:
- name: etcd
  interval: {iv}
  rules:
  - alert: etcdNoLeader
    expr: etcd_server_has_leader{{job="kube-etcd"}} == 0
    for: 30s
    labels: {{severity: critical}}
"""


class IntervalTest(unittest.TestCase):
    def test_group_interval_named_as_E(self):
        with tempfile.TemporaryDirectory() as t:
            rules = write(t, "r.yml", GROUP_RULES.format(iv="15s"))
            RV._quiet(FC.main, ["--rules", rules, "--config", PROM, "--promtool-version", "none", "--out", t])
            fit = json.loads(read(os.path.join(t, "contracts-fit.json")))
        a = "\n".join(fit["assumptions"])
        self.assertIn("group `interval` 覆寫：etcd 15s", a)
        self.assertIn("global `evaluation_interval` 30s", a)
        # group E 15s < S 30s of the job it reads: flagged per group
        self.assertTrue(any("group etcd" in c["text"] and c["ok"] is False for c in fit["env_checks"]))

    def test_proposal_changes_one_not_other(self):
        with tempfile.TemporaryDirectory() as t:
            orig = write(t, "orig.yml", GROUP_RULES.format(iv="30s"))
            same = write(t, "same.yml", GROUP_RULES.format(iv="30s"))
            both = write(t, "both.yml", GROUP_RULES.format(iv="15s"))
            newcfg = write(t, "new.yml", PROPOSED_PROM)   # kube-etcd S 30s → 15s
            only_s = PC.check([same], newcfg, None, [orig], PROM)["intervals"]
            ok = PC.check([both], newcfg, None, [orig], PROM)["intervals"]
            only_e = PC.check([both], PROM, None, [orig], PROM)["intervals"]
        self.assertEqual(len(only_s), 1)
        self.assertIn("`scrape_interval` 改了", only_s[0]["fact"])
        self.assertIn("不一致", only_s[0]["fact"])
        self.assertEqual(ok, [])
        self.assertTrue(only_e and "E 改了" in only_e[0]["fact"])


NEW_RULES = """groups:
- name: etcd
  rules:
  - alert: etcdNoLeader
    expr: etcd_server_has_leader{job="kube-etcd"} == 0
    for: 1m
  - alert: etcdMembersDown
    expr: max without (endpoint) (sum without (instance) (up{job="kube-etcd"} == bool 0)) > 0
    for: 20m
"""
TESTS = """rule_files: [r.yml]
tests:
  - interval: 15s
    input_series: []
    alert_rule_test:
      - {eval_time: 1m, alertname: etcdMembersDown, exp_alerts: []}
"""


class NewRuleTest(unittest.TestCase):
    def test_new_rule_needs_tests_and_upstream_label(self):
        with tempfile.TemporaryDirectory() as t:
            orig = write(t, "orig.yml", GROUP_RULES.format(iv="30s"))
            new = write(t, "new.yml", NEW_RULES)
            tests = write(t, "t.test.yml", TESTS)
            got = PC.check([new], None, None, [orig], None, [tests])["new_rules"]
            self.assertEqual([e["rule"] for e in got], ["etcdMembersDown"])
            e = got[0]
            self.assertEqual(e["tests_missing"], ["firing"])
            self.assertTrue(e["upstream_unlabelled"])
            self.assertIn("etcd-mixin-membersdown", PC.new_rule_text(e))
            labelled = write(t, "lab.yml", NEW_RULES.replace("  - alert: etcdMembersDown",
                                                             f"  # {PC.REWRITTEN}\n  - alert: etcdMembersDown"))
            e2 = PC.check([labelled], None, None, [orig], None, [tests])["new_rules"][0]
            self.assertNotIn("upstream_unlabelled", e2)
            cited = write(t, "cit.yml", NEW_RULES.replace(
                "  - alert: etcdMembersDown",
                "  # https://github.com/etcd-io/etcd/blob/v3.7.3/contrib/mixin/alerts/alerts.libsonnet#L8\n  - alert: etcdMembersDown"))
            e3 = PC.check([cited], None, None, [orig], None, [tests])["new_rules"][0]
            self.assertNotIn("upstream_unlabelled", e3)

    def test_in_summary(self):
        with tempfile.TemporaryDirectory() as t:
            orig = write(t, "orig.yml", GROUP_RULES.format(iv="30s"))
            new = write(t, "new.yml", NEW_RULES)
            out = os.path.join(t, "r")
            RV._quiet(RV.main, ["--rules", new, "--config", PROM, "--original", orig, "--promtool-version", "none", "--out", out])
            s = read(os.path.join(out, "summary.md"))
        self.assertIn("新加的 `etcdMembersDown`", s)
        self.assertIn("沒有 promtool 測試", s)


class SkillGuidanceTest(unittest.TestCase):
    def test_rules_written(self):
        s = read(os.path.join(ROOT, "plugins/core/skills/rule-review/SKILL.md"))
        for want in ("deferred", "unknown: [", IU.ASSUMED_DEFAULT, "approved", "keep_range", "range selector",
                     PC.REPLACE_BEFORE, ".invalid", "group `interval`", "promtool", PC.REWRITTEN, "proposal_check.py"):
            self.assertIn(want, s, want)
        f = read(os.path.join(ROOT, "plugins/core/references/five-sections.md"))
        for want in (IU.ASSUMED_DEFAULT, PC.REWRITTEN, "unknown"):
            self.assertIn(want, f, want)


if __name__ == "__main__":
    unittest.main()
