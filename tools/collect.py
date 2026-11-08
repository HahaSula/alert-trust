#!/usr/bin/env python3
"""Read-only snapshot of a monitoring stack for the alert-trust self-check (stage 2 of docs/model.md#flow).

Only sends HTTP GET requests. Standard library only.

  python3 tools/collect.py --out ./selfcheck-data/<date> [--range 7d] \
      --prom prom-a=http://localhost:9090 \
      --vmalert vmalert-biz=http://localhost:8880 \
      --vm vm=http://localhost:8428 \
      --am am=http://localhost:9093

Every backend flag can be repeated and takes `[id=]URL`. Use the component or observer id from
env/<name>.yaml so tools/check_coverage.py can match the snapshot to the env profile. Without an id,
one is derived from host and port (e.g. `localhost-9090`) and printed.

Backends:
  --prom     Prometheus: status APIs, rules, targets, config, peak queries, /metrics
  --vmalert  vmalert: /api/v1/rules, /api/v1/alerts, /flags, /metrics
  --vm       VictoriaMetrics single or vmselect: /flags, /metrics, peak queries via /api/v1/query.
             For a cluster pass the query prefix, e.g. http://vmselect:8481/select/0/prometheus;
             /flags and /metrics are then read from the host root.
  --vmagent  vmagent: /flags, /metrics
  --vlogs    VictoriaLogs: /flags, /metrics
  --am       Alertmanager: /api/v2/status, /api/v2/alerts, /api/v2/silences, /metrics

Snapshot layout (see skills/selfcheck/references/snapshot.md):
  <out>/manifest.json            what was collected, per id: kind, url (credentials removed), files, error count
  <out>/<id>/component.json      {id, kind, url, collected_at}
  <out>/<id>/buildinfo.json      {"status":"success","data":{"version":...,"source":...}} when known
  <out>/<id>/rules.json          raw /api/v1/rules response (prom, vmalert)
  <out>/<id>/flags.json          {"status":"success","data":{flag: value}, "set":[...]} (prom: raw API)
  <out>/<id>/metrics.txt         raw /metrics text
  <out>/<id>/peaks.json          {"range", "exprs", "results"} (prom, vm)
  <out>/<id>/metric_jobs.json    metric used by an alert -> {job: series count} (prom, vm)
  <out>/<id>/errors.json         {name: message}; an empty object means everything was read

Authentication: set AT_TOKEN_<ID> (id upper-cased, non-alphanumerics -> _) or the per-kind
PROM_BEARER_TOKEN / VMALERT_BEARER_TOKEN / VM_BEARER_TOKEN / VMAGENT_BEARER_TOKEN /
VLOGS_BEARER_TOKEN / AM_BEARER_TOKEN. Tokens are never written to disk.
The output can contain internal hostnames, label values and configuration. Keep it private.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1"
KINDS = ("prom", "vmalert", "vm", "vmagent", "vlogs", "am")
TOKEN_ENV = {
    "prom": "PROM_BEARER_TOKEN",
    "vmalert": "VMALERT_BEARER_TOKEN",
    "vm": "VM_BEARER_TOKEN",
    "vmagent": "VMAGENT_BEARER_TOKEN",
    "vlogs": "VLOGS_BEARER_TOKEN",
    "am": "AM_BEARER_TOKEN",
}
MAX_METRIC_JOBS = 800

# ---------------------------------------------------------------- parsing helpers


def parse_backend(spec: str) -> tuple[str, str]:
    """`[id=]URL` -> (id, url). The id is derived from host and port when missing."""
    ident, url = "", spec
    if "=" in spec and not re.match(r"^[a-z]+://", spec):
        ident, url = spec.split("=", 1)
    if not re.match(r"^https?://", url):
        raise ValueError(f"not an http(s) URL: {spec!r}")
    if not ident:
        p = urllib.parse.urlsplit(url)
        ident = f"{p.hostname}-{p.port}" if p.port else (p.hostname or "backend")
    ident = re.sub(r"[^A-Za-z0-9_.-]", "-", ident)
    return ident, url.rstrip("/")


def safe_url(url: str) -> str:
    """Drop user:password@ and the query string before writing a URL to disk."""
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    if p.port:
        host += f":{p.port}"
    return urllib.parse.urlunsplit((p.scheme, host, p.path, "", ""))


def root_url(url: str) -> str:
    """http://h:8481/select/0/prometheus -> http://h:8481 (where /flags and /metrics live)."""
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, "", "", ""))


