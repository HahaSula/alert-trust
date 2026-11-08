"""實驗組合與模型預測。每組：S（scrape_interval）、d（回應延遲）、R（range）、O（offset）、E（evaluation_interval）、F（for）。"""
COMBOS = [
    # id,        S,   d,   R,   O,   F    說明
    ("s120-d100-r5m",        120, 100, 300,   0, 180),  # 圖 2、圖 3 的條件
    ("s120-d100-r5m-o2m",    120, 100, 300, 120, 180),  # 同上加 offset 2m
    ("s120-d100-r10m-o2m",   120, 100, 600, 120, 180),  # 建議寫法
    ("s120-d60-r5m",         120,  60, 300,   0, 180),  # d 變小
    ("s60-d40-r2m",           60,  40, 120,   0, 180),  # R = 2S
    ("s60-d40-r2m-o1m",       60,  40, 120,  60, 180),
    ("s60-d40-r3m",           60,  40, 180,   0, 180),  # R = 3S
    ("s60-d5-r2m",            60,   5, 120,   0, 180),  # d 很小，R = 2S
    ("s30-d25-r1m",           30,  25,  60,   0, 120),
    ("s30-d25-r2m",           30,  25, 120,   0, 120),
]
E = 30


def predict_empty_fraction(S, d, R, O, E=E, steps=1200):
    """沿所有相位平均：evaluation 時刻 ts 看得到的 sample 數 < 2 的比例。
    Sample 時間戳 = scrape 開始 s；可見條件：ts - O - R < s <= ts - O，且 s + d <= ts（已寫入）。"""
    empty = 0
    for i in range(steps):
        ts = 10_000 + (i / steps) * S  # 相位均勻分布在一個 scrape 週期內
        k_hi = int((ts - O) // S)
        visible = 0
        for k in range(k_hi - int(R // S) - 2, k_hi + 1):
            s = k * S
            if ts - O - R < s <= ts - O and s + d <= ts:
                visible += 1
        empty += visible < 2
    return empty / steps
