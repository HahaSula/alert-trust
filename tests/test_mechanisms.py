import copy
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import catalog as C  # noqa: E402
import check_trace as T  # noqa: E402

REFS = {"r1": {"kind": "code"}}
CARD = {
    "_path": "t.yaml", "id": "t",
    "signals": [{"id": "sig-a", "section": "misuse", "tier": 2}, {"id": "sig-b", "section": "misuse", "tier": 3},
                {"id": "sig-o", "section": "observe", "tier": 1}],
    "blind_spots": [{"id": "blind-opt", "what": "x", "category": "exporter", "options": "建議：…"},
                    {"id": "blind-plain", "what": "y", "category": "exporter"}, {"what": "沒有 id 的舊寫法"}],
    "mechanisms": [
        {"id": "purge", "name": "Purge", "resource": "undo", "text": "t", "evidence": "code", "refs": ["r1"]},
        {"id": "redo", "name": "Redo", "resource": "log", "text": "t", "evidence": "inferred",
         "not_studied": "還沒讀 checkpoint"}],
    "failure_modes": [
        {"id": "by-signal", "mechanism": "purge", "trigger": "a", "effect": "b", "evidence": "inferred",
         "detected_by": {"signals": ["sig-a"]}, "distinguishes": "partial", "sources": ["MDEV-1"]},
        {"id": "by-blind", "mechanism": "purge", "trigger": "a", "effect": "b", "evidence": "code", "refs": ["r1"],
         "detected_by": {"blind_spots": ["blind-opt"]}, "versions": {"max": "10.6.19"}},
        {"id": "by-plain-blind", "mechanism": "purge", "trigger": "a", "effect": "b", "evidence": "inferred",
         "detected_by": {"blind_spots": ["blind-plain"]}},
        {"id": "accepted", "mechanism": "purge", "trigger": "a", "effect": "b", "evidence": "inferred",
         "detected_by": {"accepted": "只影響已 EOL 的版本"}}],
}


def card(**change):
    c = copy.deepcopy(CARD)
    c.update(change)
    return c


def errors_of(c):
    return "\n".join(C.check_mechanisms(c, REFS))


class GatesTest(unittest.TestCase):
    def test_valid_card_and_card_without_p1(self):
        self.assertEqual(C.check_mechanisms(card(), REFS), [])
        self.assertEqual(C.check_mechanisms({"_path": "t.yaml", "signals": [], "blind_spots": [{"what": "x"}]}, REFS), [])

    def test_t1_dangling_ids(self):
        c = card()
        c["failure_modes"][0]["mechanism"] = "nope"
        c["failure_modes"][0]["detected_by"] = {"signals": ["sig-x"], "blind_spots": ["沒有 id 的舊寫法"]}
        c["failure_modes"][1]["refs"] = ["r-missing"]
        text = errors_of(c)
        for frag in ("mechanism 'nope'", "'sig-x' 不在這張卡", "不是這張卡 blind_spots 的 id", "refs 'r-missing' 不存在"):
            self.assertIn(frag, text)

    def test_t2_every_failure_mode_is_accounted_for(self):
        for d in (None, {}, {"signals": []}, {"accepted": "  "}):
            c = card()
            c["failure_modes"][0]["detected_by"] = d
            self.assertTrue("T2" in errors_of(c) or "accepted 要寫明" in errors_of(c), d)
        c = card()
        c["failure_modes"][0]["detected_by"] = {"signals": ["sig-a"], "rules": ["x"]}
        self.assertIn("不認得 'rules'", errors_of(c))

    def test_t3_mechanism_needs_failure_mode_or_not_studied(self):
        c = card()
        del c["mechanisms"][1]["not_studied"]
        self.assertIn("mechanisms[redo] 沒有任何 failure mode，也沒有 not_studied（T3）", errors_of(c))
        c = card()
        c["mechanisms"][0]["not_studied"] = "x"
        self.assertIn("拿掉 not_studied", errors_of(c))

    def test_shape(self):
        c = card()
        c["mechanisms"].append({"id": "Purge 2", "evidence": "doc"})
        c["failure_modes"].append(dict(c["failure_modes"][0], trigger="", distinguishes="maybe", sources="MDEV-1",
                                       versions={"max": 10}))
        c["failure_modes"][1]["refs"] = []          # code evidence without refs
        c["blind_spots"].append({"id": "blind-opt", "what": "dup"})
        text = errors_of(c)
        for frag in ("mechanisms[2] id 要是 kebab-case", "缺少 name", "evidence 只能是", "id 'by-signal' 重複",
                     "缺少 trigger", "distinguishes 只能是", "sources 要是字串清單", "versions.max",
                     "要有 refs（只有 evidence: inferred 可以沒有）", "id 'blind-opt' 重複"):
            self.assertIn(frag, text)

    def test_real_catalog(self):
        refs = C.load_refs()
        cards = C.load_components()
        for c in cards:
            self.assertEqual(C.check_mechanisms(c, refs), [], c["id"])
        mdb = next(c for c in cards if c["id"] == "mariadb")
        self.assertTrue(mdb["mechanisms"] and mdb["failure_modes"])
        # a claim taken from an issue only (sources, no refs) must stay inferred
        for f in mdb["failure_modes"]:
            if f.get("sources") and not f.get("refs"):
                self.assertEqual(f["evidence"], "inferred", f["id"])


class GapsTest(unittest.TestCase):
    def test_g1_g2(self):
        t = T.trace_card(card(), {}, {})
        self.assertEqual([g["signal"] for g in t["G1"]], ["sig-b"])      # sig-o is observe, sig-a is pointed at
        self.assertEqual([(g["failure_mode"], g["by"]) for g in t["G2"]],
                         [("by-plain-blind", "blind_spots"), ("accepted", "accepted")])
        self.assertEqual((t["mechanisms"], t["failure_modes"]), (2, 4))
        self.assertEqual((T.counts(t)["G1"], T.counts(t)["G2"]), (1, 2))

    def test_cards_without_p1_count_zero(self):
        t = T.trace_card({"id": "t", "signals": CARD["signals"]}, {}, {})
        self.assertFalse(t["p1"])
        self.assertEqual((t["G1"], t["G2"]), ([], []))

    def test_listed_and_ratcheted(self):
        self.assertIn("G1", T.RATCHET)
        self.assertIn("G2", T.RATCHET)
        text = T.render([dict(T.trace_card(card(), {}, {}), path="t.yaml")])
        self.assertIn("  G1 t#sig-b tier 3", text)
        self.assertIn("  G2 t: accepted（機制 purge，accepted）", text)


if __name__ == "__main__":
    unittest.main()
