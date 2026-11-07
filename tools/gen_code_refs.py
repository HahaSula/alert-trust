#!/usr/bin/env python3
"""Regenerate catalog/refs-code.yaml: pinned source locations found by keyword.

  python3 tools/gen_code_refs.py            # needs the gh CLI (authenticated) to fetch files

catalog/refs-code.src.json maps key -> [repo, tag, path, keyword, nth?]. The script fetches the file at
that tag, finds the nth line containing the keyword and writes a permalink. A keyword that is no longer
found is a sign the code moved: the script fails so the reference gets reviewed instead of going stale.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES = {"prometheus/prometheus": "Prometheus", "prometheus/alertmanager": "Alertmanager",
         "VictoriaMetrics/VictoriaMetrics": "VictoriaMetrics", "prometheus/mysqld_exporter": "mysqld_exporter",
         "prometheus-community/postgres_exporter": "postgres_exporter", "VictoriaMetrics/operator": "VictoriaMetrics operator"}


def fetch(repo: str, tag: str, path: str, cache: dict) -> list[str]:
    key = (repo, tag, path)
    if key not in cache:
        out = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github.raw",
                              f"repos/{repo}/contents/{path}?ref={tag}"], capture_output=True, check=True)
        cache[key] = out.stdout.decode("utf-8", "replace").split("\n")
    return cache[key]


def main() -> int:
    specs = json.load(open(os.path.join(ROOT, "catalog", "refs-code.src.json"), encoding="utf-8"))
    cache: dict = {}
    lines = ["# 由 tools/gen_code_refs.py 依 catalog/refs-code.src.json 產生，不要手改。",
             "# 每一筆是釘在版本與行號的原始碼位置。", "refs:"]
    failed = []
    for key, spec in specs.items():
        repo, tag, path, keyword = spec[:4]
        nth = spec[4] if len(spec) > 4 else 1
        hits = [i + 1 for i, l in enumerate(fetch(repo, tag, path, cache)) if keyword in l]
        if len(hits) < nth:
            failed.append(f"{key}: {keyword!r} not found in {repo}@{tag}/{path}")
            continue
        line = hits[nth - 1]
        q = lambda x: json.dumps(x, ensure_ascii=False)  # noqa: E731
        lines += [f"  {key}:", "    kind: code", f"    project: {q(NAMES.get(repo, repo))}", f"    version: {q(tag)}",
                  f"    file: {q(path)}", f"    line: {line}",
                  f"    url: {q(f'https://github.com/{repo}/blob/{tag}/{path}#L{line}')}"]
    if failed:
        print("\n".join(failed), file=sys.stderr)
        return 1
    with open(os.path.join(ROOT, "catalog", "refs-code.yaml"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {len(specs)} code refs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
