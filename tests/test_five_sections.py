import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import catalog as C  # noqa: E402

REFS = {"r1": {}}


class FiveSectionsTest(unittest.TestCase):
    def card(self, **kw):
        c = {"_path": "t.yaml", "id": "t", "signals": [{"id": "a", "section": "observe"}, {"id": "b"}]}
        c.update(kw)
        return c

    def test_ok_and_missing_count(self):
        errors, missing = C.check_five_sections(self.card(usage=[
            {"id": "exporter-account", "text": "x", "evidence": "code", "refs": ["r1"]}]), REFS)
        self.assertEqual(errors, [])
        self.assertEqual(missing, 1)

    def test_usage_rules(self):
        errors, _ = C.check_five_sections(self.card(usage=[
            {"id": "Bad", "text": "", "evidence": "inferred", "refs": ["nope"]},
            {"id": "dup", "text": "x", "evidence": "doc", "refs": []},
            {"id": "dup", "text": "x", "evidence": "tested", "refs": ["r1"]}]), REFS)
        text = "\n".join(errors)
        for frag in ("kebab-case", "缺少 text", "evidence 只能是", "refs 'nope'", "要有 refs", "重複"):
            self.assertIn(frag, text)

    def test_section_value_and_options(self):
        errors, _ = C.check_five_sections(self.card(
            signals=[{"id": "a", "section": "health"}],
            blind_spots=[{"options": "一\n二"}, {"options": "x" * 200}, {"options": "一句話的方案"}]), REFS)
        self.assertEqual(sum("section 要是" in e for e in errors), 1)
        self.assertEqual(sum("options" in e for e in errors), 2)


if __name__ == "__main__":
    unittest.main()
