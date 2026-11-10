---
name: rule-review
description: Review someone's alerting rule files (rule.yml, VMRule, PrometheusRule). Advice first - the first reply already gives what the files alone show (rules that can never fire, job mismatches, missing self-monitoring of the ruler, scrape durations near the timeout) together with the first batch of questions; then it refines the same report round by round. Analysis order stays fixed - monitoring stack, where every metric comes from (never assumed), monitor-of-monitor coverage, per-rule timing contracts, rule style - and every finding is a recommendation with evidence and cost, never a verdict. Use whenever the user hands over rule files or asks "are these rules OK / can I trust them". 檢查別人的 rule 檔、審 alert 規則時使用：第一次回覆就給從檔案看得出的建議，再逐輪補問與更新。
when_to_use: 檢查 rule.yml、審 alert rule、這些規則能不能信、review PrometheusRule／VMRule、rule 時間契約、scrape 該設多少
argument-hint: "<rule files...>"
---

# 檢查 rule 檔

**輸出是建議，不是判決。**alert-trust 能證明的是「這樣設定，在什麼條件下會失效」；要不要改、往哪個方向改，取決於使用者的偵測需求、成本與 rule 的語意。每個發現都寫成：契約或訊號 → 依據（證據等級）→ 建議 → 代價，讓使用者選。

**分析順序固定，對話不用等。**分析一定照第 1～5 步的順序（體系 → metric 來源 → 基礎設施 → 時間契約 → 寫法），因為後面的結論建立在前面之上。但**第一次回覆就要給建議**：先做「第 0 輪」，把只看檔案就說得出的結論交出去，同時附上第一組問題；之後每輪回答都更新同一份報告。

`$AT` 是這個 plugin 的根目錄（`${CLAUDE_PLUGIN_ROOT}`，沒有時用 `${CLAUDE_SKILL_DIR}/../..`）。直接讀 repo 的 AI 也照這份走，見 `AGENTS.md` 的「檢查使用者的 rule 檔」。

## 全程規則

- 唯讀，不改使用者的檔案，除非對方要求。
- 數字標來源：量到的、使用者說的、推算的、假設的。
- **不要假定 metric 從哪裡來。**來源只能是：使用者給的 Prometheus config 裡的 scrape job、live 快照、使用者的回答。目錄卡片寫「這個 metric 通常由 mysqld_exporter 產生」只是提示。
- **缺資料不要卡住。**用下面「不知道時的假設」先跑完，報告開頭寫明假設了什麼、補上哪一項會改變哪個結論。
- **每輪最多問 3 題，但可以問任何一步的問題。**優先問「答案會改變最多結論」的題目，例如第 4 步要的耗時數字，在第一輪就可以一起問。
- 不合契約時給出路：在目前環境下成立的是哪些 → 方案 A 改 rule → 方案 B 改環境 → 視窗是刻意的時候怎麼保留語意，代價都寫出來。使用者沒選之前不要替他改檔。
- 結論引用目錄：`$AT/catalog/` 的訊號 id、契約 id，網站連結 `https://hahasula.github.io/alert-trust/components/<卡片>/#<訊號>`、`https://hahasula.github.io/alert-trust/contracts/#<契約>`。

## 第 0 輪：只看檔案，第一次回覆就給

拿到檔案後，先不問問題，**先盤點、再檢查**。一個指令跑完全部：

```bash
python3 "$AT/tools/review.py" --rules <rule 檔...> [--config prometheus.yml|vmagent 設定 --config-kind prometheus|vmagent] \
    [--alertmanager alertmanager.yml] [--answers env/<name>.sources.yaml] --out report
```

它會依序跑：盤點（`tools/inventory.py`，產生 `report/inventory.yaml` 與 `inventory.md`）→ metric 來源 → presence lint → 時間契約（沒有數字時用 Prometheus 預設值並寫明）→ 覆蓋（給 `--env` 時），並寫出 `report/summary.md`：建議先處理的 3 件事、盤點確認、假設、下一輪的問題。之後要單獨重跑某個工具時，加 `--inventory report/inventory.yaml`，所有工具都從同一份盤點取資料。

**盤點是共同的事實基礎。**`inventory.md` 列出從檔案理解的環境：每個 job 的 interval 與實際生效的 timeout、target label 從哪來、被丟掉的 metric、每條 rule 對到哪個 job、需要哪些 label、會被送到哪個 receiver、哪些元件沒被抓。每個值標來源（檔案、預設、提示、使用者、未知）。第一次回覆要附上盤點摘要，請使用者確認「這是我從檔案理解的環境，哪裡不對？」。使用者的更正寫進盤點檔（標 `confirmed: true`），之後用 `--merge` 重跑會保留。

第一次回覆的格式：

