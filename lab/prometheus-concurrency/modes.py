#!/usr/bin/env python3
"""A／B：rule group 的執行模式。一次只開一台 Prometheus（避免彼此搶 CPU），
每 0.25 秒讀 /api/v1/rules，收集每條 rule 每一輪的 [開始, 開始 + evaluationTime]，算出同時執行的峰值。

  python3 modes.py            # 約 5 分鐘，結果寫到 results/modes.json 與 results/modes.md
"""
import json
import os
import statistics
import subprocess
import time
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CASES = [  # 服務名稱, port, 說明
    ("seq", 21301, "2.55.1 預設，1 group × 8 條"),
    ("k1", 21307, "2.55.1 concurrent-rule-eval，K=1，1 group × 8 條"),
    ("k2", 21303, "2.55.1 concurrent-rule-eval，K=2，1 group × 8 條"),
    ("k4", 21302, "2.55.1 concurrent-rule-eval，K=4，1 group × 8 條"),
    ("v3k2", 21306, "3.13.5 concurrent-rule-eval，K=2，1 group × 8 條"),
    ("multi", 21304, "2.55.1 預設，5 group × 8 條"),
    ("multik2", 21305, "2.55.1 concurrent-rule-eval，K=2，5 group × 8 條"),
]
WARMUP, OBSERVE = 8, 36


def ts(s: str) -> float:
    # 2026-11-08T15:00:40.732923628Z → 只保留到微秒
    s = s.rstrip("Z")
    if "." in s:
        a, b = s.split(".")
        s = f"{a}.{b[:6]}"
    return datetime.fromisoformat(s).timestamp()


def peak(intervals):
    ev = sorted([(a, 1) for a, _ in intervals] + [(b, -1) for _, b in intervals], key=lambda x: (x[0], x[1]))
    cur = best = 0
    for _, d in ev:
        cur += d
        best = max(best, cur)
    return best


def observe(port):
    rules, groups = {}, {}
    end = time.time() + OBSERVE
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/api/v1/rules", timeout=2) as r:
                data = json.load(r)["data"]["groups"]
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
            continue
        for g in data:
            if g["lastEvaluation"].startswith("0001"):
                continue
            groups[(g["name"], g["lastEvaluation"])] = g["evaluationTime"]
            for r in g["rules"]:
                if r["lastEvaluation"].startswith("0001"):
                    continue
                s = ts(r["lastEvaluation"])
                rules[(g["name"], r["name"], r["lastEvaluation"])] = (s, s + r["evaluationTime"])
        time.sleep(0.25)
    return rules, groups


def main():
    out = []
    for svc, port, desc in CASES:
        subprocess.run(["docker", "compose", "up", "-d", svc], cwd=HERE, check=True, capture_output=True)
        time.sleep(WARMUP)
        rules, groups = observe(port)
        subprocess.run(["docker", "compose", "rm", "-sf", svc], cwd=HERE, check=True, capture_output=True)
        ivs = list(rules.values())
        per_group = {}
        for (g, _, _), iv in rules.items():
            per_group.setdefault(g, []).append(iv)
        res = {
            "case": svc, "desc": desc,
            "rule_evals": len(ivs),
            "rule_ms_median": round(statistics.median(b - a for a, b in ivs) * 1000),
            "group_ms_median": round(statistics.median(groups.values()) * 1000),
            "group_iterations": len(groups),
            "peak_global": peak(ivs),
            "peak_per_group": {g: peak(v) for g, v in sorted(per_group.items())},
        }
        out.append(res)
        print(json.dumps(res, ensure_ascii=False))
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    with open(os.path.join(HERE, "results", "modes.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
