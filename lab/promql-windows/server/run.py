#!/usr/bin/env python3
"""Server lab 的資料與查詢（由 run.sh 呼叫）。

  python3 run.py data <dir>      # 寫出 data.om（給 promtool 回填）與 import.jsonl（給 VictoriaMetrics）
  python3 run.py wait            # 等三台都 ready
  python3 run.py import <dir>    # 匯入兩台 VictoriaMetrics
  python3 run.py query <out>     # 跑 cases.py，寫 <out>.json 與 <out>.md

只用標準函式庫。資料時間戳是 T0 + 30s × i，查詢指定 time=，不依賴牆鐘。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from cases import CASES, T0  # noqa: E402

PORTS = {"prom": 33001, "vm": 33002, "vm-minstale": 33003}
INTERVAL = 30
# 與 ../gen_tests.py 的 SERIES 相同；staleness marker 沒辦法用文字格式匯入，s 不放。
SERIES = {
    "c": "0+10x20",
    "g": "1+1x20",
    "c_gap": "0 10 20 _ 40 50 60 70 80 90",
    "c_inflight": "0 10 20 30",
}


def expand(desc: str) -> list:
    out: list = []
    for tok in desc.split():
        m = re.fullmatch(r"(-?[\d.]+)([+-][\d.]+)x(\d+)", tok)
        if m:
            a, b, n = float(m[1]), float(m[2]), int(m[3])
            out += [a + b * i for i in range(n + 1)]
        elif tok == "_":
            out.append(None)
        else:
            out.append(float(tok))
    return out


def write_data(d: str) -> None:
    om, js = [], []
    for name, desc in SERIES.items():
        vals = expand(desc)
        ts = [T0 + INTERVAL * i for i, v in enumerate(vals) if v is not None]
        vs = [v for v in vals if v is not None]
        om.append(f"# TYPE {name} gauge")
        om += [f'{name}{{job="lab"}} {v:g} {t}' for t, v in zip(ts, vs)]
        js.append(json.dumps({"metric": {"__name__": name, "job": "lab"}, "values": vs,
                              "timestamps": [t * 1000 for t in ts]}))
    om.append("# EOF")
    open(os.path.join(d, "data.om"), "w").write("\n".join(om) + "\n")
    open(os.path.join(d, "import.jsonl"), "w").write("\n".join(js) + "\n")


def http(url: str, data: bytes | None = None) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as r:
        return r.read()


def do_import(d: str) -> None:
    body = open(os.path.join(d, "import.jsonl"), "rb").read()
    for t in ("vm", "vm-minstale"):
        base = f"http://127.0.0.1:{PORTS[t]}"
        http(base + "/api/v1/import", body)
        http(base + "/internal/force_flush")


def query(target: str, q: str, at: int, step: str | None) -> dict:
    p = {"query": q, "time": str(T0 + at)}
    if target != "prom":
        p["nocache"] = "1"
        if step:
            p["step"] = step
    return json.loads(http(f"http://127.0.0.1:{PORTS[target]}/api/v1/query?" + urllib.parse.urlencode(p)))


def wait_ready(timeout: float = 90) -> None:
    import time
    end = time.time() + timeout
    for t, path in (("prom", "/-/ready"), ("vm", "/health"), ("vm-minstale", "/health")):
        while True:
            try:
                http(f"http://127.0.0.1:{PORTS[t]}{path}")
                break
            except Exception:  # noqa: BLE001
                if time.time() > end:
                    raise
                time.sleep(1)


def run(out: str) -> int:
    rows, bad = [], 0
    for target, group, why, q, at, step, exp in CASES:
        r = query(target, q, at, step)
        res = r.get("data", {}).get("result", [])
        got = None if not res else [float(x["value"][1]) for x in res]
        ok = (exp is None and got is None) or (got is not None and len(got) == 1 and abs(got[0] - exp) < 1e-9)
        bad += not ok
        rows.append({"target": target, "group": group, "why": why, "query": q, "at": at, "step": step,
                     "expect": exp, "got": got, "labels": [x["metric"] for x in res], "ok": ok})
    json.dump(rows, open(out + ".json", "w"), ensure_ascii=False, indent=1)
    md = ["| 對象 | 群組 | 說明 | 查詢 | time | step | 期望 | 實際 | 符合 |", "|---|---|---|---|---|---|---|---|---|"]
    fmt = lambda v: "空" if v is None else (f"{v:.6g}" if isinstance(v, float) else str(v))  # noqa: E731
    for x in rows:
        got = "空" if x["got"] is None else "、".join(f"{v:.6g}" for v in x["got"])
        md.append(f"| {x['target']} | {x['group']} | {x['why']} | `{x['query']}` | T0+{x['at']}s | {x['step'] or '—'} "
                  f"| {fmt(x['expect'])} | {got} | {'是' if x['ok'] else '**否**'} |")
    open(out + ".md", "w").write("\n".join(md) + "\n")
    print(f"{len(rows)} queries, {bad} mismatches")
    return 1 if bad else 0


def main() -> int:
    cmd = sys.argv[1]
    if cmd == "data":
        write_data(sys.argv[2])
        return 0
    if cmd == "wait":
        wait_ready()
        return 0
    if cmd == "import":
        do_import(sys.argv[2])
        return 0
    if cmd == "query":
        return run(sys.argv[2])
    return 2


if __name__ == "__main__":
    sys.exit(main())
