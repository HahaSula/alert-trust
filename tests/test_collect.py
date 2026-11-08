"""Offline tests for tools/collect.py: canned responses served from 127.0.0.1, no real backends."""
import contextlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import collect  # noqa: E402

PROM_RULES = {"status": "success", "data": {"groups": [{
    "name": "meta", "file": "meta.yml", "interval": 30,
    "rules": [
        {"type": "alerting", "name": "PrometheusDown", "query": 'up{job="prometheus"} == 0', "duration": 60},
        {"type": "alerting", "name": "Slow", "query": 'rate(http_requests_total{code=~"5.."}[5m] offset 1m) > 0.1'},
        {"type": "recording", "name": "job:x", "query": "sum by (job) (ignored_recording_metric)"},
    ]}]}}
VMALERT_RULES = {"status": "success", "data": {"groups": [
    {"name": "vm", "type": "prometheus", "interval": 30,
     "rules": [{"type": "alerting", "name": "VMReadOnly", "query": "max(vm_storage_is_read_only) == 1"}]},
    {"name": "logs", "type": "vlogs", "interval": 60,
     "rules": [{"type": "alerting", "name": "NoLogs", "query": "_time:2m flow:canary | stats count() c"}]},
]}}
VM_FLAGS = '-rule.resendDelay="3m"\n-datasource.url="http://vm:8428"\n-evaluationInterval="30s"\n'
VMALERT_METRICS = (
    '# HELP vm_app_version\n'
    'vm_app_version{version="vmalert-20251201-v1.155.0",short_version="v1.155.0"} 1\n'
    'flag{name="rule.resendDelay", value="3m", is_set="true"} 1\n'
    'flag{name="eval_delay", value="30s", is_set="false"} 1\n'
    'flags_unrelated_total 3\n'
)
AM_STATUS = {"cluster": {"status": "ready", "peers": [{"name": "a"}]},
             "versionInfo": {"version": "0.27.0"}, "config": {"original": "route:\n  receiver: x\n"}}


def make_handler(routes, seen):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append(("GET", self.path, self.headers.get("Authorization")))
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/v1/query":
                body = json.dumps({"status": "success", "data": {"resultType": "vector", "result": [
                    {"metric": {"job": "prometheus"}, "value": [0, "1"]}]}})
                ctype = "application/json"
            elif path in routes:
                v = routes[path]
                body = v if isinstance(v, str) else json.dumps(v)
                ctype = "text/plain" if isinstance(v, str) else "application/json"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body.encode())

        def do_POST(self):  # noqa: N802 - must never be called
            seen.append(("POST", self.path, None))
            self.send_response(405)
            self.end_headers()

        def log_message(self, *a):
            pass
    return H


@contextlib.contextmanager
def serve(routes):
    seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(routes, seen))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    finally:
        srv.shutdown()
        srv.server_close()


def load(d, *parts):
    with open(os.path.join(d, *parts), encoding="utf-8") as f:
        return json.load(f)


class ParseTest(unittest.TestCase):
    def test_backend_spec(self):
        self.assertEqual(collect.parse_backend("prom-a=http://h:9090/"), ("prom-a", "http://h:9090"))
        self.assertEqual(collect.parse_backend("http://h:9090"), ("h-9090", "http://h:9090"))
        self.assertEqual(collect.parse_backend("http://h/x?a=b")[0], "h")
        with self.assertRaises(ValueError):
            collect.parse_backend("vm=localhost:8428")

    def test_safe_url_drops_credentials_and_query(self):
        self.assertEqual(collect.safe_url("https://u:secret@h:8481/select/0/prometheus?token=x"),
                         "https://h:8481/select/0/prometheus")
        self.assertEqual(collect.root_url("http://h:8481/select/0/prometheus"), "http://h:8481")

    def test_flags_text_and_metric(self):
        self.assertEqual(collect.parse_flags_text(VM_FLAGS)["rule.resendDelay"], "3m")
        values, set_ = collect.flags_from_metrics(VMALERT_METRICS)
        self.assertEqual(values, {"rule.resendDelay": "3m", "eval_delay": "30s"})
        self.assertEqual(set_, ["rule.resendDelay"])
        self.assertEqual(collect.version_from_metrics(VMALERT_METRICS), "v1.155.0")
        self.assertEqual(collect.version_from_metrics('alertmanager_build_info{version="0.27.0"} 1\n'), "0.27.0")

    def test_alert_metrics_skip_recording_and_logs(self):
        self.assertEqual(collect.alert_metrics(PROM_RULES), {"up", "http_requests_total"})
        self.assertEqual(collect.alert_metrics(VMALERT_RULES), {"vm_storage_is_read_only"})


