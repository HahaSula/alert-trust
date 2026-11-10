---
name: rule-review
description: Review someone's alerting rule files (rule.yml, VMRule, PrometheusRule). Always establishes the monitoring stack, then verifies where every metric the rules read comes from (scrape job in the given Prometheus config, live snapshot, or the user's answer - never assumed), then checks the monitor-of-monitor coverage of the infrastructure, and only then checks per-rule timing contracts. Use whenever the user hands over rule files or asks "are these rules OK / can I trust them". 檢查別人的 rule 檔、審 alert 規則時使用：先確認監控體系、每個 metric 的來源、基礎設施的 monitor of monitor，再查時間契約。
when_to_use: 檢查 rule.yml、審 alert rule、這些規則能不能信、review PrometheusRule／VMRule、rule 時間契約
argument-hint: "<rule files...>"
---

# 檢查 rule 檔

**順序是固定的，不能跳步。**只看 rule 本身的時間契約，會漏掉更根本的問題：rule 讀的 metric 根本沒有人在抓（job 名稱對不上、被 relabel 丟掉），或負責評估的 Prometheus／vmalert 自己變慢、掛掉時沒有人知道。所以先確認監控體系與 metric 來源，再確認監控基礎設施，最後才看 rule。

`$AT` 是這個 plugin 的根目錄（`${CLAUDE_PLUGIN_ROOT}`，沒有時用 `${CLAUDE_SKILL_DIR}/../..`）。直接讀 repo 的 AI 也照同樣的步驟走，見 `AGENTS.md` 的「檢查使用者的 rule 檔」。

## 全程規則

- 唯讀，不改使用者的檔案，除非對方要求。
- 數字標來源：量到的、使用者說的、推算的。
- **不要假定 metric 從哪裡來。**每個 metric 的來源只能是：使用者給的 Prometheus config 裡的 scrape job、live 快照、使用者的回答。目錄卡片寫「這個 metric 通常由 mysqld_exporter 產生」只是提示，不能當成來源。
- **不合規格時要給出路，不要只列 FAIL。**每個不合格的地方都照這個格式回報：「目前規格不合（哪一項、差多少）→ 在目前規格下合格的是這些 → 方案 A：改 rule 配合現在的環境（改成什麼）→ 方案 B：不改 rule，改環境（例如 `scrape_interval` 調到多少、加多少 `query_offset`）」，兩個方案的代價都寫出來，讓使用者選。使用者沒選之前不要替他改檔。
- 一次最多問 3 題；不知道就記「未知」繼續，最後列在「沒有確認的部分」。
- 結論要引用目錄：`$AT/catalog/` 的訊號 id、契約 id，以及網站連結 `https://hahasula.github.io/alert-trust/components/<卡片>/#<訊號>`。

## 第 1 步：確認監控體系（必做）

先回答這三個問題，答案寫進 `env/<name>.yaml`（格式見 `$AT/env/env.example.yaml`）。能從檔案判斷的先判斷，判斷不了才問。

| 問題 | 怎麼判斷 | 為什麼重要 |
|---|---|---|
| **評估這些 rule 的是 Prometheus 還是 vmalert？版本？** | `type: vlogs`、`eval_delay`、`concurrency` 是 vmalert；`query_offset`、`keep_firing_for` 是 Prometheus；VMRule／PrometheusRule CRD 名稱；問使用者 | 時間契約的公式不同：Prometheus `R ≥ (2+k)·I + d`，vmalert `R ≥ I + L − D`；Prometheus 2.53 以前沒有 `query_offset` |
| **資料從哪裡來？** | Prometheus 自己 scrape，或 vmagent → VictoriaMetrics（single／cluster），log 來自 VictoriaLogs | 決定要用哪些元件卡 |
| **有沒有外部觀察者？告警走哪台 Alertmanager？** | 問使用者 | 沒有外部觀察者時，monitor of monitor 全部只在自己身上 |

## 第 2 步：每個 metric 的來源（必做）

