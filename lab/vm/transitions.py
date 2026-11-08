"""把 faults／vl 的 JSON Lines 取樣轉成「每個階段裡，哪個值在故障後第幾秒改變」的清單。

用法：python3 transitions.py results/faults-rf1.jsonl > results/faults-rf1-transitions.txt
每 5 秒取樣一次，所以時間的誤差約 ±5 秒。
"""
import json
import sys


def flatten(rec):
    out = {}
    for k, v in rec.items():
        if k in ("t", "rel", "phase"):
            continue
        if k in ("vmalert_default", "vmalert_resend") and v is not None:
            for name, st in v.items():
                out[f"{k}:{name}.firing"] = st.get("firing", 0)
        elif k == "single" and isinstance(v, dict):
            for name, st in v.items():
                out[f"vl-single:{name}.firing"] = st.get("firing", 0)
        elif k == "cluster" and isinstance(v, dict):
            for name, n in v.items():
                out[f"vl-cluster:{name}.firing"] = n
        elif k in ("health_default", "cluster_health", "single_health") and v is not None:
            for g, h in v.items():
                out[f"{k}:{g}.err"] = h["err"] > 0
        elif k == "am" and v is not None:
            for a, n in v.items():
                out[f"am:{a}"] = n
        elif k == "up" and v is not None:
            for comp, u in v.items():
                out[f"up:{comp}"] = u
        elif k in ("single_rows_ingested",):
            continue  # 一直增加，另外看
        else:
            out[k] = v
    return out


COUNTERS = ("partial_results_total", "rule_errors_total", "iteration_missed_total")


def main(path):
    rows = [json.loads(l) for l in open(path) if l.strip()]
    mark_t, mark_name = rows[0]["rel"], rows[0].get("mark", "start")
    prev, base, last, fired = {}, {}, {}, set()
    lines = []

    def close_phase():
        for c in COUNTERS:
            if c in base and base[c] is not None and last.get(c) is not None:
                lines.append(f"  (phase total) {c} +{last[c] - base[c]:g}")

    for r in rows:
        if "mark" in r:
            close_phase()
            base, fired = dict(last), set()
            extra = {k: v for k, v in r.items() if k not in ("t", "rel", "mark")}
            lines.append(f"\n== {r['mark']} @ {r['rel']}s {json.dumps(extra, ensure_ascii=False) if extra else ''}")
            mark_t, mark_name = r["rel"], r["mark"]
            continue
        if "cells" in r or "ingest_during_down" in r:
            if "ingest_during_down" in r:
                lines.append(f"  +{r['rel'] - mark_t:6.0f}s ingest_during_down = {r['ingest_during_down']}")
            continue
        cur = flatten(r)
        for c in COUNTERS:
            if c in cur:
                v = cur.pop(c)
                if v is not None:
                    if base.get(c) is None:
                        base[c] = v
                    if v > base[c] and c not in fired:
                        fired.add(c)
                        lines.append(f"  +{r['rel'] - mark_t:6.0f}s {c} starts growing ({base[c]:g} -> {v:g})")
                    last[c] = v
        keys = sorted(set(cur) | set(prev))
        for k in keys:
            a, b = prev.get(k, "absent"), cur.get(k, "absent")
            if a != b and prev:
                lines.append(f"  +{r['rel'] - mark_t:6.0f}s {k}: {a} -> {b}")
        prev = cur
    close_phase()
    print("\n".join(lines).lstrip())


if __name__ == "__main__":
    main(sys.argv[1])