class CollectTest(unittest.TestCase):
    def run_collect(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = collect.main(argv)
        return rc, out.getvalue()

    def test_snapshot_layout_for_all_backends(self):
        prom_routes = {"/api/v1/status/buildinfo": {"status": "success", "data": {"version": "3.13.5"}},
                       "/api/v1/status/flags": {"status": "success", "data": {"rules.alert.resend-delay": "1m"}},
                       "/api/v1/rules": PROM_RULES, "/metrics": "prometheus_tsdb_head_series 10\n"}
        vmalert_routes = {"/api/v1/rules": VMALERT_RULES, "/api/v1/alerts": {"status": "success", "data": {"alerts": []}},
                          "/flags": VM_FLAGS, "/metrics": VMALERT_METRICS}
        vm_routes = {"/flags": '-retentionPeriod="1"\n', "/metrics": 'vm_app_version{short_version="v1.155.0"} 1\n'}
        am_routes = {"/api/v2/status": AM_STATUS, "/api/v2/alerts": [], "/api/v2/silences": [], "/metrics": ""}
        with serve(prom_routes) as (p, ps), serve(vmalert_routes) as (va, vas), \
                serve(vm_routes) as (vm, vms), serve(am_routes) as (am, ams), tempfile.TemporaryDirectory() as d:
            os.environ["AT_TOKEN_PROM_A"] = "t0ken"
            try:
                rc, _ = self.run_collect(["--out", d, "--range", "1h", "--prom", f"prom-a={p}",
                                          "--vmalert", f"vmalert-biz={va}", "--vm", f"vm={vm}/select/0/prometheus",
                                          "--am", am])
            finally:
                del os.environ["AT_TOKEN_PROM_A"]
            self.assertEqual(rc, 0)
            manifest = load(d, "manifest.json")
            ids = {c["id"]: c for c in manifest["components"]}
            am_id = "127.0.0.1-" + am.rsplit(":", 1)[1]
            self.assertEqual(set(ids), {"prom-a", "vmalert-biz", "vm", am_id})

            # The interface tools/check_coverage.py relies on: raw rules.json per ruler, buildinfo.json.
            self.assertEqual(load(d, "prom-a", "rules.json"), PROM_RULES)
            self.assertEqual(load(d, "vmalert-biz", "rules.json"), VMALERT_RULES)
            self.assertEqual(load(d, "prom-a", "buildinfo.json")["data"]["version"], "3.13.5")
            self.assertEqual(load(d, "vmalert-biz", "buildinfo.json")["data"]["version"], "v1.155.0")
            self.assertEqual(load(d, "vm", "buildinfo.json")["data"]["version"], "v1.155.0")
            self.assertEqual(load(d, am_id, "buildinfo.json")["data"]["version"], "0.27.0")

            flags = load(d, "vmalert-biz", "flags.json")
            self.assertEqual(flags["data"]["rule.resendDelay"], "3m")
            self.assertEqual(flags["data"]["eval_delay"], "30s")  # only in the flag metric
            self.assertEqual(flags["set"], ["rule.resendDelay"])

            peaks = load(d, "prom-a", "peaks.json")
            self.assertIn("scrape_duration_peak", peaks["results"])
            self.assertIn("[1h]", peaks["exprs"]["scrape_duration_peak"])
            self.assertEqual(set(load(d, "prom-a", "metric_jobs.json")), {"up", "http_requests_total"})
            self.assertEqual(set(load(d, "vm", "metric_jobs.json")), {"vm_storage_is_read_only"})
            self.assertIn("partial_results", load(d, "vm", "peaks.json")["results"])

            # Missing endpoints are recorded, not fatal.
            self.assertIn("runtimeinfo", load(d, "prom-a", "errors.json"))
            self.assertEqual(ids["prom-a"]["errors"], len(load(d, "prom-a", "errors.json")))

            # VM cluster: /flags and /metrics from the host root, queries under the prefix.
            vm_paths = [urllib.parse.urlsplit(x[1]).path for x in vms]
            self.assertIn("/flags", vm_paths)
            self.assertIn("/select/0/prometheus/api/v1/query", vm_paths)

            # Read-only and token handling.
            for seen in (ps, vas, vms, ams):
                self.assertTrue(all(m == "GET" for m, _, _ in seen))
            self.assertTrue(all(auth == "Bearer t0ken" for _, _, auth in ps))
            self.assertTrue(all(auth is None for _, _, auth in vas))
            for root, _, files in os.walk(d):
                for f in files:
                    with open(os.path.join(root, f), encoding="utf-8") as fh:
                        self.assertNotIn("t0ken", fh.read(), f)

    def test_unreachable_backend_still_writes_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            rc, _ = self.run_collect(["--out", d, "--timeout", "1", "--no-metric-jobs", "--am", "am=http://127.0.0.1:9"])
            self.assertEqual(rc, 0)
            self.assertIn("status", load(d, "am", "errors.json"))
            self.assertEqual(load(d, "manifest.json")["components"][0]["id"], "am")

    def test_bad_arguments(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_collect(["--out", d])[0], 2)
            self.assertEqual(self.run_collect(["--out", d, "--range", "7days", "--am", "http://x:1"])[0], 2)
            self.assertEqual(self.run_collect(["--out", d, "--am", "a=http://x:1", "--prom", "a=http://y:1"])[0], 2)


if __name__ == "__main__":
    unittest.main()