1. **建議先處理的 3 件事**：依影響排序。常見的有：
   - 永遠不會響的 rule（job 對不上、被 `metric_relabel_configs` 丟掉、要求 config 沒加的 label）。
   - 一直在響的 rule（`absent()` 包住對不上的 job）。
   - 評估這些 rule 的 Prometheus／vmalert 沒有任何自我監控（沒有 Watchdog、沒有外部 dead man's switch、Alertmanager 沒被 scrape）；config 沒有 `rule_files` 或 `alerting`。
   - 告警送不到該送的人：路由需要的 label rule 沒有（例如 critical 只會寄信，或被送進空的 receiver）、inhibit 沒有 `equal`、有 dead man's switch 路由卻沒有 Watchdog rule。
   - config 裡 `scrape_timeout` 與 `scrape_interval` 的關係、明顯太短的視窗。
   使用者問了具體問題（例如「失去 quorum 會不會告警」）時，**第一句就直接回答**，再列這 3 件事。
2. **盤點摘要**（`inventory.md` 的重點）：從檔案判斷出的監控體系與環境，判斷不了的部分標「未知」，請使用者更正。
3. **我做了哪些假設**，以及補上哪一項會改變哪個結論。
4. **第一組問題**（最多 3 題）。通常是：
   - 版本與有沒有外部觀察者（決定公式與 monitor of monitor 的結論）。
   - 每個 job 的 scrape 耗時 p99／最大值與 `up == 0` 次數（查詢見 `$AT/docs/tools/baseline.md#procedure`），可以一次貼回。
   - 短視窗（例如 `[1m]`）是不是刻意的。

沒有 config 時，第 0 輪仍然可以給：presence 寫法、明顯太短的視窗（用假設的 S）、沒有自我監控的規則；並在問題裡要 config。

## 不知道時的假設

| 不知道 | 先用 | 補上後會改變什麼 |
|---|---|---|
| Prometheus 版本 | 3.x（有 `query_offset`） | 2.53 以前沒有 `query_offset`，方案 B 少一個選項 |
| 外部觀察者 | 沒有 | monitor of monitor 的結論 |
| scrape 耗時 d | 最壞情況 d = S | 每條 rule 需要的最短視窗 |
| 最長連續失敗 | k = 1 | 最短視窗 |
| exporter 自己的期限 | 卡片寫的預設值 | exporter-deadline 是否成立 |
| 視窗是否刻意 | 不是，但方案 A 要先問過 | 是否提方案 A |
| 拓撲（複本、heartbeat、`/probe` 等） | 不知道，相關訊號標「需要回答」 | 哪些訊號不適用 |

## 第 1 步：監控體系

答案寫進 `env/<name>.yaml`（格式見 `$AT/env/env.example.yaml`；`targets[].job` 填 **config 裡實際的 job 名稱**，`features` 填這個 target 有哪些功能，例如複本、heartbeat、`/probe`，讓不適用的訊號能被排除）。

| 問題 | 怎麼判斷 | 為什麼重要 |
|---|---|---|
| 評估 rule 的是 Prometheus 還是 vmalert？版本？ | `type: vlogs`、`eval_delay`、`concurrency` 是 vmalert；`query_offset`、`keep_firing_for` 是 Prometheus；CRD 名稱；config 的 `rule_files` | 公式不同：Prometheus `R ≥ (n+k)·I + max(0, d − O)`，vmalert `R ≥ (n+k)·I + max(0, L − D)`；n 依函式（`$AT/docs/contracts.md#range-functions`） |
| 資料從哪裡來？ | config 的 `scrape_configs`；vmagent → VictoriaMetrics | 決定用哪些元件卡 |
| 有沒有外部觀察者？告警走哪台 Alertmanager？ | config 的 `alerting`；問 | 沒有外部觀察者時，monitor of monitor 全部只在自己身上 |

## 第 2 步：每個 metric 的來源

`check_metric_sources.py` 把每個 metric selector 對到 config 的 scrape job：recording rule、內建（`up`、`scrape_*`、`ALERTS`）、live 看得到、使用者確認、**job 對不上**、**被丟掉**、**一直在響**（`absent()` 對不上 job）、live 查不到、要問人（含 rule 要的 label config 沒加）。

- 「要問人」的題目工具已經依 exporter 與缺的 label 合併好，照它問。答案寫進 `env/<name>.sources.yaml`（格式見 `$AT/env/sources.example.yaml`；metric 的來源用 `producers`／`metrics`，rule 要的 label 用 `labels: {role: exporter|none}`）再跑一次。
- 能連到 Prometheus 時先跑 `collect.py`，用它的 `metric_jobs.json` 給 `--live`。
- 對照 config 的 `rule_files` 與 `alerting.alertmanagers`；Alertmanager 有沒有被 scrape。
- **全部 job 都對不上時**（例如範本的佔位符 `job="mysql"` 沒改）：建議改成 config 裡的實際名稱，並在後面的步驟**假設已改**繼續分析，報告寫明這個假設。

## 第 3 步：監控基礎設施的覆蓋

```bash
python3 "$AT/tools/rules_to_snapshot.py" --out snapshot --ruler <受測 ruler id> <rule 檔...> [--ruler <觀察者 id> <觀察者的 rule 檔...>]
python3 "$AT/tools/check_coverage.py" --env env/<name>.yaml --inventory report/inventory.yaml --out report [--tiers 1,2]
# 連得到 ruler 時改用 collect.py 的快照：--snapshot snapshot --live
```

