"""假的資料來源：每個 cell 每 I 秒產生一筆 counter sample（每秒 +1），
但延遲 L 秒才用 /api/v1/import 寫進 VM，時間戳仍是產生的時間。模擬資料間隔 I、寫入延遲 L。"""
import json, os, time, urllib.request

VM = os.environ.get("VM", "http://vm:8428")
IS = [int(x) for x in os.environ.get("IS", "30,60,120").split(",")]
LS = [int(x) for x in os.environ.get("LS", "0,60,120,180").split(",")]
OFFSET = 7  # sample 相位：epoch 秒數 % I == 7，避開和 evaluation 剛好對齊
T0 = int(time.time())
queue = []  # (push_at, I, L, ts)
nxt = {}
for i in IS:
    first = T0 - (T0 % i) + OFFSET
    if first < T0:
        first += i
    nxt[i] = first

while True:
    now = time.time()
    for i in IS:
        while nxt[i] <= now:
            for l in LS:
                queue.append((nxt[i] + l, i, l, nxt[i]))
            nxt[i] += i
    due = [q for q in queue if q[0] <= now]
    queue = [q for q in queue if q[0] > now]
    if due:
        body = "".join(json.dumps({"metric": {"__name__": "lab_m", "I": str(i), "L": str(l)},
                                   "values": [ts - T0], "timestamps": [ts * 1000]}) + "\n"
                       for _, i, l, ts in due)
        try:
            urllib.request.urlopen(urllib.request.Request(VM + "/api/v1/import", data=body.encode()), timeout=5)
            for _, i, l, ts in due:  # 每筆 sample 實際寫入的時間，給 check_evals.py 用
                print(json.dumps({"I": i, "L": l, "ts": ts, "pushed": round(time.time(), 2)}), flush=True)
        except Exception as e:
            print("push error", e, flush=True)
    time.sleep(0.5)
