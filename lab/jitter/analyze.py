"""讀兩個 Prometheus 的結果，比對模型預測與實測。輸出 results/<date>.json 與 results/<date>.md。"""
import json, sys, time, urllib.parse, urllib.request
from combos import COMBOS, E, predict_empty_fraction

START = float(sys.argv[1]); END = float(sys.argv[2]); WARM = float(sys.argv[3]) if len(sys.argv) > 3 else 600
PROMS = {"2.51.2": "http://localhost:19191", "3.13.5": "http://localhost:19192"}


def q(base, expr, t):
    url = f"{base}/api/v1/query?" + urllib.parse.urlencode({"query": expr, "time": t})
    return json.load(urllib.request.urlopen(url, timeout=30))["data"]["result"]


def raw(base, expr, t):
    res = q(base, expr, t)
    return [(float(ts), float(v)) for r in res for ts, v in r["values"]]


out = {"start": START, "end": END, "analysis_window_s": END - START - WARM, "E": E, "results": []}
win = int(END - START - WARM)
for cid, S, d, R, O, F in COMBOS:
    alert = "KnownTrue_" + cid.replace("-", "_")
    row = {"combo": cid, "S": S, "d": d, "R": R, "O": O, "F": F,
           "predicted_empty": round(predict_empty_fraction(S, d, R, O), 3)}
    for ver, base in PROMS.items():
        pts = [p for p in raw(base, f'probe:visible_samples{{combo="{cid}"}}[{win}s]', END) if p[0] >= START + WARM]
        hist = {}
        for _, v in pts:
            hist[int(v)] = hist.get(int(v), 0) + 1
        # 這些組合不會出現 0 個 sample，所以只看實際記錄到的 evaluation：看得到不到 2 個就是 rate 回傳空
        empty = sum(c for v, c in hist.items() if v < 2)
        expected = max(len(pts), 1)
        fire = raw(base, f'ALERTS{{alertname="{alert}",alertstate="firing"}}[{int(END - START)}s]', END)
        fs = raw(base, f'ALERTS_FOR_STATE{{alertname="{alert}"}}[{int(END - START)}s]', END)
        row[ver] = {"evals": expected, "visible_hist": dict(sorted(hist.items())),
                    "observed_empty": round(empty / expected, 3),
                    "first_firing_s": round(min(t for t, _ in fire) - START) if fire else None,
                    "pending_starts": len({v for _, v in fs})}
    out["results"].append(row)
date = time.strftime("%Y%m%d", time.localtime(END))
json.dump(out, open(f"results/run-{date}.json", "w"), indent=1)
lines = [f"# Pending 抖動實驗 {date}", "",
         f"每組一個 job 與一個 rule group，`evaluation_interval` {E}s，跑 {int((END - START) / 60)} 分鐘；"
         f"空結果比例只算最後 {int(win / 60)} 分鐘（前 {int(WARM / 60)} 分鐘是資料累積）。", "",
         "| 組合 | S | d | R | O | for | 預測空結果 | 2.51.2 實測 | 2.51.2 首次 firing | 2.51.2 pending 次數 | 3.13.5 實測 | 3.13.5 首次 firing | 3.13.5 pending 次數 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
for r in out["results"]:
    a, b = r["2.51.2"], r["3.13.5"]
    ff = lambda x: f"{x}s" if x is not None else "從未"
    lines.append(f"| `{r['combo']}` | {r['S']}s | {r['d']}s | {r['R']}s | {r['O']}s | {r['F']}s | {r['predicted_empty']:.0%} | "
                 f"{a['observed_empty']:.0%} | {ff(a['first_firing_s'])} | {a['pending_starts']} | "
                 f"{b['observed_empty']:.0%} | {ff(b['first_firing_s'])} | {b['pending_starts']} |")
lines += ["", "看得到的 sample 數分布（每次 evaluation 一筆；沒有列出的次數代表 0 個 sample）：", ""]
for r in out["results"]:
    lines.append(f"- `{r['combo']}`：2.51.2 {r['2.51.2']['visible_hist']}，3.13.5 {r['3.13.5']['visible_hist']}")
open(f"results/run-{date}.md", "w").write("\n".join(lines) + "\n")
print("\n".join(lines))