def parse_flags_text(text: str) -> dict[str, str]:
    """VictoriaMetrics /flags: one `-name="value"` per line."""
    out = {}
    for line in text.splitlines():
        m = re.match(r'^\s*-([A-Za-z0-9_.\-]+)=(.*)$', line)
        if not m:
            continue
        v = m.group(2).strip()
        if len(v) >= 2 and v[0] == v[-1] == '"':
            v = v[1:-1]
        out[m.group(1)] = v
    return out


_LABEL_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_metric_lines(text: str, name: str) -> list[tuple[dict, str]]:
    """Return (labels, value) for every sample of metric `name` in Prometheus exposition text."""
    out = []
    for line in text.splitlines():
        if not line.startswith(name):
            continue
        rest = line[len(name):]
        if rest[:1] not in ("{", " "):
            continue
        labels = {}
        if rest.startswith("{"):
            end = rest.rfind("}")
            labels = {k: v.replace('\\"', '"').replace("\\\\", "\\") for k, v in _LABEL_RE.findall(rest[1:end])}
            rest = rest[end + 1:]
        parts = rest.split()
        out.append((labels, parts[0] if parts else ""))
    return out


def flags_from_metrics(text: str) -> tuple[dict[str, str], list[str]]:
    """The VictoriaMetrics `flag{name,value,is_set}` metric."""
    values, set_ = {}, []
    for labels, _ in parse_metric_lines(text, "flag"):
        if "name" in labels:
            values[labels["name"]] = labels.get("value", "")
            if labels.get("is_set") == "true":
                set_.append(labels["name"])
    return values, sorted(set_)


def version_from_metrics(text: str) -> str | None:
    """`vm_app_version{short_version="v1.155.0",version="vmalert-..."}` or Go `*_build_info{version=}`."""
    for labels, _ in parse_metric_lines(text, "vm_app_version"):
        return labels.get("short_version") or labels.get("version")
    for line in text.splitlines():
        m = re.match(r'^([a-z_]+)_build_info\{(.*)\}', line)
        if m and m.group(1) != "go":
            v = dict(_LABEL_RE.findall(m.group(2))).get("version")
            if v:
                return v
    return None


PROMQL_KEYWORDS = {
    "sum", "min", "max", "avg", "count", "by", "on", "ignoring", "group_left", "group_right", "without", "and",
    "or", "unless", "bool", "offset", "rate", "irate", "increase", "delta", "idelta", "time", "vector", "absent",
    "absent_over_time", "topk", "bottomk", "quantile", "stddev", "stdvar", "count_values", "histogram_quantile",
    "label_replace", "label_join", "clamp", "clamp_min", "clamp_max", "changes", "resets", "timestamp", "sort",
    "sort_desc", "abs", "ceil", "floor", "round", "exp", "ln", "log2", "log10", "sqrt", "scalar", "deriv",
    "predict_linear", "holt_winters", "double_exponential_smoothing", "sum_over_time", "avg_over_time",
    "min_over_time", "max_over_time", "count_over_time", "last_over_time", "present_over_time",
    "quantile_over_time", "stddev_over_time", "stdvar_over_time", "e", "inf", "nan", "start", "end", "hour",
    "minute", "month", "year", "day_of_month", "day_of_week", "day_of_year", "days_in_month", "group", "limitk",
    "limit_ratio", "keep_metric_names", "default", "if", "ifnot", "atan2", "sgn", "info",
}