使用者給了 Prometheus config（`prometheus.yml`，含 `scrape_config_files`）時一定要讀；沒給就先要。跑：

```bash
python3 "$AT/tools/check_metric_sources.py" --rules <使用者的 rule 檔...> --config prometheus.yml \
    [--answers env/<name>.sources.yaml] [--live snapshot/<id>/metric_jobs.json] --out report
```

- 它把每條 rule 的每個 metric selector 對到 config 的 scrape job，分成：recording rule、內建（`up`、`scrape_*`、`ALERTS`）、live 看得到、使用者確認、**job 對不上**、**被 `metric_relabel_configs` 丟掉**、live 查不到、要問人。
- **「要問人」照報告最後的問題逐題問使用者**（一次最多 3 題，同一個 exporter 的 metric 合成一題），答案寫進 `env/<name>.sources.yaml`（格式見 `$AT/env/sources.example.yaml`）後再跑一次。使用者不知道的標「未知」，列在「沒有確認的部分」。
- 能連到 Prometheus 時先跑 `collect.py`，用它的 `metric_jobs.json` 給 `--live`，以實際資料為準。
- 另外對照 config 的 `rule_files`：使用者給的 rule 檔是不是真的由這台 Prometheus 評估；`alerting.alertmanagers` 指向哪台 Alertmanager。
- 「job 對不上」「被丟掉」「live 查不到」的 rule 永遠回傳空、永遠不會響，**在第 4 步之前就要回報**，時間契約對它們沒有意義。

## 第 3 步：監控基礎設施的覆蓋（必做，先於時間契約）

1. 依第 1 步選元件卡：`$AT/catalog/components/<元件>.yaml`。Prometheus 體系看 `prometheus`、`alertmanager`、`observer`；VM 體系看 `vmagent`、`victoriametrics`、`vmalert`、`alertmanager`、`observer`，有 log 再加 `victorialogs`。
2. 把使用者的 rule 檔（以及外部觀察者的 rule 檔，如果有）轉成快照，跑覆蓋檢查：

```bash
python3 "$AT/tools/rules_to_snapshot.py" --out snapshot --ruler <受測 ruler id> <使用者的 rule 檔...> [--ruler <觀察者 id> <觀察者的 rule 檔...>]
python3 "$AT/tools/check_coverage.py" --env env/<name>.yaml --snapshot snapshot --out report
```

   能連到 ruler 時改用 `collect.py` 蒐集，並加 `--live` 確認規則的 selector 真的查得到資料。

3. **先報告第 1 級訊號的缺口**，例如 Prometheus 體系常見的：
   - Scrape 耗時接近 `scrape_interval`（`scrape_duration_seconds`；需要 federate 或在受測 Prometheus 上評估）
   - Rule group 跑不完（`prometheus_rule_group_last_duration_seconds` ÷ `prometheus_rule_group_interval_seconds`）、group 停止評估（`prometheus_rule_group_last_evaluation_timestamp_seconds`）
   - WAL 寫入失敗、送不到 Alertmanager、Watchdog 與外部 dead man's switch
   - VM 體系：vmalert 查詢錯誤、partial response、唯讀、`-rule.resendDelay`

   使用者無法提供觀察者的規則時，逐項問「這個訊號有沒有人在看、在哪台評估」，答案標成「使用者說的」。
4. 基礎設施的缺口沒有處理前，後面時間契約的結論要加上一句：「評估這些 rule 的 ruler 本身沒有被監控，以下結論在它變慢或掛掉時不成立。」

## 第 4 步：每條 rule 的時間契約

需要的數字：`scrape_interval`（S）、`scrape_timeout`（τ）、尖峰 scrape 耗時（d_max）與平常耗時（p99）、最長連續失敗次數（決定 k）、exporter 自己的查詢期限（X）、`evaluation_interval`（E）、尖峰 group 耗時（T）；vmalert 另外要 `eval_delay`（D）與資料延遲（L）。

