"""docs/tools/templates/for-reset.*: the promtool pair for 「a scrape failure resets `for`」 must pass and use `stale`."""
import os
import shutil
import subprocess
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(ROOT, "docs", "tools", "templates")
TEST = os.path.join(TPL, "for-reset.test.yml")


class ForResetTemplateTest(unittest.TestCase):
    def test_simulates_failure_with_stale(self):
        with open(TEST, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        values = [s["values"] for t in doc["tests"] for s in t["input_series"]]
        self.assertTrue(any("stale" in v.split() for v in values))
        # the pair: the original rule is reset under stale, the proposed one is not
        names = {(t["input_series"][0]["values"], c["alertname"], bool(c["exp_alerts"]))
                 for t in doc["tests"] for c in t["alert_rule_test"] if c["eval_time"] == "90s"}
        stale = next(v for v in values if "stale" in v.split())
        self.assertIn((stale, "DemoHighOriginal", False), names)
        self.assertIn((stale, "DemoHighProposed", True), names)

    @unittest.skipUnless(shutil.which("promtool"), "promtool not installed")
    def test_promtool_passes(self):
        out = subprocess.run(["promtool", "test", "rules", os.path.basename(TEST)], cwd=TPL, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
