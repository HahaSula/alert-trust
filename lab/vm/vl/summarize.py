"""VL log rule 視窗：每條 rule 觸發次數 vs 期間內的事件數。python3 summarize.py > results/vl-logdelay.json"""
import calendar, json, os, time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
rows = [json.loads(l) for l in open(os.path.join(HERE, "results", "vl.jsonl"))]
lg = [json.loads(l) for l in open(os.path.join(HERE, "results", "loggen.jsonl"))]
marks = {r["mark"]: r["t"] for r in rows if "mark" in r}
start, end = marks["steady"], marks["readonly-on"]  # single 只在唯讀前受控
D, E = 30, 30
created = sorted(x["t"] for x in lg if x["type"] == "event-created")
out = {"window": [start, end], "D": D, "E": E, "rules": {}}
for name, lag in (("EvL0", 0), ("EvL20", 20), ("EvL45", 45), ("EvL90", 90), ("EvL90Time10m", 90)):
    active = set()
    for r in rows:
        if "single" in r and start <= r["t"] < end and name in r["single"]:
            active.update(r["single"][name]["activeAt"])
    # 事件要在 start 之後產生、且 end 之前有機會被看到。這台機器實際延遲到 77 秒（見 README），留 110 秒
    ev = [t for t in created if t >= start and t + lag + 110 <= end]
    act_ts = sorted(calendar.timegm(time.strptime(a, "%Y-%m-%dT%H:%M:%SZ")) for a in active)
    act_ts = [a for a in act_ts if a >= start]
    out["rules"][name] = {"lag_s": lag, "events": len(ev), "firing_episodes": len(act_ts),
                          "predicted": ("covered by _time:10m" if "Time10m" in name else
                                        "always" if lag <= D else ("never" if lag > D + E else "phase-dependent"))}
print(json.dumps(out, indent=1))
