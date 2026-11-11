#!/usr/bin/env python3
"""從 results/events.jsonl 算出各情境的時間，寫 results/summary-data.md（覆寫）。

時鐘：events.jsonl 的 t 是接收端容器的牆鐘；startsAt／updatedAt／endsAt 是 Alertmanager 的時間戳記，
activeAt 是 Prometheus 的時間戳記。三個容器跑在同一台 Docker Desktop VM 上，共用同一個牆鐘。
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
GI = 30.0


def ts(s):
    m = re.match(r"(.*T\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$", s)
    frac = (m.group(2) or ".0")[:7]
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    return datetime.fromisoformat(m.group(1) + frac + tz).replace(tzinfo=timezone.utc).timestamp()


def main():
    res = os.path.join(RES, "rounding") if sys.argv[1:] == ["rounding"] else RES
    ev = [json.loads(l) for l in open(os.path.join(res, "events.jsonl"))]
    t0 = next(e["t"] for e in ev if e["ev"] == "mark" and e["what"] == "a")
    rel = lambda t: t - t0  # noqa: E731
    marks = {e["what"]: e["t"] for e in ev if e["ev"] == "mark"}
    sets = [(e["t"], e["inst"], e["v"]) for e in ev if e["ev"] == "set"]
    out = ["# alert-delivery：由 analyze.py 從 events.jsonl 算出", "",
           "時間都是秒，相對於情境 (a) 開始的記號（接收端容器牆鐘）。`AM 收到` 是 Alertmanager API 的 `updatedAt`，"
           "`activeAt` 是 Prometheus API 的值。", ""]
    rows = [(t, f"set inst {i} = {v}") for t, i, v in sets] + [(t, f"記號 {k}") for k, t in marks.items()]
    for e in ev:
        if e["ev"] == "prom":
            act = f"（activeAt {rel(ts(e['activeAt'])):.1f}）" if e.get("activeAt") else ""
            rows.append((e["t"], f"Prometheus inst {e['inst']} → {e['state']}{act}（輪詢看到）"))
    out += ["## 事件", "", "| 時間 | 事件 |", "|---|---|"] + [f"| {rel(t):.1f} | {x} |" for t, x in sorted(rows)]
    for am in ("a", "b", "c", "d"):
        nots = [e for e in ev if e["ev"] == "notify" and e["am"] == am]
        if not nots:
            continue
        T1 = nots[0]["t"]
        out += ["", f"## Alertmanager {am.upper()}", "",
                "| 通知 | 時間 | 距第一次通知 | ÷ group_interval 30s | 距上一次 | status | 內容 |", "|---|---|---|---|---|---|---|"]
        prev = None
        for n, e in enumerate(nots, 1):
            d = e["t"] - T1
            body = "、".join(f"{a['inst']}:{a['status']}" for a in sorted(e["alerts"], key=lambda a: a["inst"]))
            out.append(f"| {n} | {rel(e['t']):.1f} | {d:.1f} | {d / GI:.2f} | "
                       f"{'' if prev is None else f'{e['t'] - prev:.1f}'} | {e['status']} | {body} |")
            prev = e["t"]
        ams = [e for e in ev if e["ev"] == "am" and e["am"] == am and e["state"] != "gone"]
        first = {}
        for e in ams:
            first.setdefault(e["inst"], e)
        out += ["", "| inst | AM 收到（第一次 updatedAt） | startsAt | 第一次 endsAt − updatedAt |", "|---|---|---|---|"]
        for i, e in sorted(first.items()):
            out.append(f"| {i} | {rel(ts(e['updatedAt'])):.1f} | {rel(ts(e['startsAt'])):.1f} | "
                       f"{ts(e['endsAt']) - ts(e['updatedAt']):.1f} |")
        gone = [e for e in ev if e["ev"] == "am" and e["am"] == am and e["state"] == "gone"]
        if gone:
            out += ["", "API 不再列出（resolved，輪詢看到）：" + "、".join(f"inst {e['inst']} {rel(e['t']):.1f}" for e in gone)]
        # (e) 最後一次 endsAt
        if "e-kill" in marks:
            last = [e for e in ams if e["inst"] == "2" and e["t"] < marks["e-kill"] + 1]
            if last:
                L = last[-1]
                rn = next((x for x in nots if x["t"] > marks["e-kill"] and any(
                    a["inst"] == "2" and a["status"] == "resolved" for a in x["alerts"])), None)
                out += ["", f"(e) kill 在 {rel(marks['e-kill']):.1f}；inst 2 最後一次 updatedAt {rel(ts(L['updatedAt'])):.1f}、"
                        f"endsAt {rel(ts(L['endsAt'])):.1f}（差 {ts(L['endsAt']) - ts(L['updatedAt']):.1f}）"
                        + (f"；resolved 通知 {rel(rn['t']):.1f}，距 endsAt {rn['t'] - ts(L['endsAt']):.1f}、距 kill "
                           f"{rn['t'] - marks['e-kill']:.1f}、距第一次通知 {(rn['t'] - T1) / GI:.2f} × 30s" if rn else "；沒有 resolved 通知")]
    ck = [e for e in ev if e["ev"] == "clock"]
    if ck:
        out += ["", "## 時鐘（接收端容器）", "", "每 30s 單調時鐘對應的牆鐘秒數：" + "、".join(f"{e['wall']:.2f}" for e in ck)]
    with open(os.path.join(res, "summary-data.md"), "w") as f:
        f.write("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()
