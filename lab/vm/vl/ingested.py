"""`victorialogs#ingested` 的規則範本要用的兩個 counter：寫入請求數與實際收進的筆數。

只起 VL single。依序送 3 種 Content-Type（不帶、x-www-form-urlencoded、application/stream+json），每次 5 行，
記錄 `vl_http_requests_total{path="/insert/jsonline"}`、`vl_rows_ingested_total`、`vl_http_errors_total` 的變化，
寫到 results/vl-ingested.json。
  python3 vl/ingested.py
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import Compose, metrics, msum, wait_http, HOST
from run import post_raw, logsql

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
PORT = 20060


def counters():
    ms = metrics(PORT)
    return {"requests": msum(ms, "vl_http_requests_total", 'path="/insert/jsonline"') or 0.0,
            "errors": msum(ms, "vl_http_errors_total", 'path="/insert/jsonline"') or 0.0,
            "rows_ingested": msum(ms, "vl_rows_ingested_total") or 0.0}


def main():
    c = Compose("at-vm-vl", os.path.join(HERE, "compose.yml"), env={"OUT": RES})
    c.down()
    out = []
    try:
        c("up", "-d", "--quiet-pull", "vl")
        wait_http(f"http://{HOST}:{PORT}/health")
        for i, ct in enumerate([None, "application/x-www-form-urlencoded", "application/stream+json"]):
            src = f"ing{i}"
            body = "".join(json.dumps({"src": src, "msg": f"line {n}"}) + "\n" for n in range(5)).encode()
            before = counters()
            status = post_raw(PORT, body, ct)
            time.sleep(3)
            after = counters()
            out.append({"content_type": ct, "http_status": status[0],
                        "delta": {k: after[k] - before[k] for k in after},
                        "query_count": logsql(PORT, f"_stream:{{src={src}}} | stats count() c")[1]})
            print(out[-1], flush=True)
    finally:
        json.dump(out, open(os.path.join(RES, "vl-ingested.json"), "w"), indent=1, ensure_ascii=False)
        c.down()


if __name__ == "__main__":
    main()
