"""Gaps found by blind run 7: card defaults for must-ask facts reach the conclusions (prefix effects), the
external-observer question is not left to the last round, and a replica label that is not dropped from alerts."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import catalog as C  # noqa: E402
import inventory as I  # noqa: E402
import inventory_use as IU  # noqa: E402

AM = """route: {receiver: chat}
receivers: [{name: chat, webhook_configs: [{url: "http://chat.example.invalid/hook"}]}]
"""
RULES = """groups:
  - name: g
    rules:
      - alert: SsRule
        expr: mongodb_ss_uptime{job="mongodb"} < 300
        labels: {severity: critical}
      - alert: RsRule
        expr: mongodb_rs_members_health{job="mongodb"} == 0
        labels: {severity: critical}
      - alert: UpRule
        expr: mongodb_up{job="mongodb"} == 0
        labels: {severity: critical}
"""


def config(external: str = "", relabel: str = "") -> str:
    return f"""global: {{scrape_interval: 30s, evaluation_interval: 30s{external}}}
rule_files: [rules.yml]
alerting:
  alertmanagers: [{{static_configs: [{{targets: ["am:9093"]}}]}}]{relabel}
scrape_configs:
  - job_name: mongodb
    static_configs: [{{targets: ["mongo-1:9216"]}}]
"""


def build(cfg: str, answers: dict | None = None, rules: str = RULES, am: str | None = None):
    import contextlib
    import io
    import yaml
    d = tempfile.mkdtemp()
    paths = {}
    for name, text in (("prometheus.yml", cfg), ("rules.yml", rules), ("alertmanager.yml", am or AM)):
        paths[name] = os.path.join(d, name)
        with open(paths[name], "w", encoding="utf-8") as f:
            f.write(text)
    out = os.path.join(d, "inv.yaml")
    argv = ["--rules", paths["rules.yml"], "--config", paths["prometheus.yml"], "--alertmanager", paths["alertmanager.yml"],
            "--out", out]
    if answers:
        a = os.path.join(d, "answers.yaml")
        with open(a, "w", encoding="utf-8") as f:
            yaml.safe_dump(answers, f, allow_unicode=True)
        argv += ["--answers", a]
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        I.main(argv)
    with open(out, encoding="utf-8") as f:
        return yaml.safe_load(f)


class PrefixEffectsTest(unittest.TestCase):
    def test_prefix_and_exact(self):
        e = [{"metrics": ["mongodb_ss_*", "exact_total"], "verdict": "no_data"}]
        self.assertTrue(IU.effect_for_metric("mongodb_ss_uptime", e))
        self.assertTrue(IU.effect_for_metric("exact", e))            # same counter family
        self.assertIsNone(IU.effect_for_metric("mongodb_rs_members_health", e))
        self.assertIsNone(IU.effect_for_metric("mongodb_up", e))

    def test_cards_with_prefix_effects_validate(self):
        refs = C.load_refs()
        for c in C.load_components():
            if c["id"] in ("mongodb_exporter", "clickhouse"):
                self.assertEqual(C.check_must_ask(c, refs), [], c["id"])
                with_effects = [q for q in c["must_ask"] if q.get("effects")]
                self.assertGreaterEqual(len(with_effects), 2, c["id"])
                self.assertTrue(all(q.get("default") is not None for q in with_effects), c["id"])


class MongoDefaultsTest(unittest.TestCase):
    def test_default_marks_rules_without_data_until_answered(self):
        inv = build(config())
        fires = {r["name"]: r.get("fires") or {} for r in inv["rules"]}
        self.assertEqual(fires["SsRule"]["value"], "never")
        self.assertTrue(fires["SsRule"].get("assumed"))
        self.assertEqual(fires["RsRule"]["value"], "never")
        self.assertNotEqual(fires["UpRule"].get("value"), "never")
        top = [q["id"] for q in inv["questions"] if q.get("ask_now")]
        self.assertIn("must_ask.mongodb_exporter.diagnosticdata", top)

    def test_answer_overrides_default(self):
        inv = build(config(), {"facts": {"mongodb_exporter": {"diagnosticdata": "有", "replicasetstatus": "沒有"}}})
        fires = {r["name"]: r.get("fires") or {} for r in inv["rules"]}
        self.assertNotEqual(fires["SsRule"].get("value"), "never")
        self.assertEqual(fires["RsRule"]["value"], "never")
        self.assertFalse(fires["RsRule"].get("assumed"))


class ReplicaLabelTest(unittest.TestCase):
    def kinds(self, inv):
        return [f["kind"] for f in inv["findings_from_files"]]

    def test_replica_label_without_alert_relabel(self):
        inv = build(config(external=", external_labels: {cluster: a, replica: prom-a}"))
        self.assertIn("replica_label_on_alerts", self.kinds(inv))
        f = next(x for x in inv["findings_from_files"] if x["kind"] == "replica_label_on_alerts")
        self.assertEqual(f["labels"], ["replica"])
        self.assertIn("alert_relabel_configs", f["fact"])

    def test_no_finding_when_dropped_or_absent(self):
        drop = "\n  alert_relabel_configs: [{action: labeldrop, regex: replica}]"
        self.assertNotIn("replica_label_on_alerts",
                         self.kinds(build(config(external=", external_labels: {replica: prom-a}", relabel=drop))))
        self.assertNotIn("replica_label_on_alerts", self.kinds(build(config(external=", external_labels: {cluster: a}"))))
        self.assertNotIn("replica_label_on_alerts", self.kinds(build(config())))


class ObserverQuestionTest(unittest.TestCase):
    def test_mandatory_from_round_two(self):
        inv = build(config())
        q0 = next(q for q in inv["questions"] if q["id"] == "external_observer")
        self.assertFalse(q0.get("mandatory"))
        with open(os.path.join(ROOT, "tools", "inventory.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"answer_key": "external_observer",\n                   **({"mandatory": True', src)
        self.assertIn('if int(inv.get("round") or 0) >= 2 else {})})', src)   # version keeps round 1 (test_run6_gaps)


AM_MUTED = """route:
  receiver: chat
  routes:
    - matchers: ['severity="critical"']
      receiver: pager
      mute_time_intervals: [quiet-hours]
    - matchers: ['severity="warning"']
      receiver: chat
      mute_time_intervals: [quiet-hours]
