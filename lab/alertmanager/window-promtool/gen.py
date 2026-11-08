#!/usr/bin/env python3
"""產生 promtool test rules 用的測試檔：單一 group 持續失敗，counter 每 60 分鐘加 1（G = 1h）（scrape 30s）。

每個評估時間點、每條 alert 都寫成「預期沒有 alert」。promtool 回報的失敗數＝firing 的點數。
"""
GI_MIN = 60
rules = {
    "Rate5mFor5m": ("rate(fails[5m]) > 0", "5m"),
    "Increase1h": ("increase(fails[1h]) > 0", None),   # 視窗 = G，邊界情況
    "Increase2h": ("increase(fails[2h]) > 0", None),   # 視窗 = 2 × G，建議
}
with open("rules.yml", "w") as f:
    f.write("groups:\n  - name: window\n    interval: 1m\n    rules:\n")
    for name, (expr, for_) in rules.items():
        f.write(f"      - alert: {name}\n        expr: {expr}\n")
        if for_:
            f.write(f"        for: {for_}\n")
values = " ".join(str(i // (GI_MIN * 2)) for i in range(2 * 400))  # 400 分鐘，每 30s 一點
with open("test.yml", "w") as f:
    f.write("rule_files: [rules.yml]\nevaluation_interval: 1m\ntests:\n  - interval: 30s\n")
    f.write(f"    input_series:\n      - series: 'fails'\n        values: '{values}'\n    alert_rule_test:\n")
    for m in range(180, 331):
        for name in rules:
            f.write(f"      - {{eval_time: {m}m, alertname: {name}, exp_alerts: []}}\n")
