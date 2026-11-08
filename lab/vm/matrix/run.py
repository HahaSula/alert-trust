"""延遲矩陣：跑 DURATION 秒，每 5 秒記錄每個 (I, L) cell 的 alert 狀態，最後輸出 summary。"""
import json, os, subprocess, sys, time, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import Compose, Recorder, getjson, wait_http, HOST, load_jsonl

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
DURATION = int(os.environ.get("DURATION", "780"))
TAG = os.environ.get("TAG", "for1m")  # for1m 用 rules.yml；for2m 用 rules-for2m.yml
RULES = {"for1m": "rules.yml", "for2m": "rules-for2m.yml", "debug": "rules-debug.yml"}[TAG]
D, R, E = 30, 120, 30
SERVICEDOWN_RE = ".*(victoriametrics|vmselect|vminsert|vmstorage|vmagent|vmalert|vmsingle|vmalertmanager|vmauth).*"


def snapshot():
    code, j = getjson(f"http://{HOST}:20011/api/v1/alerts")
    cells = {}
    if j:
        for a in j["data"]["alerts"]:
            k = f'{a["name"].split("_")[0]}|I={a["labels"].get("I")}|L={a["labels"].get("L")}'
            cells[k] = a["state"]
    return {"cells": cells}


def query(q):
    _, j = getjson(f"http://{HOST}:20010/api/v1/query?query=" + urllib.parse.quote(q))
    return j["data"]["result"] if j else None


def summarize(path):
    rows = load_jsonl(path)
    start = next(r for r in rows if r.get("mark") == "sampling")["rel"]
    samples = [r for r in rows if "cells" in r]
    out = {}
    for alert in ("Rate2m", "Rate5m"):
        for i in (30, 60, 120):
            for l in (0, 60, 120, 180):
                k = f"{alert}|I={i}|L={l}"
                first, resets, prev = None, 0, None
                for s in samples:
                    st = s["cells"].get(k)
                    if st == "firing" and first is None:
                        first = round(s["rel"] - start)
                    if prev == "pending" and st is None:
                        resets += 1
                    prev = st
                r = 120 if alert == "Rate2m" else 300
                zero = sum(1 for s in samples if s["cells"].get(f"Rate2mZero|I={i}|L={l}") == "firing") if alert == "Rate2m" else None
                out[k] = {"alert": alert, "I": i, "L": l, "R": r, "predicted_ok": r >= i + l - D,
                          "first_firing_s": first, "pending_resets": resets, "samples_with_rate_zero": zero}
    return out


def main():
    c = Compose("at-vm-matrix", os.path.join(HERE, "compose.yml"), env={"RULES": RULES})
    c.down()
    try:
        c("up", "-d", "--quiet-pull")
        wait_http(f"http://{HOST}:20011/-/healthy")
        rec = Recorder(os.path.join(RES, f"matrix-{TAG}.jsonl"))
        rec.mark("sampling", D=D, E=E, duration=DURATION)
        rec.sample_for(DURATION, snapshot)
        # 官方 ServiceDown 的 job regex 對 job="vm" 比不比得到
        sd = {"up_job_vm": query('up{job="vm"}'), "servicedown_selector": query(f'up{{job=~"{SERVICEDOWN_RE}"}}')}
        rec.mark("servicedown-check", **{k: (len(v) if v is not None else None) for k, v in sd.items()})
        rec.f.close()
        if TAG == "debug":  # 保留 vmalert 的逐次 evaluation log，給 check_evals.py 用
            log = subprocess.run(["docker", "compose", "-p", "at-vm-matrix", "logs", "--no-log-prefix", "vmalert"],
                                 capture_output=True, text=True).stdout
            with open(os.path.join(RES, "matrix-debug-vmalert.log"), "w") as f:
                f.writelines(l + "\n" for l in log.splitlines() if "alerting rule" in l and "query returned" in l)
            with open(os.path.join(RES, "matrix-debug-pushes.jsonl"), "w") as f:
                f.write(subprocess.run(["docker", "compose", "-p", "at-vm-matrix", "logs", "--no-log-prefix", "pusher"],
                                       capture_output=True, text=True).stdout)
        summ = summarize(os.path.join(RES, f"matrix-{TAG}.jsonl"))
        json.dump({"cells": summ, "servicedown": {k: (len(v) if v is not None else None) for k, v in sd.items()},
                   "duration_s": DURATION, "D": D, "E": E}, open(os.path.join(RES, f"matrix-{TAG}-summary.json"), "w"), indent=1)
        for k, v in summ.items():
            print(k, v)
    finally:
        if os.environ.get("KEEP") != "1":
            c.down()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "summarize":
        print(json.dumps(summarize(os.path.join(RES, f"matrix-{TAG}.jsonl")), indent=1))
    else:
        main()
