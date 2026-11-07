"""Offline tests: python3 -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import build_pages  # noqa: E402
import catalog as C  # noqa: E402
import check_metrics  # noqa: E402
import validate  # noqa: E402


class CatalogTest(unittest.TestCase):
    def test_validate_has_no_errors(self):
        errors, _ = validate.validate()
        self.assertEqual(errors, [], "\n".join(errors))

    def test_every_tier1_signal_says_where_it_is_evaluated(self):
        for card in C.load_components():
            for s in card["signals"]:
                if s["tier"] == 1:
                    self.assertIn(s["evaluate_on"], C.EVALUATE_ON, f"{card['id']}#{s['id']}")

    def test_yes_no_are_normalised(self):
        for card in C.load_components():
            for s in card["signals"]:
                self.assertIsInstance(s["upstream"]["status"], str)

    def test_component_page_has_anchor_for_every_signal(self):
        sources, contracts = C.load_sources(), C.load_contracts()
        for card in C.load_components():
            page = build_pages.component_page(card, sources, contracts)
            for s in card["signals"]:
                self.assertIn("{#" + s["id"] + "}", page, f"{card['id']}#{s['id']}")

    def test_private_sources_have_no_link(self):
        sources = C.load_sources()
        for key, s in sources.items():
            if s.get("visibility") == "private":
                self.assertNotIn("](", build_pages.src(key, sources))


class LintTest(unittest.TestCase):
    def _rules(self, expr, **ann):
        return {"rules/x.yml#A": {"alert": "A", "expr": expr, "annotations": ann},
                "rules/x.yml#B": {"alert": "B", "expr": "up == 0", "annotations": {}}}

    def test_presence_flags_bare_selector(self):
        _, warnings = validate.check_presence(self._rules('mysql_up{job="mysql"} == 0'))
        self.assertEqual(len(warnings), 1)

    def test_presence_accepts_unless_absent_and_cover(self):
        for expr in ('up == 1 unless on (job) mysql_exporter_collector_success',
                     'mysql_up == 0 or absent(mysql_up)'):
            self.assertEqual(validate.check_presence(self._rules(expr)), ([], []))
        self.assertEqual(validate.check_presence(self._rules("mysql_up == 0", absence_covered_by="B")), ([], []))
        errors, _ = validate.check_presence(self._rules("mysql_up == 0", absence_covered_by="Nope"))
        self.assertEqual(len(errors), 1)

    def test_presence_ignores_functions_and_up(self):
        for expr in ("increase(x_total[15m]) == 0", "up == 0", "max_over_time(x[5m]) == 0", "x == 0.5"):
            self.assertEqual(validate.check_presence(self._rules(expr)), ([], []), expr)

    def test_target_pattern_requires_signals(self):
        card = {"_path": "t.yaml", "kind": "target", "signals": [{"id": "exporter-up", "tier": 1}],
                "not_applicable": {"probe-blast-radius": "單一 target"}}
        errors = validate.check_target_pattern([card], ROOT)
        missing = {e.split("缺少 target 標準訊號 ")[1].split("（")[0] for e in errors if "缺少" in e}
        self.assertEqual(missing, {"db-up", "collectors-present", "collector-success", "scrape-budget"})

    def test_duplicate_keys_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("a: 1\na: 2\n")
        try:
            with self.assertRaises(ValueError):
                C._load(f.name)
        finally:
            os.unlink(f.name)


class CheckMetricsTest(unittest.TestCase):
    SAMPLE = "\n".join([
        "# HELP prometheus_rule_group_last_duration_seconds x",
        'prometheus_rule_group_last_duration_seconds{rule_group="a"} 0.1',
        "prometheus_engine_query_duration_seconds_sum 1",
        "prometheus_notifications_errors_total 0",
    ])

    def test_suffixes_and_external(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(self.SAMPLE)
        try:
            names = check_metrics.exposed_names(f.name)
        finally:
            os.unlink(f.name)
        self.assertTrue(check_metrics.is_exposed("prometheus_engine_query_duration_seconds", names))
        self.assertTrue(check_metrics.is_exposed("prometheus_notifications_errors_total", names))
        self.assertFalse(check_metrics.is_exposed("prometheus_tsdb_wal_writes_failed_total", names))
        card = {"id": "x", "signals": [{"id": "s", "tier": 1, "metrics": ["up", "prometheus_tsdb_wal_writes_failed_total"]}]}
        status = {r["metric"]: r["status"] for r in check_metrics.check(card, names)}
        self.assertEqual(status, {"up": "external", "prometheus_tsdb_wal_writes_failed_total": "missing"})


if __name__ == "__main__":
    unittest.main()
