#!/usr/bin/env python3
"""Turn rule files into the snapshot layout check_coverage.py reads, for when there is no API access.

  python3 tools/rules_to_snapshot.py --out snapshot --ruler prom-a rules/a.yml rules/b.yml --ruler meta meta/*.yml

Each `--ruler ID` starts a group; the files after it are that ruler's rule files (Prometheus or vmalert
format). Writes <out>/<ID>/rules.json shaped like /api/v1/rules, so the coverage checker can tell
missing / self_only before anyone looks at timing contracts. Data presence (rule_no_data) still
needs --live against a real ruler.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog as C  # noqa: E402


def convert(files: list[str]) -> dict:
    groups = []
    for path in files:
        doc = C._load(path) or {}
        for g in doc.get("groups", []) or []:
            rules = []
            for r in g.get("rules", []) or []:
                if "alert" in r:
                    rules.append({"type": "alerting", "name": r["alert"], "query": str(r.get("expr", "")),
                                  "duration": r.get("for", "0s"), "labels": r.get("labels", {}),
                                  "annotations": r.get("annotations") or {}})
                elif "record" in r:
                    rules.append({"type": "recording", "name": r["record"], "query": str(r.get("expr", ""))})
            groups.append({"name": g.get("name"), "file": path, "interval": g.get("interval"),
                           "type": g.get("type", "prometheus"), "rules": rules})
    return {"status": "success", "data": {"groups": groups}}


def parse_args(raw: list[str]) -> tuple[str, dict[str, list[str]]]:
    """--out DIR anywhere; then --ruler ID file... groups. argparse cannot express the grouping, so parse by hand."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], epilog=__doc__.split("\n", 2)[2],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 usage="%(prog)s --out DIR --ruler ID file... [--ruler ID file...]")
    ap.add_argument("--out", required=True, help="snapshot directory to write")
    ap.add_argument("--ruler", metavar="ID", action="append", help="starts a group: the files after it belong to ID")
    if not raw or "-h" in raw or "--help" in raw:
        ap.print_help()
        sys.exit(0 if raw else 2)
    if "--out" not in raw or raw.index("--out") + 1 >= len(raw):
        ap.error("--out DIR is required")
    out_i = raw.index("--out")
    out = raw[out_i + 1]
    rest = raw[:out_i] + raw[out_i + 2:]
    plan: dict[str, list[str]] = {}
    current = None
    i = 0
    while i < len(rest):
        if rest[i] == "--ruler":
            if i + 1 >= len(rest):
                ap.error("--ruler needs an ID")
            current = rest[i + 1]
            plan.setdefault(current, [])
            i += 2
            continue
        if current is None:
            ap.error("give --ruler ID before rule files")
        plan[current].append(rest[i])
        i += 1
    if not plan or not any(plan.values()):
        ap.error("no rule files: --ruler ID file...")
    return out, plan


def main() -> int:
    out, plan = parse_args(sys.argv[1:])
    for rid, files in plan.items():
        os.makedirs(os.path.join(out, rid), exist_ok=True)
        snap = convert(files)
        with open(os.path.join(out, rid, "rules.json"), "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
        n = sum(len(g["rules"]) for g in snap["data"]["groups"])
        print(f"{rid}: {len(files)} files, {n} rules -> {out}/{rid}/rules.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