def alert_metrics(rules_resp: dict) -> set[str]:
    """Metric names referenced by alerting rules in a /api/v1/rules response (PromQL/MetricsQL groups only)."""
    names = set()
    data = rules_resp.get("data", rules_resp) if isinstance(rules_resp, dict) else {}
    for g in data.get("groups", []) or []:
        if g.get("type") not in (None, "", "prometheus"):
            continue  # vmalert graphite / vlogs groups
        for r in g.get("rules", []) or []:
            if r.get("type") != "alerting":
                continue
            body = r.get("query", "") or r.get("expr", "")
            names |= set(re.findall(r'__name__\s*=\s*"([A-Za-z_:][A-Za-z0-9_:]*)"', body))
            body = re.sub(r'"[^"]*"|\'[^\']*\'|`[^`]*`|\{[^}]*\}|\[[^\]]*\]', " ", body)
            body = re.sub(r"\b(by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", " ", body)
            body = re.sub(r"\b\d+(\.\d+)?([smhdwy]|ms)?\b", " ", body)
            names |= {m for m in re.findall(r"[A-Za-z_:][A-Za-z0-9_:]*", body) if m.lower() not in PROMQL_KEYWORDS}
    return names


def prom_peaks(w: str) -> dict[str, str]:
    """Peak queries against a Prometheus (ported from prometheus-selfcheck inventory)."""
    return {
        "scrape_duration_peak": f"max by (job) (max_over_time(scrape_duration_seconds[{w}]))",
        "scrape_duration_now": "max by (job) (scrape_duration_seconds)",
        "scrape_samples_peak": f"max by (job) (max_over_time(scrape_samples_scraped[{w}]))",
        "up_min": f"min by (job) (min_over_time(up[{w}]))",
        "group_duration_peak": f"max by (rule_group) (max_over_time(prometheus_rule_group_last_duration_seconds[{w}]))",
        "group_duration_now": "max by (rule_group) (prometheus_rule_group_last_duration_seconds)",
        "group_interval": "max by (rule_group) (prometheus_rule_group_interval_seconds)",
        "group_missed": f"sum by (rule_group) (increase(prometheus_rule_group_iterations_missed_total[{w}]))",
        "group_failures": f"sum by (rule_group) (increase(prometheus_rule_evaluation_failures_total[{w}]))",
        "for_state_resets": f"topk(50, changes(ALERTS_FOR_STATE[{w}]))",
        "queue_time_p90_peak": f'max(max_over_time(prometheus_engine_query_duration_seconds{{slice="queue_time",quantile="0.9"}}[{w}]))',
        "query_slots_peak": f"max(max_over_time(prometheus_engine_queries[{w}]) / prometheus_engine_queries_concurrent_max)",
        "wal_fsync_p99_peak": f'max(max_over_time(prometheus_tsdb_wal_fsync_duration_seconds{{quantile="0.99"}}[{w}]))',
        "wal_size": "max(prometheus_tsdb_wal_storage_size_bytes)",
        "blocks_size": "max(prometheus_tsdb_storage_blocks_bytes)",
        "head_series": "max(prometheus_tsdb_head_series)",
        "samples_rate": "sum(rate(prometheus_tsdb_head_samples_appended_total[5m]))",
        "replay_seconds": "max(prometheus_tsdb_data_replay_duration_seconds)",
        "lowest_ts_age_days": "(time() - min(prometheus_tsdb_lowest_timestamp_seconds)) / 86400",
        "retention_seconds": "max(prometheus_tsdb_retention_limit_seconds)",
        "retention_bytes": "max(prometheus_tsdb_retention_limit_bytes)",
        "restarts": f'max(changes(process_start_time_seconds{{job=~".*prometheus.*"}}[{w}]))',
        "rss": 'max(process_resident_memory_bytes{job=~".*prometheus.*"})',
        "cpu_cores_peak": f'max(max_over_time(rate(process_cpu_seconds_total{{job=~".*prometheus.*"}}[5m])[{w}:5m]))',
        "compactions_failed": f"max(increase(prometheus_tsdb_compactions_failed_total[{w}]))",
        "checkpoint_failed": f"max(increase(prometheus_tsdb_checkpoint_creations_failed_total[{w}]))",
        "wal_writes_failed": f"max(increase(prometheus_tsdb_wal_writes_failed_total[{w}]))",
        "wal_corruptions": "max(prometheus_tsdb_wal_corruptions_total)",
        "reloads_min_5m": f"min(min_over_time(increase(prometheus_tsdb_reloads_total[5m])[{w}:5m]))",
        "notify_errors": f"sum(increase(prometheus_notifications_errors_total[{w}]))",
        "notify_dropped": f"sum(increase(prometheus_notifications_dropped_total[{w}]))",
        "notify_queue_ratio_peak": f"max(max_over_time(prometheus_notifications_queue_length[{w}])) / max(prometheus_notifications_queue_capacity)",
        "am_discovered": "min(prometheus_notifications_alertmanagers_discovered)",
        "am_notifications_failed": f"sum by (integration) (increase(alertmanager_notifications_failed_total[{w}]))",
        "has_self_metrics": "count(prometheus_tsdb_head_series)",
        "has_am_metrics": "count(alertmanager_notifications_total)",
        "has_cadvisor": "count(container_cpu_cfs_periods_total)",
        "has_ksm": "count(kube_pod_container_status_restarts_total)",
        "has_kubelet_volume": "count(kubelet_volume_stats_available_bytes)",
        "has_node_exporter": "count(node_filesystem_avail_bytes)",
        "has_blackbox": "count(probe_success)",
        "throttling_ratio_peak": f'max(max_over_time((rate(container_cpu_cfs_throttled_periods_total{{container=~"prometheus"}}[5m]) / rate(container_cpu_cfs_periods_total{{container=~"prometheus"}}[5m]))[{w}:5m]))',
        "mem_ratio_peak": f'max(max_over_time((container_memory_working_set_bytes{{container="prometheus"}} / on (namespace, pod, container) group_left kube_pod_container_resource_limits{{container="prometheus",resource="memory"}})[{w}:5m]))',
    }


