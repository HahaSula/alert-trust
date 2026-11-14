"""dedup 對通知時間的影響。python3 run.py [trials] [delay|blip]（預設 delay，每種 4 次，約 20 分鐘；blip 每次約 2 分鐘）。

每次試驗：在隨機時間點讓 target 回 500（失敗路徑）或把 lab_value 改成 1（值路徑），每秒查兩台 vmalert 的
/api/v1/alerts，記錄 alert 第一次出現 pending／firing 的牆上時間；之後還原，等 alert 消失再做下一次。
結束後從兩台 VM 的 /api/v1/export 取出變化前後的 sample（看得到的 sample 時間戳），寫進 results/。"""
import json, os, random, sys, time, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import Compose, http, getjson, wait_http, HOST

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 4
MODE = sys.argv[2] if len(sys.argv) > 2 else "delay"  # delay：down／value 各 N 次；blip：N 次單次 scrape 失敗
VMALERT = {"dedup": 37013, "nodedup": 37014}
VM = {"dedup": 37011, "nodedup": 37012}
TARGET = 37010
RULES = {"down": ["TDown", "TDownFor30s"], "value": ["TValue"]}


def alerts(port):
    code, j = getjson(f"http://{HOST}:{port}/api/v1/alerts")
    if code != 200 or not j:
        return {}
    return {a["name"]: {"state": a["state"], "activeAt": a.get("activeAt")} for a in j["data"]["alerts"]}


def export(port, metric, start, end):
    q = urllib.parse.urlencode({"match[]": f'{metric}{{job="t"}}', "start": int(start), "end": int(end)})
    code, body = http(f"http://{HOST}:{port}/api/v1/export?{q}", timeout=10)
    out = []
    for line in (body or "").splitlines():
        try:
            j = json.loads(line)
            out += [[t / 1000, v] for t, v in zip(j["timestamps"], j["values"])]
        except Exception:
            pass
    return sorted(out)


def trial(kind, f):
    time.sleep(random.uniform(0, 15))  # 隨機相位
    t0 = time.time()
    http(f"http://{HOST}:{TARGET}/" + ("down" if kind == "down" else "value/1"))
    seen = {(m, r, s): None for m in VMALERT for r in RULES[kind] for s in ("pending", "firing")}
    while time.time() - t0 < 300 and any(v is None for v in seen.values()):
        for m, p in VMALERT.items():
            for r, a in alerts(p).items():
                if r in RULES[kind]:
                    for s in ("pending", "firing"):
                        if (m, r, s) in seen and seen[(m, r, s)] is None and (a["state"] == s or (s == "pending" and a["state"] == "firing")):
                            seen[(m, r, s)] = round(time.time() - t0, 1)
        time.sleep(1)
    t1 = time.time()
    http(f"http://{HOST}:{TARGET}/" + ("up" if kind == "down" else "value/0"))
    # 等兩台都沒有 alert
    while time.time() - t1 < 300 and any(alerts(p) for p in VMALERT.values()):
        time.sleep(2)
    time.sleep(20)
    samples = {m: export(VM[m], "up" if kind == "down" else "lab_value", t0 - 45, t1 + 15) for m in VM}
    first = {}
    for m, ss in samples.items():
        bad = [t for t, v in ss if t >= t0 - 0.5 and ((kind == "down" and v == 0) or (kind == "value" and v == 1))]
        first[m] = round(bad[0] - t0, 1) if bad else None
    rec = {"kind": kind, "t0": round(t0, 1), "restore_after_s": round(t1 - t0, 1),
           "first_changed_sample_s": first,
           "seen_s": {f"{m}|{r}|{s}": v for (m, r, s), v in seen.items()},
           "samples_rel": {m: [[round(t - t0, 1), v] for t, v in ss] for m, ss in samples.items()}}
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    f.flush()
    print(kind, rec["first_changed_sample_s"], rec["seen_s"], flush=True)


def latest(port, metric, since):
    ss = export(port, metric, since, time.time() + 5)
    return ss[-1] if ss else None


def blip(f):
    """一次 scrape 失敗：看到 vm-nodedup 寫入第一個 up=0 就馬上恢復，之後 120 秒看兩台 vmalert 有沒有 TDown。"""
    time.sleep(random.uniform(0, 15))
    t0 = time.time()
    http(f"http://{HOST}:{TARGET}/down")
    while time.time() - t0 < 40:
        x = latest(VM["nodedup"], "up", t0 - 1)
        if x and x[0] >= t0 - 0.5 and x[1] == 0:
            break
        time.sleep(0.3)
    t1 = time.time()
    http(f"http://{HOST}:{TARGET}/up")
    seen = {m: None for m in VMALERT}
    while time.time() - t0 < 120:
        for m, p in VMALERT.items():
            if seen[m] is None and "TDown" in alerts(p):
                seen[m] = round(time.time() - t0, 1)
        time.sleep(1)
    samples = {m: [[round(t - t0, 1), v] for t, v in export(VM[m], "up", t0 - 45, t0 + 90)] for m in VM}
    zeros = {m: [t for t, v in ss if v == 0] for m, ss in samples.items()}
    rec = {"kind": "blip", "t0": round(t0, 1), "restore_after_s": round(t1 - t0, 1), "zeros_s": zeros,
           "tdown_seen_s": seen, "samples_rel": samples,
           # 30s dedup 區間裡的位置：0 sample 的時間戳 mod 30（VictoriaMetrics 以 Unix 時間對齊的區間）
           "zero_mod30": [round((t0 + t) % 30, 1) for t in zeros["nodedup"]]}
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    f.flush()
    print("blip", zeros, seen, rec["zero_mod30"], flush=True)


def main():
    c = Compose("at-vm-dedup-notify", os.path.join(HERE, "compose.yml"))
    c.down()
    try:
        c("up", "-d", "--quiet-pull")
        for p in VMALERT.values():
            wait_http(f"http://{HOST}:{p}/-/healthy")
        time.sleep(90)  # 先累積幾次 scrape
        out = "blips.jsonl" if MODE == "blip" else "trials.jsonl"
        with open(os.path.join(RES, out), "w") as f:
            f.write(json.dumps({"start": time.time(), "versions": c("images")}, ensure_ascii=False) + "\n")
            for i in range(N):
                if MODE == "blip":
                    blip(f)
                    continue
                for kind in ("down", "value"):
                    trial(kind, f)
            for m, p in {**{f"vmalert-{k}": v for k, v in VMALERT.items()}, **{f"vm-{k}": v for k, v in VM.items()}}.items():
                _, body = http(f"http://{HOST}:{p}/metrics")
                ver = [l for l in (body or "").splitlines() if l.startswith("vm_app_version")]
                _, flags = http(f"http://{HOST}:{p}/flags")
                f.write(json.dumps({"component": m, "version": ver, "flags": [l for l in (flags or "").splitlines()
                                    if any(x in l for x in ("dedup", "evalDelay", "evaluationInterval", "eval.", "datasource.lookback", "search.latencyOffset"))]},
                                   ensure_ascii=False) + "\n")
    finally:
        c.down()


if __name__ == "__main__":
    main()
