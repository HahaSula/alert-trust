"""Log 產生器。
- 事件流（src=event）：每 PERIOD 秒一筆，帶 lag=L；_time 是事件發生時間，但延遲 L 秒才送進 VL single。
  PERIOD 不是 30 的倍數，讓事件與 evaluation 的相位一直變。
- canary 流（src=canary）：每 10 秒一筆，立即送進 VL single 與 VL cluster。
每次送出的結果（HTTP 狀態）寫到 /out/loggen.jsonl。"""
import json, os, time, urllib.request
from datetime import datetime, timezone

SINGLE = os.environ.get("SINGLE", "http://vl:9428")
CLUSTER = os.environ.get("CLUSTER", "http://vlfront:9428")
PERIOD = int(os.environ.get("PERIOD", "67"))
LAGS = [int(x) for x in os.environ.get("LAGS", "0,20,45,90").split(",")]
OUT = open("/out/loggen.jsonl", "a")
URL = "/insert/jsonline?_stream_fields=src&_time_field=_time&_msg_field=msg"


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def push(base, lines):
    body = "".join(json.dumps(l) + "\n" for l in lines).encode()
    req = urllib.request.Request(base + URL, data=body, headers={"Content-Type": "application/stream+json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


queue, seq = [], 0
next_event = time.time() + 5
next_canary = time.time()
while True:
    now = time.time()
    if now >= next_event:
        seq += 1
        for lag in LAGS:
            queue.append((next_event + lag, {"_time": iso(next_event), "src": "event", "kind": "event",
                                             "lag": str(lag), "seq": seq, "msg": f"event {seq} lag {lag}"}))
        OUT.write(json.dumps({"t": round(next_event, 1), "type": "event-created", "seq": seq}) + "\n")
        next_event += PERIOD
    due = [q for q in queue if q[0] <= now]
    queue = [q for q in queue if q[0] > now]
    for _, line in due:
        code = push(SINGLE, [line])
        OUT.write(json.dumps({"t": round(now, 1), "type": "event-pushed", "lag": line["lag"], "seq": line["seq"], "code": code}) + "\n")
    if now >= next_canary:
        line = {"_time": iso(now), "src": "canary", "flow": "canary", "msg": "canary"}
        OUT.write(json.dumps({"t": round(now, 1), "type": "canary", "single": push(SINGLE, [line]),
                              "cluster": push(CLUSTER, [line])}) + "\n")
        next_canary += 10
    OUT.flush()
    time.sleep(0.5)
