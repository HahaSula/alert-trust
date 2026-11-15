import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import collect as CO  # noqa: E402
import fit_contracts as FC  # noqa: E402


class PeakShapeTest(unittest.TestCase):
    def test_queries(self):
        q = FC.peak_shape_queries("mysqld", "10s")
        self.assertEqual(q["hourly_peak_p50"],
                         'max by (job) (quantile_over_time(0.5, (max_over_time(scrape_duration_seconds{job=~"mysqld"}[1h]))[7d:1h]))')
        self.assertEqual(q["peak_hours"],
                         'max by (job) (count_over_time((max_over_time(scrape_duration_seconds{job=~"mysqld"}[1h]) > 8)[7d:1h]))')
        self.assertNotIn("peak_hours", FC.peak_shape_queries("mysqld"))       # no threshold without tau

    @unittest.skipUnless(subprocess.run(["which", "promtool"], capture_output=True).returncode == 0, "promtool not installed")
    def test_queries_parse(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.yml")
            with open(p, "w", encoding="utf-8") as f:
                f.write("groups:\n  - name: g\n    rules:\n")
                for i, q in enumerate([*FC.peak_shape_queries("a|b", "10s").values(),
                                       CO.prom_peaks("7d")["scrape_duration_hourly_peak_p50"]]):
                    f.write(f"      - record: r{i}\n        expr: '{q}'\n")
            r = subprocess.run(["promtool", "check", "rules", p], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

    def test_reading(self):
        self.assertEqual(FC.peak_shape("9.8s", "1.2s")["kind"], "rare")
        self.assertEqual(FC.peak_shape("9.8s", "6s")["kind"], "frequent")
        s = FC.peak_shape("9.8s", "9s", 150)
        self.assertEqual((s["kind"], s["peak_hours"]), ("sustained", 150))
        self.assertIn("168 個小時裡有 150 個小時", s["text"])
        self.assertIsNone(FC.peak_shape(None, "1s"))
        self.assertIsNone(FC.peak_shape("9.8s", None))

    def test_collect_has_hourly_peak(self):
        for peaks in (CO.prom_peaks("7d"), CO.vm_peaks("7d")):
            self.assertIn("scrape_duration_hourly_peak_p50", peaks)


if __name__ == "__main__":
    unittest.main()