def vm_peaks(w: str) -> dict[str, str]:
    """Peak queries against VictoriaMetrics. Results exist only for components whose /metrics are scraped into it."""
    return {
        "scrape_duration_peak": f"max by (job) (max_over_time(scrape_duration_seconds[{w}]))",
        "scrape_samples_peak": f"max by (job) (max_over_time(scrape_samples_scraped[{w}]))",
        "up_min": f"min by (job) (min_over_time(up[{w}]))",
        "vmalert_iteration_duration_peak": f'max by (group) (max_over_time(vmalert_iteration_duration_seconds{{quantile="1"}}[{w}]))',
        "vmalert_iteration_duration_p99_peak": f'max by (group) (max_over_time(vmalert_iteration_duration_seconds{{quantile="0.99"}}[{w}]))',
        "vmalert_iteration_interval": "max by (group) (vmalert_iteration_interval_seconds)",
        "vmalert_iteration_missed": f"sum by (group) (increase(vmalert_iteration_missed_total[{w}]))",
        "vmalert_rule_errors": f"sum by (group) (increase(vmalert_alerting_rules_errors_total[{w}]))",
        "vmalert_send_errors": f"sum by (addr) (increase(vmalert_alerts_send_errors_total[{w}]))",
        "vmalert_remotewrite_errors": f"sum(increase(vmalert_remotewrite_errors_total[{w}]))",
        "partial_results": f"sum(increase(vm_partial_results_total[{w}]))",
        "read_only_max": f"max(max_over_time(vm_storage_is_read_only[{w}]))",
        "vmstorage_unreachable_min": f"min(min_over_time(vm_rpc_vmstorage_is_reachable[{w}]))",
        "select_limit_timeouts": f"sum(increase(vm_concurrent_select_limit_timeout_total[{w}]))",
        "rows_ignored": f"sum by (reason) (increase(vm_rows_ignored_total[{w}]))",
        "rows_dropped_on_overload": f"sum(increase(vm_rpc_rows_dropped_on_overload_total[{w}]))",
        "free_disk_min": f"min(min_over_time(vm_free_disk_space_bytes[{w}]))",
        "data_size": "sum(vm_data_size_bytes)",
        "vmagent_rw_retries": f"sum(increase(vmagent_remotewrite_retries_count_total[{w}]))",
        "vmagent_rw_dropped": f"sum(increase(vmagent_remotewrite_packets_dropped_total[{w}]))",
        "vmagent_pq_dropped": f"sum(increase(vm_persistentqueue_bytes_dropped_total[{w}]))",
        "vl_read_only_max": f"max(max_over_time(vl_storage_is_read_only[{w}]))",
        "am_notifications_failed": f"sum by (integration) (increase(alertmanager_notifications_failed_total[{w}]))",
        "has_vmalert_metrics": "count(vmalert_iteration_duration_seconds)",
        "has_vm_metrics": "count(vm_app_version)",
        "has_am_metrics": "count(alertmanager_notifications_total)",
    }


