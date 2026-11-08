"""VictoriaLogs lab：空資料的 stats count()、Content-Type、log rule 視窗、cluster 缺 vlstorage、single 唯讀。"""
import http.client as httpclient, json, os, sys, time, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import Compose, Recorder, http, metrics, msum, mmax, vmalert_alerts, vmalert_rule_health, am_alerts, wait_http, HOST

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
S = float(os.environ.get("SCALE", "1"))


def logsql(port, q):
    code, body = http(f"http://{HOST}:{port}/select/logsql/query", data=urllib.parse.urlencode({"query": q}).encode())
    return code, body.strip()[:300]


def post_raw(port, body, content_type):
    """content_type=None 時完全不送 Content-Type。"""
    conn = httpclient.HTTPConnection(HOST, port, timeout=5)
    headers = {"Content-Length": str(len(body))}
    if content_type is not None:
        headers["Content-Type"] = content_type
    conn.request("POST", "/insert/jsonline?_stream_fields=src&_msg_field=msg", body=body, headers=headers)
    r = conn.getresponse()
    out = (r.status, r.read().decode()[:200])
    conn.close()
    return out


def ingested(port):
    return msum(metrics(port), "vl_rows_ingested_total") or 0.0


def content_type_test(rec):
    results = []
    for i, ct in enumerate([None, "application/x-www-form-urlencoded", "application/stream+json"]):
        src = f"cttest{i}"
        body = "".join(json.dumps({"src": src, "msg": f"line {n}"}) + "\n" for n in range(5)).encode()
        before = ingested(20060)
        status = post_raw(20060, body, ct)
        time.sleep(3)
        after = ingested(20060)
        code, cnt = logsql(20060, f"_stream:{{src={src}}} | stats count() c")
        r = {"content_type": ct, "http_status": status[0], "response": status[1],
             "rows_ingested_delta": after - before, "query_count": cnt}
        results.append(r)
        rec.mark("content-type", **r)
    return results


def snapshot():
    vs, vf = metrics(20060), metrics(20064)
    qcode, _ = logsql(20064, "_time:5m flow:canary | stats count() c")
    single_alerts = vmalert_alerts(20061)
    return {
        "single": {k: {"firing": v.get("firing", 0), "activeAt": v["activeAt"]} for k, v in (single_alerts or {}).items()},
        "cluster": {k: v.get("firing", 0) for k, v in (vmalert_alerts(20062) or {}).items()},
        "cluster_health": vmalert_rule_health(20062),
        "single_health": vmalert_rule_health(20061),
        "am": am_alerts(20063),
        "cluster_query_status": qcode,
        "cluster_reachable": msum(vf, "vl_insert_remote_is_reachable"),
        "single_read_only": mmax(vs, "vl_storage_is_read_only"),
        "single_rows_ingested": msum(vs, "vl_rows_ingested_total"),
    }


def readonly_only():
    """只重跑 VL single 唯讀（資料放在具名 volume，重建容器不會遺失）。"""
    c = Compose("at-vm-vl", os.path.join(HERE, "compose.yml"), env={"OUT": RES})
    c.down()
    rec = Recorder(os.path.join(RES, "vl-readonly.jsonl"))
    run = lambda sec: rec.sample_for(sec * S, snapshot)
    try:
        c("up", "-d", "vl", "vmalert-single", "alertmanager", "vlstorage-1", "vlstorage-2", "vlfront")
        wait_http(f"http://{HOST}:20060/health")
        c("--profile", "gen", "up", "-d", "loggen")
        rec.mark("steady"); run(150)
        rec.mark("readonly-on"); c("up", "-d", "vl", env={"VLMINFREE": "1000000000000000"}); time.sleep(5)
        rec.mark("readonly-probe", ingest=post_raw(20060, b'{"src":"probe","msg":"x"}\n', "application/stream+json"),
                 canary_count=logsql(20060, "_time:5m flow:canary | stats count() c"))
        run(150)
        rec.mark("readonly-off"); c("up", "-d", "vl", env={"VLMINFREE": "10000000"}); run(60)
        rec.mark("end", canary_count_10m=logsql(20060, "_time:10m flow:canary | stats count() c"))
    finally:
        rec.f.close()
        if os.environ.get("KEEP") != "1":
            c.down()


def main():
    c = Compose("at-vm-vl", os.path.join(HERE, "compose.yml"), env={"OUT": RES})
    c.down()
    open(os.path.join(RES, "loggen.jsonl"), "w").close()
    rec = Recorder(os.path.join(RES, "vl.jsonl"))
    run = lambda sec: rec.sample_for(sec * S, snapshot)
    try:
        c("up", "-d", "--quiet-pull")
        wait_http(f"http://{HOST}:20060/health"); wait_http(f"http://{HOST}:20064/health")
        time.sleep(3)
        rec.mark("empty-count", single=logsql(20060, "* | stats count() c"), cluster=logsql(20064, "* | stats count() c"),
                 single_filtered=logsql(20060, "* | stats count() c | filter c:>0"))
        content_type_test(rec)
        c("--profile", "gen", "up", "-d", "loggen")
        rec.mark("steady"); run(300)
        rec.mark("vlstorage-2-down"); c("stop", "vlstorage-2")
        rec.write({"ingest_during_down": post_raw(20064, b'{"src":"probe","msg":"x"}\n', "application/stream+json")})
        run(240)
        rec.mark("vlstorage-2-up"); c("start", "vlstorage-2"); run(150)
        rec.mark("readonly-on"); c("up", "-d", "vl", env={"VLMINFREE": "1000000000000000"}); run(150)
        rec.mark("readonly-off"); c("up", "-d", "vl", env={"VLMINFREE": "10000000"}); run(60)
        rec.mark("end")
    finally:
        rec.f.close()
        if os.environ.get("KEEP") != "1":
            c.down()


if __name__ == "__main__":
    readonly_only() if sys.argv[1:] == ["readonly"] else main()