- 只比對 metric 名稱不算涵蓋：卡片訊號的 `key_metrics`（例如 native 型的 `probe_success`）沒有任何 rule 用到時是 `partial`。
- 覆蓋檢查會核對規則的 job 是否對得上 env 的 target：名稱相同但 job 對不上的是 `rule_job_mismatch`，不算涵蓋。給 `--config` 時，還會列出沒有任何 scrape job 在抓的元件（例如 Alertmanager）。env 的 `features` 沒有的功能，相關訊號是「不適用」；沒填 `features` 時會變成「需要回答」。
- **依影響排序回報**：Watchdog 與外部 dead man's switch、rule group 停止評估或跑不完、WAL 寫入失敗、送不到 Alertmanager 優先；資料庫的附加訊號（例如 query-exporter）在後。scrape 耗時接近 timeout（Prometheus 卡 `scrape-ratio`、資料庫卡 `scrape-budget`）是第 2 級，要加 `--tiers 1,2` 才會列出；第 0 輪如果已經從數字看到，就直接寫進建議。
- 基礎設施的缺口沒有處理前，第 4 步的結論加一句：「評估這些 rule 的 ruler 本身沒有被監控，以下結論在它變慢或掛掉時不成立。」

## 第 4 步：每條 rule 的時間契約

需要的數字：`scrape_interval`（S）、`scrape_timeout`（τ）、尖峰與平常的 scrape 耗時（d_max、p99）、最長連續失敗（k）、exporter 的期限（X）、`evaluation_interval`（E）、尖峰 group 耗時（T）；vmalert 另外要 `eval_delay`（D）與資料延遲（L）。S、τ 從 config 讀（`--config`）；其他問使用者或從快照取，拿不到就用假設。

**先問視窗是不是刻意的，再跑工具**：像 `[1m]` 常常就是要「最近 1 分鐘」，拉長會改變語意。是的話加 `--keep-range <rule>`（或 rule 的 annotation `keep_range: "true"`）。

```bash
python3 "$AT/tools/fit_contracts.py" --inventory report/inventory.yaml [--job-duration <job>=<d_max>] \
    [--job-p99 <job>=<p99>] [--job-failrun <job>=<N>] [--exporter-deadline <job>=<X>] [--group-duration <T>] \
    [--keep-range <rule>] [--prom-version <版本>] [--dedup-interval <dedup>] [--latency <L> --eval-delay <D>] \
    --out report [--write-fitted report/fitted-rules.yml]
```

- 使用者還沒有基準值（不知道 S、τ 該設多少）時，加 `--baseline`，依 `$AT/docs/tools/baseline.md` 由量到的耗時推出建議值。官方沒有 timeout 餘裕的建議；0.8、0.9 是 alert-trust 依契約訂的預警線，回報時說明。
- 報告順序：環境本身（`d ≤ S`、`τ < S`、`X < τ`、耗時是否被 τ 截斷、p99、`E ≥ S`、`T ≤ E`、lookback）→ 在目前環境下成立的 rule → 不成立的 rule 的方案 A／B → 視窗要保留時的做法（縮短該 job 的 S、`query_offset`、Prometheus 3.x `anchored`）→ 一次讓所有 rule 成立的環境設定。
- 沒有 range 的門檻 rule 加 `for` 時，一次 scrape 失敗就讓 series 變成 stale、`for` 重新計時；工具會提示，工具第 5 節會建議改用 `last_over_time(x[R])`（R 依 `range-window-1sample`）；`keep_firing_for` 只在已經 firing 之後才有幫助。這一項是推論。
- 代價一起說：A 視窗變長，短暫尖峰反應變慢、語意可能改變；B 的 `scrape_interval` 變短會增加 scrape 負載，`query_offset` 讓每條 rule 都晚判斷。

其他契約（resend／EndsAt、Alertmanager 告警視窗、exporter 期限）照各 pack：Prometheus `$AT/skills/prometheus/SKILL.md` 第 2 節；vmalert／VictoriaLogs `$AT/skills/victoriametrics/SKILL.md` 第 1、2 節；Alertmanager `$AT/skills/alertmanager/SKILL.md` 第 2 節；資料庫 `$AT/skills/database-targets/SKILL.md`。

## 第 5 步：規則本身的寫法

- `validate.py --lint-rules` 找出 `x == 0`、`x != 1` 這類資料消失時回傳空的寫法。
- 關鍵 alert 與昂貴的 recording rule 放在不同 group。
- 被 `metric_relabel_configs` 丟掉的 metric 會讓之後加入的範本失效，提醒使用者。

## 回報格式（每輪更新同一份）

1. 建議先處理的 3 件事（依影響排序）
2. 監控體系（第 1 步，標明判斷依據）
3. 讀不到資料或一直在響的 rule（第 2 步）
4. 基礎設施的 monitor of monitor 缺口（第 3 步，第 1 級優先）
5. 時間契約：在目前環境下成立的、不成立的與方案（第 4 步）
6. 寫法建議（第 5 步）
7. 假設與沒有確認的部分，以及下一輪要問的問題