# ---------------------------------------------------------------- HTTP


class Collector:
    def __init__(self, out: str, window: str, timeout: float, metric_jobs: bool = True):
        self.out, self.window, self.timeout, self.metric_jobs = out, window, timeout, metric_jobs
        self.now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
        self.manifest = {"tool": "alert-trust/tools/collect.py", "version": VERSION, "collected_at": self.now,
                         "range": window, "components": []}
        self.rules_by_id: dict[str, dict] = {}

    # low level -------------------------------------------------------------
    def _get(self, url: str, token: str | None, timeout: float | None = None) -> bytes:
        req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json, text/plain, */*"})
        if token:
            req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return r.read()

    def _dir(self, ident: str) -> str:
        d = os.path.join(self.out, ident)
        os.makedirs(d, exist_ok=True)
        return d

    def _write(self, ident: str, name: str, content, files: list) -> None:
        path = os.path.join(self._dir(ident), name)
        if isinstance(content, (bytes, str)):
            with open(path, "wb") as f:
                f.write(content if isinstance(content, bytes) else content.encode())
        else:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(content, f, indent=1, ensure_ascii=False)
        files.append(name)

    # one backend ---------------------------------------------------------
    def collect(self, kind: str, ident: str, url: str) -> dict:
        token = os.environ.get("AT_TOKEN_" + re.sub(r"[^A-Z0-9]", "_", ident.upper())) or os.environ.get(TOKEN_ENV[kind])
        files: list[str] = []
        errors: dict[str, str] = {}
        ctx = {"kind": kind, "id": ident, "url": url, "token": token, "files": files, "errors": errors}

        def fetch_json(name, path, base=url, save=True):
            try:
                d = json.loads(self._get(base + path, token).decode())
                if save:
                    self._write(ident, name + ".json", d, files)
                return d
            except Exception as e:  # noqa: BLE001 - every failure is recorded, collection continues
                errors[name] = f"GET {path}: {str(e)[:300]}"
                return None

        def fetch_text(name, path, base=url):
            try:
                t = self._get(base + path, token).decode("utf-8", "replace")
                self._write(ident, name, t, files)
                return t
            except Exception as e:  # noqa: BLE001
                errors[name] = f"GET {path}: {str(e)[:300]}"
                return None

        ctx["fetch_json"], ctx["fetch_text"] = fetch_json, fetch_text
        self._write(ident, "component.json", {"id": ident, "kind": kind, "url": safe_url(url), "collected_at": self.now}, files)
        getattr(self, "_collect_" + kind)(ctx)
        self._write(ident, "errors.json", errors, files)
        entry = {"id": ident, "kind": kind, "url": safe_url(url), "files": sorted(files), "errors": len(errors)}
        self.manifest["components"].append(entry)
        return entry

    def _query(self, ctx, base: str, expr: str):
        try:
            d = json.loads(self._get(base + "/api/v1/query?query=" + urllib.parse.quote(expr), ctx["token"],
                                     max(self.timeout, 120)).decode())
            return d.get("data", {}).get("result")
        except Exception as e:  # noqa: BLE001
            ctx["errors"]["query: " + expr[:70]] = str(e)[:200]
            return None

    def _peaks(self, ctx, base: str, exprs: dict[str, str]) -> None:
        res = {k: self._query(ctx, base, v) for k, v in exprs.items()}
        self._write(ctx["id"], "peaks.json", {"range": self.window, "exprs": exprs, "results": res}, ctx["files"])

    def _metric_jobs(self, ctx, base: str, rules: list[dict]) -> None:
        if not self.metric_jobs:
            return
        names = set()
        for r in rules:
            names |= alert_metrics(r)
        jobs = {}
        for m in sorted(names)[:MAX_METRIC_JOBS]:
            r = self._query(ctx, base, f'count by (job) ({{__name__="{m}"}})')
            jobs[m] = {x["metric"].get("job", ""): float(x["value"][1]) for x in (r or [])}
        self._write(ctx["id"], "metric_jobs.json", jobs, ctx["files"])

    def _vm_flags_and_version(self, ctx, base: str) -> str | None:
        """Shared by vmalert / vm / vmagent / vlogs: /flags + /metrics -> flags.json, buildinfo.json."""
        text = ctx["fetch_text"]("flags.txt", "/flags", base)
        metrics = ctx["fetch_text"]("metrics.txt", "/metrics", base)
        flags = parse_flags_text(text) if text else {}
        set_: list[str] = []
        if metrics:
            mflags, set_ = flags_from_metrics(metrics)
            for k, v in mflags.items():
                flags.setdefault(k, v)
        if flags:
            src = "+".join(s for s, ok in (("/flags", bool(text)), ("flag metric", bool(metrics))) if ok)
            self._write(ctx["id"], "flags.json", {"status": "success", "data": flags, "set": set_, "source": src}, ctx["files"])
        version = version_from_metrics(metrics) if metrics else None
        if version:
            self._write(ctx["id"], "buildinfo.json",
                        {"status": "success", "data": {"version": version, "source": "metrics:vm_app_version"}}, ctx["files"])
        return metrics

    # per kind -------------------------------------------------------------
    def _collect_prom(self, ctx) -> None:
        fj = ctx["fetch_json"]
        for name, path in {
            "buildinfo": "/api/v1/status/buildinfo", "flags": "/api/v1/status/flags",
            "runtimeinfo": "/api/v1/status/runtimeinfo", "tsdb": "/api/v1/status/tsdb",
            "walreplay": "/api/v1/status/walreplay", "alertmanagers": "/api/v1/alertmanagers",
            "rules": "/api/v1/rules", "targets": "/api/v1/targets?state=active",
            "config": "/api/v1/status/config", "metadata": "/api/v1/metadata",
        }.items():
            d = fj(name, path)
            if name == "rules" and d:
                self.rules_by_id[ctx["id"]] = d
        ctx["fetch_text"]("metrics.txt", "/metrics")
        self._peaks(ctx, ctx["url"], prom_peaks(self.window))
        if ctx["id"] in self.rules_by_id:
            self._metric_jobs(ctx, ctx["url"], [self.rules_by_id[ctx["id"]]])

    def _collect_vmalert(self, ctx) -> None:
        d = ctx["fetch_json"]("rules", "/api/v1/rules")
        if d:
            self.rules_by_id[ctx["id"]] = d
        ctx["fetch_json"]("alerts", "/api/v1/alerts")
        self._vm_flags_and_version(ctx, ctx["url"])

    def _collect_vm(self, ctx) -> None:
        root = root_url(ctx["url"])
        self._vm_flags_and_version(ctx, root)
        ctx["fetch_json"]("tsdb", "/api/v1/status/tsdb")
        self._peaks(ctx, ctx["url"], vm_peaks(self.window))
        vmalert_rules = [self.rules_by_id[c["id"]] for c in self.manifest["components"]
                         if c["kind"] == "vmalert" and c["id"] in self.rules_by_id]
        if vmalert_rules:
            self._metric_jobs(ctx, ctx["url"], vmalert_rules)

    def _collect_vmagent(self, ctx) -> None:
        self._vm_flags_and_version(ctx, ctx["url"])

    def _collect_vlogs(self, ctx) -> None:
        self._vm_flags_and_version(ctx, ctx["url"])

    def _collect_am(self, ctx) -> None:
        fj = ctx["fetch_json"]
        st = fj("status", "/api/v2/status")
        fj("alerts", "/api/v2/alerts?active=true&silenced=true&inhibited=true")
        fj("silences", "/api/v2/silences")
        ctx["fetch_text"]("metrics.txt", "/metrics")
        version = ((st or {}).get("versionInfo") or {}).get("version")
        if version:
            self._write(ctx["id"], "buildinfo.json",
                        {"status": "success", "data": {"version": version, "source": "api/v2/status"}}, ctx["files"])

    def finish(self) -> None:
        os.makedirs(self.out, exist_ok=True)
        with open(os.path.join(self.out, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(self.manifest, f, indent=1, ensure_ascii=False)


# ---------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    for kind, label in (("prom", "Prometheus"), ("vmalert", "vmalert"), ("vm", "VictoriaMetrics single or vmselect query prefix"),
                        ("vmagent", "vmagent"), ("vlogs", "VictoriaLogs"), ("am", "Alertmanager")):
        ap.add_argument("--" + kind, action="append", default=[], metavar="[ID=]URL", help=f"{label}; repeatable")
    ap.add_argument("--out", default="selfcheck-data", help="snapshot directory (default: selfcheck-data)")
    ap.add_argument("--range", default="7d", help="look-back window for peak queries, e.g. 7d")
    ap.add_argument("--timeout", type=float, default=60, help="per-request timeout in seconds")
    ap.add_argument("--no-metric-jobs", action="store_true", help="skip the per-metric job lookup (many queries)")
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if not re.fullmatch(r"\d+[smhdw]", a.range):
        print("--range must look like 7d", file=sys.stderr)
        return 2
    backends = []
    seen = set()
    # vmalert before vm so metric_jobs on vm can use vmalert rules.
    for kind in ("prom", "vmalert", "vm", "vmagent", "vlogs", "am"):
        for spec in getattr(a, kind):
            try:
                ident, url = parse_backend(spec)
            except ValueError as e:
                print(str(e), file=sys.stderr)
                return 2
            if ident in seen:
                print(f"duplicate id {ident!r}; give each backend its own id=", file=sys.stderr)
                return 2
            seen.add(ident)
            backends.append((kind, ident, url, "=" in spec.split("://")[0]))
    if not backends:
        print("give at least one of --prom/--vmalert/--vm/--vmagent/--vlogs/--am", file=sys.stderr)
        return 2
    c = Collector(a.out, a.range, a.timeout, metric_jobs=not a.no_metric_jobs)
    for kind, ident, url, explicit in backends:
        if not explicit:
            print(f"note: {kind} {safe_url(url)} has no id; using {ident!r}. Match it to env.yaml or pass id=URL.",
                  file=sys.stderr)
        c.collect(kind, ident, url)
    c.finish()
    print(json.dumps({"out": a.out, "components": [{k: e[k] for k in ("id", "kind", "errors")} for e in c.manifest["components"]]},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
