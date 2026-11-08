"""逐次比對 vmalert 每次 evaluation 的實際結果與「視窗內有沒有可見 sample」的預測。

輸入：results/matrix-debug-vmalert.log（vmalert debug log）、results/matrix-debug-pushes.jsonl（每筆 sample 實際寫入時間）。
預測：evaluation 在牆上時間 W 執行，查詢時間是 ts，視窗 (ts − R, ts]。sample t 在 W 時可見的條件是寫入時間 ≤ W。
rate() 要有值：視窗內至少一筆可見 sample，且視窗前（queryStep 5m 內）還有一筆，或視窗內有兩筆。
寫入時間和 W 相差不到 2 秒的 evaluation 標為「邊界」，不計入比對。
輸出：results/matrix-debug-check.json
"""
import json
import os
import re
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
R, STEP = 120, 300


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


pushes = {}
for line in open(os.path.join(RES, "matrix-debug-pushes.jsonl")):
    if line.startswith("{"):
        p = json.loads(line)
        pushes.setdefault((p["I"], p["L"]), []).append((p["ts"], p["pushed"]))

pat = re.compile(r'^(\S+)\s.*"Rate2m_I(\d+)_L(\d+)".* at (\S+): query returned (\d+) series')
cells = {}
for line in open(os.path.join(RES, "matrix-debug-vmalert.log")):
    m = pat.search(line)
    if not m:
        continue
    w, i, l, ts, n = parse_time(m.group(1)), int(m.group(2)), int(m.group(3)), parse_time(m.group(4)), int(m.group(5))
    samples = pushes.get((i, l), [])
    if not samples or w < samples[0][1] + STEP:  # 前 5 分鐘資料還在累積，跳過
        continue
    vis = [t for t, p in samples if p <= w]
    border = any(abs(p - w) < 2 for t, p in samples)
    inwin = [t for t in vis if ts - R < t <= ts]
    before = [t for t in vis if ts - R - STEP < t <= ts - R]
    pred = len(inwin) >= 2 or (len(inwin) == 1 and len(before) >= 1)
    c = cells.setdefault(f"I={i},L={l}", {"I": i, "L": l, "evals": 0, "border": 0, "pred_nonempty": 0,
                                           "actual_nonempty": 0, "mismatch": 0, "delay_min": 1e9, "delay_max": 0})
    c["evals"] += 1
    c["delay_min"] = round(min(c["delay_min"], w - ts), 1)
    c["delay_max"] = round(max(c["delay_max"], w - ts), 1)
    if border:
        c["border"] += 1
        continue
    c["pred_nonempty"] += pred
    c["actual_nonempty"] += n > 0
    c["mismatch"] += pred != (n > 0)

for c in cells.values():
    c["contract_ok_D30"] = R >= c["I"] + c["L"] - 30
out = {"R": R, "cells": dict(sorted(cells.items(), key=lambda kv: (kv[1]["I"], kv[1]["L"])))}
json.dump(out, open(os.path.join(RES, "matrix-debug-check.json"), "w"), indent=1)
tot = sum(c["evals"] - c["border"] for c in cells.values())
mis = sum(c["mismatch"] for c in cells.values())
print(f"evaluations compared: {tot}, mismatches: {mis}")
for k, c in out["cells"].items():
    print(k, c)
