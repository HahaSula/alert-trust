"""VL cluster 停 1 台 vlstorage 時，vlinsert／vlselect 哪個 metric 會變（`vl_insert_remote_is_reachable` 重測）。

只起 vlstorage-1、vlstorage-2、vlfront（兼 vlinsert／vlselect）。Driver 從 host 直接寫 log、查詢，不用 loggen。
  A 段：只寫 1 條 stream（和 run.py 的 canary 一樣），停「沒有存這條 stream」的那台 vlstorage。
  B 段：寫 8 條 stream（會分到兩台），停同一台。
每 5 秒取 vlfront 的 /metrics 與寫入、查詢的 HTTP 狀態，寫到 results/vl-reachable.jsonl。
  python3 vl/reachable.py
"""
import http.client as httpclient, json, os, sys, time, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import Compose, Recorder, http, metrics, wait_http, HOST

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
FRONT, ST = 20064, {"vlstorage-1": 20066, "vlstorage-2": 20067}
NAMES = ["vl_insert_remote_is_reachable", "vl_insert_remote_send_errors_total", "vl_select_remote_send_errors_total",
         "vl_http_errors_total", "vl_rows_ingested_total"]


def push(port, streams):
    body = "".join(json.dumps({"src": s, "msg": f"{s} {time.time():.1f}"}) + "\n" for s in streams).encode()
    conn = httpclient.HTTPConnection(HOST, port, timeout=5)
    try:
        conn.request("POST", "/insert/jsonline?_stream_fields=src&_msg_field=msg", body=body,
                     headers={"Content-Type": "application/stream+json", "Content-Length": str(len(body))})
        r = conn.getresponse()
        r.read()
        return r.status
    except Exception:
        return None
    finally:
        conn.close()


def query(port, q):
    code, body = http(f"http://{HOST}:{port}/select/logsql/query", data=urllib.parse.urlencode({"query": q}).encode())
    return code, body.strip()[:200]


def front_metrics():
    ms = metrics(FRONT) or {}
    return {f"{n}{l}": v for (n, l), v in ms.items() if n in NAMES}


def main():
    c = Compose("at-vm-vl", os.path.join(HERE, "compose.yml"), env={"OUT": RES})
    c.down()
    rec = Recorder(os.path.join(RES, "vl-reachable.jsonl"))
    streams = ["canary"]

    def run(sec):
        end = time.time() + sec
        while time.time() < end:
            t = time.time()
            ins = push(FRONT, streams)
            q = query(FRONT, "_time:5m | stats count() c")
            rec.write({"insert_status": ins, "query_status": q[0], "query_body": q[1][:120], "front": front_metrics()})
            time.sleep(max(0, 5 - (time.time() - t)))

    def placement(names):
        return {sn: {s: query(p, f"_stream:{{src={s}}} | stats count() c")[1] for s in names} for sn, p in ST.items()}

    try:
        c("up", "-d", "--quiet-pull", "vlstorage-1", "vlstorage-2", "vlfront")
        for p in [FRONT, *ST.values()]:
            wait_http(f"http://{HOST}:{p}/health")
        rec.mark("A-steady"); run(30)
        pl = placement(streams)
        rec.mark("A-placement", placement=pl)
        holder = next((sn for sn, v in pl.items() if '"c":"0"' not in v["canary"]), "vlstorage-1")
        victim = "vlstorage-2" if holder == "vlstorage-1" else "vlstorage-1"
        rec.mark("A-down", victim=victim, holder=holder); c("stop", victim); run(60)
        rec.mark("A-up"); c("start", victim); wait_http(f"http://{HOST}:{ST[victim]}/health"); run(20)
        streams = ["canary"] + [f"s{i}" for i in range(8)]
        rec.mark("B-steady"); run(30)
        rec.mark("B-placement", placement=placement(streams))
        rec.mark("B-down", victim=victim); c("stop", victim); run(60)
        rec.mark("B-up"); c("start", victim); wait_http(f"http://{HOST}:{ST[victim]}/health"); run(30)
        rec.mark("end")
    finally:
        rec.f.close()
        if os.environ.get("KEEP") != "1":
            c.down()


def summarize():
    """每段（A-down、B-down…）的寫入／查詢狀態與 vlfront metric 變化，寫到 results/vl-reachable-summary.json。"""
    rows = [json.loads(l) for l in open(os.path.join(RES, "vl-reachable.jsonl")) if l.strip()]
    out, phase, t0, first = [], None, 0, None
    for r in rows:
        if "mark" in r:
            phase, t0 = r["mark"], r["rel"]
            continue
        if first is None:
            first = r["front"]
        seg = next((s for s in out if s["phase"] == phase), None)
        if seg is None:
            seg = {"phase": phase, "insert_status": {}, "query_status": {}, "reachable": {}, "first_change_s": {}}
            out.append(seg)
        for k in ("insert_status", "query_status"):
            seg[k][str(r[k])] = seg[k].get(str(r[k]), 0) + 1
        for k, v in r["front"].items():
            if k.startswith("vl_insert_remote_is_reachable"):
                seg["reachable"].setdefault(k, set()).add(v)
        prev = seg.get("_prev") or (out[-2]["_last"] if len(out) > 1 else first)
        for k, v in r["front"].items():
            if prev.get(k) != v and k not in seg["first_change_s"]:
                seg["first_change_s"][k] = round(r["rel"] - t0, 1)
        seg["_prev"] = seg["_last"] = r["front"]
        seg["end"] = r["front"]
    for s in out:
        s.pop("_prev", None), s.pop("_last", None)
        s["reachable"] = {k: sorted(v) for k, v in s["reachable"].items()}
    path = os.path.join(RES, "vl-reachable-summary.json")
    json.dump(out, open(path, "w"), indent=1, ensure_ascii=False)
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    summarize() if sys.argv[1:] == ["summarize"] else main()
