import copy
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import catalog as C  # noqa: E402
import check_trace as T  # noqa: E402

HYP = {"id": "s1-ok", "claim": "c", "versions": ["MariaDB 11.4.13"], "method": "m",
       "expect": {"appear": ["x 上升"]}, "result": "pass"}
REFS = {
    "exp-a": {"kind": "experiment", "hypotheses": [
        HYP, dict(HYP, id="s2-failed", result="fail"), dict(HYP, id="s3-mysql", versions=["MySQL 8.4.7"]),
        dict(HYP, id="s4-old", versions=["MariaDB 10.6.28"])]},
    "exp-plain": {"kind": "experiment"},
    "code-1": {"kind": "code"},
}


def card(tested_by, **item):
    sig = {"id": "sig", "evidence": "tested", "refs": ["exp-a", "code-1"], "tested_by": tested_by}
    sig.update(item)
    return {"_path": "t.yaml", "id": "t", "versions_checked": ["mysqld_exporter 0.21.0", "MariaDB 10.6.28",
                                                               "MariaDB 11.4.13（lab 與原始碼參考）"],
            "signals": [sig, {"id": "plain", "evidence": "tested", "refs": ["exp-plain"]},
                        {"id": "code", "evidence": "code", "refs": ["code-1"]}]}


def errs(c):
    return "\n".join(C.check_tested_by(c, REFS))


class HypothesisShapeTest(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(C.check_hypotheses(REFS), [])

    def test_bad_shapes(self):
        refs = copy.deepcopy(REFS)
        refs["code-1"]["hypotheses"] = [HYP]
        refs["exp-plain"]["hypotheses"] = []
        refs["exp-a"]["hypotheses"] += [
            dict(HYP, id="s1-ok"), {"id": "Bad Id", "versions": ["latest"], "expect": {"seen": ["x"]}, "result": "ok",
                                    "limits": "", "note": "x"},
            dict(HYP, id="s9", expect={}, versions=[])]
        text = "\n".join(C.check_hypotheses(refs))
        for frag in ("只有 kind: experiment", "hypotheses 要是非空清單", "id 's1-ok' 重複", "id 要是 kebab-case", "缺少 claim",
                     "缺少 method", "versions 要是非空的版本字串清單", "expect 要有 appear 或 disappear", "result 只能是",
                     "limits 要是非空字串", "不認得 'note'"):
            self.assertIn(frag, text)


class TestedByTest(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(errs(card(["exp-a#s1-ok"])), "")
        self.assertEqual(errs(card(["exp-a#s3-mysql", "exp-a#s1-ok"])), "")   # one overlapping version is enough
        self.assertEqual(errs(card(["exp-a#s1-ok"], versions={"min": "11.0"})), "")

    def test_e1(self):
        self.assertIn("結果是 fail，不是 pass", errs(card(["exp-a#s2-failed"])))
        self.assertIn("沒有這個假設", errs(card(["exp-a#nope"])))
        self.assertIn("沒有這個假設", errs(card(["exp-plain#s1-ok"], refs=["exp-plain"])))
        self.assertIn("不是實驗參考", errs(card(["code-1#s1-ok"])))
        self.assertIn("不是實驗參考", errs(card(["missing#s1-ok"])))
        self.assertIn("要同時列在 refs", errs(card(["exp-a#s1-ok"], refs=["code-1"])))
        self.assertIn("evidence 不是 tested", errs(card(["exp-a#s1-ok"], evidence="code")))
        for bad in ("exp-a#s1-ok", [], ["exp-a"], ["a#b#c"], [1]):
            self.assertIn("非空清單", errs(card(bad)), bad)

    def test_e2_versions_overlap(self):
        self.assertIn("不在這張卡的 versions_checked（E2）", errs(card(["exp-a#s3-mysql"])))
        self.assertIn("不在這個項目的版本範圍 ≥ 11.8（E2）", errs(card(["exp-a#s1-ok"], versions={"min": "11.8"})))
        self.assertIn("版本範圍 < 11.0", errs(card(["exp-a#s1-ok"], versions={"max": "11.0"})))
        self.assertEqual(errs(card(["exp-a#s4-old"], versions={"max": "11.0"})), "")
        c = card(["exp-a#s3-mysql"])
        del c["versions_checked"]                       # nothing to compare against
        self.assertEqual(errs(c), "")

    def test_nested_items_are_checked(self):
        c = card(["exp-a#s1-ok"])
        c["failure_modes"] = [{"id": "fm", "evidence": "tested", "refs": ["exp-a"], "tested_by": ["exp-a#s2-failed"]}]
        self.assertIn("failure_modes[fm]", errs(c))


class G7Test(unittest.TestCase):
    def test_counts_tested_items_without_hypothesis(self):
        t = T.trace_card(card(["exp-a#s1-ok"]), {}, {})
        self.assertEqual((t["tested"], T.counts(t)["G7"], t["p2"]), (2, 1, True))
        self.assertEqual(t["G7"], [{"item": "t.yaml.signals[plain]"}])
        text = T.render([dict(t, path="t.yaml")])
        self.assertIn("實測項目 2 個，其中 1 個指到假設", text)
        self.assertIn("  G7 t: t.yaml.signals[plain]", text)
        c = card(["exp-a#s1-ok"])
        del c["signals"][0]["tested_by"]
        t = T.trace_card(c, {}, {})
        self.assertEqual((T.counts(t)["G7"], t["p2"]), (2, False))
        self.assertNotIn("  G7 ", T.render([dict(t, path="t.yaml")]))   # counted, not listed, before a card starts P2
        self.assertIn("G7", T.RATCHET)


class RealCatalogTest(unittest.TestCase):
    def test_real(self):
        refs = C.load_refs()
        self.assertEqual(C.check_hypotheses(refs), [])
        cards = C.load_components()
        for c in cards:
            self.assertEqual(C.check_tested_by(c, refs), [], c["id"])
        mdb = next(c for c in cards if c["id"] == "mariadb")
        linked = [x for _, x in C.evidence_items(mdb) if x.get("tested_by")]
        self.assertGreaterEqual(len(linked), 11)
        # every tested failure mode of the pilot card names its hypothesis
        self.assertEqual([f["id"] for f in mdb["failure_modes"] if f["evidence"] == "tested" and not f.get("tested_by")], [])


if __name__ == "__main__":
    unittest.main()