**使用者還沒有基準值時（例如不知道 S、τ 該設多少）**，先依 `$AT/docs/tools/baseline.md` 的步驟量 7 天以上的耗時，再用 `fit_contracts.py --baseline --job-duration <job>=<d_max> --job-p99 <job>=<p99> [--job-failrun <job>=<N>]` 算出每個 job 建議的 S、τ、X、E、k 與最短視窗，跟使用者確認後再檢查 rule。官方沒有 timeout 餘裕的建議值；0.8、0.9 是 alert-trust 依契約訂的預警線，回報時要說明。S 從使用者給的 Prometheus config 讀（含每個 job 自己的 `scrape_interval`）；d、T 從 live 快照（`peaks.json`）或問使用者。拿不到就標成假設，工具會用最壞情況並在報告開頭寫出來。

先跑：

```bash
python3 "$AT/tools/fit_contracts.py" --rules <rule 檔...> --stack prometheus|vmalert \
    --scrape-interval <S> --scrape-duration <d_max> --scrape-timeout <τ> --eval-interval <E> [--group-duration <T>] \
    [--scrape-p99 <p99>] [--job-timeout <job>=<τ>] [--exporter-deadline <job>=<X>] [--job-failrun <job>=<N>] \
    [--job-interval <job>=<S>] [--job-duration <job>=<d>] [--query-offset <O>] [--k 1] \
    [--latency <L> --eval-delay <D>] --out report [--write-fitted report/fitted-rules.yml]
```

報告 `contracts-fit.md` 的順序就是回報的順序：

1. **環境本身合不合**：`d ≤ S`、`τ < S`、`X < τ`、耗時數據有沒有被 τ 截斷、p99 ≤ 0.8τ、`E ≥ S`、`T ≤ E`、lookback。不合時開頭就說「目前規格不合」，並說怎麼改（例如 mysql job 的 scrape 耗時比 `scrape_interval` 長，要把 `scrape_interval` 調到多少，或降低 exporter 的查詢耗時）。
2. **在目前規格下合格的 rule**，附邊際。
3. **不合格的 rule，兩個方向**：方案 A 改 rule（工具算出最短的合格視窗，`--write-fitted` 另存一份改好的 rule 檔，原檔不動）；方案 B 不改 rule、改環境（該 job 的 `scrape_interval` 至多多少，或加 `query_offset` ≥ d）。
4. **一次讓所有 rule 合格的環境設定**（依 job 分列）。

兩個方案的代價要一起說：A 的視窗變長，短暫尖峰反應變慢；B 的 `scrape_interval` 變短會增加 scrape 負載，`query_offset` 會讓每條 rule 都晚判斷。讓使用者選。

工具只處理視窗契約與環境檢查；下面各 pack 的其他契約（resend／EndsAt、Alertmanager 告警視窗、exporter 期限）照各自的 SKILL.md：

- Prometheus：照 `$AT/skills/prometheus/SKILL.md` 第 2 節。
- vmalert／VictoriaLogs：照 `$AT/skills/victoriametrics/SKILL.md` 第 1、2 節。
- Alertmanager 的告警視窗：照 `$AT/skills/alertmanager/SKILL.md` 第 2 節。
- 資料庫 exporter 的規則：照 `$AT/skills/database-targets/SKILL.md`。

公式與名詞：`$AT/catalog/contracts.yaml`、`$AT/catalog/terms.yaml`，網站的信任契約頁與名詞頁。

## 第 5 步：規則本身的寫法

- `x == 0`、`x != 1` 這類資料消失時回傳空的寫法（`tools/validate.py` 的 presence lint 規則同樣適用）。
- 關鍵 alert 與昂貴的 recording rule 放在不同 group。

## 回報順序

1. 監控體系（第 1 步的答案與來源）
2. 讀不到資料的 rule：job 對不上、被丟掉、live 查不到（第 2 步）
3. 基礎設施的 monitor of monitor 缺口（第 3 步，第 1 級優先）
4. 時間契約不成立的 rule（第 4 步）
5. 寫法問題（第 5 步）
6. 沒有確認的部分（含第 2 步標「未知」的 metric 來源）
