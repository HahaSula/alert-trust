# 檢查 rule 檔的流程

**輸出是建議，不是判決。**第一次回覆就給只看檔案說得出的建議（永遠不會響或一直在響的 rule、job 對不上、監控系統本身沒被監控、timeout 設定），缺的資料先用保守假設並寫明，再逐輪補問、更新同一份報告。手上只有部分資料時，見[從哪裡開始](start.md)。

拿到別人的 rule 檔時，**先確認監控體系與每個 metric 的來源，再確認監控基礎設施，最後才看每條 rule**。只看 rule 的時間契約，會漏掉更根本的兩類問題：

- rule 讀的 metric 根本沒有人在抓：job 名稱對不上、被 `metric_relabel_configs` 丟掉。這種 rule 永遠回傳空、永遠不會響。
- 負責評估這些 rule 的 Prometheus 或 vmalert 自己變慢、掛掉、寫不進去時，沒有人知道。

<!-- figure: rule-review-flow -->

**圖**　`rule-review` 的五個步驟。第 3 步和第 4 步之間是一道關卡：基礎設施的第 1 級缺口沒有處理前，後面的結論都要加註。
{: .at-caption }

## 怎麼跑

用 Claude Code plugin（安裝 `alert-trust-core`，資料庫另裝 `alert-trust-<db>`，見[參與與擴充](../contributing.md#plugins)）：

```text
/alert-trust-core:rule-review my-rules/*.yml
```

rule 裡有資料庫的 metric 時，它依 `catalog/map.yaml` 把每個資料庫交給對應的 `/alert-trust-<db>:<db>`；沒裝的只做通用的正確觀測檢查，並告訴你要裝哪個 plugin。

不用 skill、自己跑工具時，先用一鍵入口（盤點 → metric 來源 → lint → 時間契約）：

```bash
python3 tools/review.py --rules my-rules/*.yml --config prometheus.yml --alertmanager alertmanager.yml --out report
```

單獨跑某一步時，加 `--inventory report/inventory.yaml` 共用同一份盤點：

```bash
# 第 2 步：每個 metric 的來源（有 prometheus.yml 就一定要給）
python3 tools/check_metric_sources.py --rules my-rules/*.yml --config prometheus.yml --out report
# 回答報告最後的問題，寫進 env/<name>.sources.yaml（格式見 env/sources.example.yaml），再跑一次
python3 tools/check_metric_sources.py --rules my-rules/*.yml --config prometheus.yml --answers env/<name>.sources.yaml --out report

# 第 3 步，只有檔案時（離線）
python3 tools/rules_to_snapshot.py --out snapshot --ruler prom-a my-rules/*.yml --ruler meta meta-rules/*.yml
python3 tools/check_coverage.py --env env/<name>.yaml --snapshot snapshot --out report

# 第 2、3 步，連得到 ruler 時（以實際資料為準）
python3 tools/collect.py --out snapshot --prom prom-a=http://localhost:9090
python3 tools/check_metric_sources.py --rules my-rules/*.yml --config prometheus.yml --live snapshot/prom-a/metric_jobs.json --out report
python3 tools/check_coverage.py --env env/<name>.yaml --snapshot snapshot --out report --live
```

```bash
# 第 4 步：時間契約；不合格時同時給「改 rule」與「改環境」兩個方向
python3 tools/fit_contracts.py --rules my-rules/*.yml --stack prometheus \
    --scrape-interval 30s --scrape-duration 20s --eval-interval 30s --job-interval mysql=60s --job-duration mysql=45s \
    --out report --write-fitted report/fitted-rules.yml
```

第 4 步的公式見[信任契約](../contracts.md)，也可以用[契約計算器](calculator.md)手動代入數字。

還沒有基準值（不知道 `scrape_interval`、`scrape_timeout` 該設多少）時，先看 [scrape 與 evaluation 的基準值怎麼定](baseline.md)，再用 `fit_contracts.py --baseline` 由量到的耗時推出建議值。

## 第 4 步：不合規格時給兩個方向

`fit_contracts.py` 的報告不是只列 FAIL，而是照這個順序：

1. **環境本身合不合**：scrape 耗時有沒有超過 `scrape_interval`、rule group 有沒有在 interval 內跑完、lookback。不合時開頭就寫「目前規格不合」與怎麼改。
2. **在目前規格下合格的 rule**，附邊際。
3. **不合格的 rule**：
    - 方案 A：改 rule 配合現在的環境。工具算出最短的合格視窗，`--write-fitted` 另存一份改好的 rule 檔，原檔不動。代價是視窗變長，短暫尖峰反應變慢。
    - 方案 B：不改 rule，改環境。該 job 的 `scrape_interval` 至多多少，或加 `query_offset` ≥ 尖峰 scrape 耗時（Prometheus 2.53 起）。代價是 scrape 負載增加，或每條 rule 都晚判斷。
4. **一次讓所有 rule 合格的環境設定**，依 job 分列。

**視窗是刻意選的時候。**`[1m]` 這種短視窗常常就是要「最近 1 分鐘」的語意，拉長會改變 rule 的意思。在 rule 加 annotation `keep_range: "true"`，或用 `--keep-range <rule 名稱>`，方案 A 就不會改它，報告改列保留語意的做法：

| 做法 | 效果 | 代價 |
|---|---|---|
| 縮短該 job 的 `scrape_interval` | 視窗內 sample 變多，「最近 R」才真的有 R 的精度 | scrape 負載與 sample 數增加 |
| 加 `query_offset` ≥ d | 視窗不必多涵蓋 d | 每條 rule 晚 d 判斷 |
| Prometheus 3.x `anchored`（實驗功能，`--enable-feature=promql-extended-range-selectors`） | `rate`、`increase`、`delta`、`changes`、`resets` 用視窗前的最後一個 sample 當起點，視窗內只要 1 個 sample；不外推，值是兩端的實際差 | 實驗功能，語法和行為可能改；只能在開了 flag 的 Prometheus 上用 |

視窗內實際看得到的 sample 數約是 R ÷ I。`scrape_interval` 比 R 的一半還長時，「最近 R」的精度其實由 `scrape_interval` 決定，不是由 R 決定。各函式需要幾個 sample 見[各 range 函式需要的視窗](../contracts.md#range-functions)。

讀 recording rule 的 rule，要調的是上游 recording rule 所在 group 的 `interval`，不是 `scrape_interval`；工具會分開標示。

### 「一次 scrape 失敗會讓 `for` 重新計時」要怎麼測 {#for-reset-test}

說「一次失敗就重新計時、改成 `last_over_time` 就不會」時，證據是一對 promtool 測試：原本的 rule 在失敗下被重置、提案的寫法照常 firing。**模擬 scrape 失敗要用 `stale`，不要用 `_`。**

| `input_series` 的值 | 對應的情況 | instant selector 在下一次評估 |
|---|---|---|
| `stale` | scrape 失敗：Prometheus 寫入 staleness marker | 立刻變空，pending 被刪、`for` 從頭算 |
| `_` | 這個時間點沒有 sample（不是失敗） | lookback 內仍看得到前一個 sample，**不會**重置 |

用 `_` 的測試裡原本的 rule 也不會被重置，所以證明不了宣稱的事。範本：[for-reset.rules.yml](templates/for-reset.rules.yml)、[for-reset.test.yml](templates/for-reset.test.yml)，`promtool test rules for-reset.test.yml`（Prometheus 2.54.1 與 3.13.5 的 promtool 都通過）。把兩條 rule 換成要比較的寫法，保留三組測試：`stale` 下原本的 rule 被重置、`stale` 下提案的照常 firing、`_` 的對照。

## 第 2 步：metric 的來源不能用猜的

`check_metric_sources.py` 把每條 rule 裡的每個 metric selector，對到 Prometheus config 的 scrape job：

| 狀態 | 意思 | 要做什麼 |
|---|---|---|
| job 對不上 | selector 的 `job` 條件在 config 裡找不到任何 job | rule 永遠回傳空，先回報 |
| 被 relabel 丟掉 | 符合的 job 用 `metric_relabel_configs` 丟掉了這個 metric | 同上 |
| live 查不到 | 有 live 快照，但符合的 job 裡沒有這個 metric | 同上 |
| 要問人 | 從檔案判斷不了 | 照報告最後的問題問使用者 |
| 使用者確認、live 看得到 | 有來源 | 繼續 |
| recording rule、內建 | 由 recording rule 產生，或是 `up`、`scrape_*`、`ALERTS` | 繼續 |

**目錄只是提示，不是來源。**目錄卡片知道 `mysql_up` 通常由 mysqld_exporter 產生，但你的 mysqld_exporter 掛在哪個 job、有沒有被 relabel，只有 config、實際資料或你自己知道。所以沒有回答之前，工具一律標「要問人」，不會自己假定。

同一個 exporter 的 metric 合成一題，例如「這 12 個 metric 通常來自 mysqld_exporter，實際是哪個 job？」。`relabel_configs` 或 `honor_labels` 會改寫 job 時，config 裡的 `job_name` 不等於最後的 job label，工具會先問改寫之後實際有哪些 job 名稱。

## 為什麼順序不能反過來

- **體系決定公式。**Prometheus 與 vmalert 的視窗契約不同，Prometheus 2.53 以前也沒有 `query_offset`。不先確認體系，第 4 步會用錯公式。
- **讀不到資料的 rule，時間契約沒有意義。**job 對不上的 rule，視窗算得再對也不會響。
- **基礎設施壞了，rule 一起失效而且不報錯。**實驗中 scrape 比 interval 慢時，一條永遠成立的 alert 跑 25 分鐘都沒有 firing，`rule_evaluation_failures_total` 是 0、`up` 是 1。只有 scrape 耗時與 rule group 耗時的告警看得出來。
- **離線也能做第 2、3 步。**有 config 就能找出 job 對不上與被丟掉；`rules_to_snapshot.py` 把 rule 檔轉成快照，覆蓋檢查器就能找出「缺少」與「只在自己身上」。「規則在但沒資料」要連到真的 ruler（`--live`）。
