# Prometheus：rule group 的執行模式與查詢 slot

驗證[架構頁](../../docs/architecture/prometheus.md)「查詢引擎」「Rule group」兩節，以及圖 rule-group-modes、query-slots 的說明。用官方映像檔，單機 Docker，不需要任何 scrape target。

結果與數字：[results/summary.md](results/summary.md)。

## 需要

- Docker 與 docker compose v2、Python 3（只用標準函式庫）
- 映像檔：`prom/prometheus:v2.55.1`、`v3.13.5`
- 主機 port：21301–21307、21310–21311

## 怎麼跑

```bash
python3 modes.py     # A、B：rule group 執行模式，約 5 分鐘，一次只開一台 Prometheus
python3 slots.py     # C：查詢 slot，約 2 分鐘
docker compose --profile slot down -v
```

## 怎麼讓 rule 有可控的耗時

Prometheus 沒有 sleep 函式。這裡用的方法是**對常數做很細的子查詢**：

```promql
count_over_time(vector(1)[1000s:1ms])
```

子查詢在 1000 秒的範圍內每 1 ms 求一次 `vector(1)`，共 100 萬步，只花 CPU，不讀任何 series，也不需要 target。耗時大致和範圍成正比，在這台 16 核的 lab 機器上 1000s 約 70–150 ms。實際耗時用 `/api/v1/rules` 的 `evaluationTime` 量，不靠預估。

查詢 slot 實驗還需要一個**在關卡 ① 停留很久**的查詢。用的是很長的純量加法 `1+1+…+1`（15000 項）：建立查詢時的前處理耗時隨項數平方成長，約 1–2 秒，而這段在第一次取得 slot 之後、執行之前，不受 `--query.timeout` 限制；之後的執行在 300 ms 時被 timeout 中止。

## 量什麼

- **A、B**：每 0.25 秒讀 `/api/v1/rules`，收集每條 rule 每一輪的開始時間（`lastEvaluation`）與耗時（`evaluationTime`），算同時在跑的 rule 數峰值（全域、每個 group）與 group 一輪的耗時。每條 rule 是一個 recording rule，group interval 3s，觀察 36 秒。
- **C1**：`--query.max-concurrency=1`、`--query.timeout=300ms`。送出 H（長加法），200 ms 後送 P = `vector(1)`，量 P 的耗時與結果，重複 5 次。
- **C2**：同一台的 `SlotProbe`（永遠成立、`for: 1h`）先進入 pending；6 條執行緒持續送 `count_over_time(vector(1)[1500s:1ms])` 20 秒，記錄 rule 的 `health`、`lastError`、`prometheus_rule_evaluation_failures_total` 與 alert 的 `state`／`activeAt`。

## 限制

- 單機、每個情境一次。耗時受主機其他負載影響，A、B 的毫秒數只用來比較同一次實驗內的模式，峰值（同時幾條）才是重點。
- `/api/v1/rules` 只保留每條 rule 最後一次的時間，輪詢間隔（0.25s）遠小於 group interval（3s），每一輪都抓得到。
- 只用無依賴的 recording rule，沒有測 `concurrent-rule-eval` 的依賴判斷（2.x 的問題見架構頁，屬讀碼）。
- C1 裡 P 在關卡 ① 等多久取決於 H 的前處理時間，會隨 CPU 負載變動。
