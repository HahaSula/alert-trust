"""依 Prometheus 2.51.2 promql/functions.go 的 extrapolatedRate 與 instantValue，算 scrape 延遲下 rate／irate 的值。

假設：counter 以固定速率 r 增加，sample 時間戳 = scrape 開始 s = k·I，寫入時間 = s + d；
evaluation 在 ts 執行，只看得到 s + d ≤ ts 的 sample；視窗為 (ts − O − R, ts − O]（3.x 左開；這裡邊界不會剛好落在 sample 上）。
輸出：算出來的值 ÷ 真實速率；不足 2 點時為 None（回傳空）。
"""
import sys

def samples(ts, I, d, R, O):
    hi, lo = ts - O, ts - O - R
    out = []
    k = int(lo // I) - 1
    while k * I <= hi:
        s = k * I
        if lo < s <= hi and s + d <= ts:
            out.append(s)
        k += 1
    return out

def rate(ts, I, d, R, O):
    pts = samples(ts, I, d, R, O)
    if len(pts) < 2:
        return None
    first, last = pts[0], pts[-1]
    sampled = last - first
    avg = sampled / (len(pts) - 1)
    thr = avg * 1.1
    to_start = first - (ts - O - R)
    to_end = (ts - O) - last
    ext = sampled
    ext += to_start if to_start < thr else avg / 2
    ext += to_end if to_end < thr else avg / 2
    increase = sampled * 1.0            # r = 1，增量 = 時間差
    return increase * (ext / sampled) / R  # ÷ r（r = 1）

def irate(ts, I, d, R, O):
    pts = samples(ts, I, d, R, O)
    return None if len(pts) < 2 else 1.0  # 最後兩點的增量 ÷ 實際時間差 = r

def table(d, I=120, phases=(15, 45, 75, 105)):
    base = 10 * I  # 任一個 scrape 開始的時間
    rows = []
    for p in phases:
        ts = base + p
        rows.append((p, rate(ts, I, d, 300, 0), irate(ts, I, d, 300, 0), rate(ts, I, d, 600, 0),
                     irate(ts, I, d, 600, 0), rate(ts, I, d, 300, 120)))
    return rows

if __name__ == "__main__":
    d = float(sys.argv[1]) if len(sys.argv) > 1 else 100
    f = lambda v: "空" if v is None else f"{v:.0%}"
    for r in table(d):
        print(f"{r[0]:>4}s | " + " | ".join(f(v) for v in r[1:]))
