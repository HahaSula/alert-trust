# Metric 名稱與規則載入

輕量 lab：確認元件卡引用的 metric 名稱真的由該元件輸出，並讓 `rules/` 的規則在真實的 ruler 上載入一次。4 個小容器，約 1 分鐘，不做負載測試。

```bash
./run.sh
```

結果寫在 `results/`。

## 2026-11-08 的結果

| 項目 | 結果 |
|---|---|
| Prometheus 2.51.2、Alertmanager 0.27.0、vmalert v1.155.0 的 metric 名稱 | 42 個找到；1 個缺少：`vmalert_recording_rules_errors_total`，因為 lab 沒有 recording rule，這個 metric 逐條 rule 產生。已從必要清單移到說明 |
| Prometheus 3.13.5 的 metric 名稱 | 24 個全部找到 |
| `rules/prometheus`、`rules/alertmanager` 在 2.51.2 與 3.13.5 | 各 40 條載入，全部 `health=ok` |
| `rules/victoriametrics` 的 meta 與 canary 在 vmalert v1.155.0 | 13 條載入，全部 `health=ok` |

「external」是 `up`、`scrape_*`、`ALERTS*`、cAdvisor、kube-state-metrics 這類不由元件本身輸出的 metric，這個 lab 不檢查。只在事件發生後才出現的 counter 可能被誤報為缺少，要人工確認。