time_intervals:
  - name: quiet-hours
    time_intervals: [{weekdays: ["saturday", "sunday"]}]
receivers:
  - {name: chat, webhook_configs: [{url: "http://chat.example.invalid/hook"}]}
  - {name: pager, webhook_configs: [{url: "http://pager.example.invalid/hook"}]}
"""


class MutedCriticalRouteTest(unittest.TestCase):
    def test_critical_route_with_mute_interval(self):
        inv = build(config(), am=AM_MUTED)
        f = [x for x in inv["findings_from_files"] if x["kind"] == "critical_route_muted"]
        self.assertEqual(len(f), 1)                      # the warning route is muted too, but that is not a finding
        self.assertEqual(f[0]["receiver"], "pager")
        self.assertEqual(f[0]["intervals"], ["quiet-hours"])
        self.assertEqual(f[0]["rules"], ["RsRule", "SsRule", "UpRule"])
        self.assertIn("`continue` 不會讓被靜音的通知改送別處", f[0]["fact"])

    def test_no_finding_without_intervals(self):
        inv = build(config())
        self.assertFalse([x for x in inv["findings_from_files"] if x["kind"] == "critical_route_muted"])


WRONG_JOB = RULES.replace('job="mongodb"', 'job="mongo"')


class JobNameQuestionTest(unittest.TestCase):
    def top(self, inv):
        return [q["id"] for q in inv["questions"] if q.get("ask_now")]

    def test_not_in_first_three_when_config_shows_the_names(self):
        inv = build(config(), rules=WRONG_JOB)
        q = next(q for q in inv["questions"] if q["id"] == "job_names")
        self.assertEqual(q["cost"], 1)
        self.assertIn("先假設 config 的名稱是對的", q["q"])
        self.assertNotIn("job_names", self.top(inv))

    def test_still_asked_first_when_job_is_rewritten(self):
        cfg = config().replace('static_configs: [{targets: ["mongo-1:9216"]}]',
                               'static_configs: [{targets: ["mongo-1:9216"]}]\n'
                               '    relabel_configs: [{source_labels: [__address__], target_label: job}]')
        inv = build(cfg, rules=WRONG_JOB)
        q = next(q for q in inv["questions"] if q["id"] == "job_names")
        self.assertEqual(q["cost"], 3)


class NewMustAskTest(unittest.TestCase):
    def test_cards_ask_for_second_path_and_exporter_topology(self):
        cards = {c["id"]: c for c in C.load_components()}
        self.assertIn("readyz-probe", [q["id"] for q in cards["etcd"]["must_ask"]])
        self.assertIn("exporter-topology", [q["id"] for q in cards["mongodb_exporter"]["must_ask"]])
        self.assertIn("mute-intervals-critical", [u["id"] for u in cards["alertmanager"]["usage"]])


if __name__ == "__main__":
    unittest.main()
