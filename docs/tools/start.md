# 從哪裡開始

不用一次準備好所有東西。手上有什麼就從哪一階開始，每一階都會先拿到能用的建議，再告訴你補上什麼可以看得更多。任何一階都可以停下來。

alert-trust 的輸出都是**建議**：哪個設定在什麼條件下會失效、依據是原始碼還是實驗、改的話有什麼代價。要不要改由你決定。

| 階段 | 你給什麼 | 拿到什麼 | 下一步可以補什麼 |
|---|---|---|---|
| 1 | 幾個查詢的數字：每個 job 的 scrape 耗時（p99、最大值）、`up == 0` 次數 | 每個 job 建議的 `scrape_interval`、`scrape_timeout`、exporter 期限、`evaluation_interval`，以及 `rate` 類、`*_over_time` 類視窗至少要多長 | `prometheus.yml` |
| 2 | `prometheus.yml` | 哪些 metric 根本沒人在抓（job 名稱對不上、被 `metric_relabel_configs` 丟掉）；`scrape_timeout` 的設定問題 | rule 檔 |
| 3 | rule 檔 | 永遠不會響或一直在響的 rule；每條 rule 的視窗夠不夠，不夠時「改 rule」「改環境」「保留視窗」三種做法 | 唯讀連線或快照 |
| 4 | Prometheus／vmalert 的唯讀 URL，或快照 | 監控系統本身有沒有被監控（monitor of monitor）的缺口，依影響排序 | 決定採用 |
| 5 | 決定要補哪些 | 依缺口挑出的[規則範本](https://github.com/HahaSula/alert-trust/tree/main/rules)、外部觀察者與 dead man's switch 的設定建議 | 定期重跑，比較前後 |

## 第 1 階：只有數字

在 Prometheus 上跑（job 名稱可以先換成代號）：

```promql
max by (job) (quantile_over_time(0.99, scrape_duration_seconds[7d]))
max by (job) (max_over_time(scrape_duration_seconds[7d]))
sum by (job) (sum_over_time((up == bool 0)[7d:30s]))     # 30s 換成那個 job 的 scrape_interval
```

```bash
python3 tools/fit_contracts.py --baseline --stack prometheus --scrape-interval 30s --eval-interval 30s \
    --job-duration mysql=12s --job-p99 mysql=8s --job-failrun mysql=1 --out report
```

推導方式見 [scrape 與 evaluation 的基準值怎麼定](baseline.md)。

## 第 2、3 階：有設定檔與 rule 檔

```bash
python3 tools/review.py --rules my-rules/*.yml --config prometheus.yml --alertmanager alertmanager.yml --out report
```

先看 `report/summary.md`（建議先處理的 3 件事），再看 `report/inventory.md`：這是工具從你的檔案理解的環境，哪裡不對就直接改 `report/inventory.yaml`（改過的標 `confirmed: true`），再加 `--merge report/inventory.yaml` 跑一次，改過的值會保留。

完整流程見[檢查 rule 檔](rule-review.md)。用 Claude Code 的話，直接 `/alert-trust:rule-review my-rules/*.yml`，它會先給只看檔案就說得出的建議，再逐輪問你。

## 第 4 階：連得到監控系統

```bash
python3 tools/collect.py --out snapshot --prom prom-a=http://localhost:9090
python3 tools/check_coverage.py --env env/<name>.yaml --snapshot snapshot --out report --live
```

`env/<name>.yaml` 的格式見 [env.example.yaml](https://github.com/HahaSula/alert-trust/blob/main/env/env.example.yaml)。這個檔案描述你的環境，已在 `.gitignore`，不會被 commit。用 Claude Code 的話，`/alert-trust:selfcheck` 會引導你建立它。
