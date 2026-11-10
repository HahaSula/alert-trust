"""Presence lint on arbitrary rule files: python3 tools/validate.py --lint-rules FILE..."""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import validate as V  # noqa: E402

FX = os.path.join(ROOT, "tests", "fixtures", "lint", "rules.yml")


class LintRulesTest(unittest.TestCase):
    def test_findings(self):
        errors, advice = V.lint_rules(V.load_rule_files([FX]))
        self.assertTrue(any("#DbDown:" in w and "absent" in w for w in advice))
        self.assertFalse(any("#DbDownCovered:" in w for w in advice))
        self.assertTrue(any("NoSuchAlert" in e for e in errors))
        self.assertTrue(any(w.startswith("job other") and "ThreadsHigh" in w for w in advice))   # no up rule for it
        self.assertFalse(any(w.startswith("job mysqld") for w in advice))                       # ExporterDown guards it

    def test_cli(self):
        p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "validate.py"), "--lint-rules", FX],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, p.stderr)            # broken absence_covered_by reference
        self.assertIn("建議", p.stdout)
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
