#!/usr/bin/env python3
"""Check that the metric names a catalog card relies on are really exposed by that component.

  python3 tools/check_metrics.py prometheus=http://localhost:9090/metrics alertmanager=http://localhost:9093/metrics
  python3 tools/check_metrics.py prometheus=./saved-metrics.txt --json out.json

Names produced by a scraper rather than the component (up, scrape_*, ALERTS*), or by other exporters
(cAdvisor, kube-state-metrics, kubelet), are reported as "external" instead of missing.
Counters that only appear after the first event are possible; treat "missing" as "check by hand".
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

EXTERNAL = re.compile(r"^(up|scrape_.*|ALERTS|ALERTS_FOR_STATE|flag|container_.*|kube_.*|kubelet_.*|node_.*)$")
SUFFIXES = ("", "_total", "_bucket", "_sum", "_count", "_created", "_info")
LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)")


def exposed_names(src: str) -> set[str]:
    if re.match(r"^https?://", src):
        with urllib.request.urlopen(src, timeout=10) as r:  # noqa: S310 - user-supplied local URL
            text = r.read().decode("utf-8", "replace")
    else:
        with open(src, encoding="utf-8") as f:
            text = f.read()
    names = set()
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = LINE.match(line)
        if m:
            names.add(m.group(1))
    return names


def is_exposed(metric: str, names: set[str]) -> bool:
    if any(metric + s in names for s in SUFFIXES):
        return True
    # catalog lists counters with _total; old clients may expose them without it
    return metric.endswith("_total") and metric[: -len("_total")] in names


def check(card: dict, names: set[str]) -> list[dict]:
    out = []
    for s in card.get("signals", []):
        for m in s.get("metrics", []):
            if EXTERNAL.match(m):
                status = "external"
            else:
                status = "found" if is_exposed(m, names) else "missing"
            out.append({"component": card["id"], "signal": s["id"], "tier": s["tier"], "metric": m, "status": status})
    return out


def main() -> int:
    import catalog as C

    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="+", help="component=url-or-file")
    ap.add_argument("--json")
    a = ap.parse_args()
    cards = {c["id"]: c for c in C.load_components()}
    results = []
    for t in a.targets:
        comp, _, src = t.partition("=")
        if comp not in cards:
            sys.exit(f"unknown component {comp!r}; known: {', '.join(sorted(cards))}")
        results += check(cards[comp], exposed_names(src))
    missing = [r for r in results if r["status"] == "missing"]
    found = sum(1 for r in results if r["status"] == "found")
    print(f"{found} found, {len(missing)} missing, {sum(1 for r in results if r['status'] == 'external')} external")
    for r in missing:
        print(f"MISSING tier {r['tier']} {r['component']}#{r['signal']}: {r['metric']}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=1, ensure_ascii=False)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
