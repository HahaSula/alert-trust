# 元件 skill 的共同流程：五段

每個元件 skill（`/alert-trust-<db>:<db>`、`/alert-trust-host:host`）照這份檢查自己的卡片；上層 skill（`selfcheck`、`rule-review`）在元件 plugin 沒裝時，只做這份的「正確觀測」一段（[通用檢查](#generic)）。**知識在目錄裡，不在 skill 裡**：訊號、門檻、證據每次都在執行時從 `catalog/` 讀，不要從 skill 或記憶抄清單。

## 路徑 {#paths}

| 變數 | 裝成 plugin 時 | 在 alert-trust repo clone 裡直接用（不裝 plugin，例如 opencode） |
|---|---|---|
| `$AT`：底下有 `catalog/`、`tools/`、`rules/`、`env/` | `${CLAUDE_PLUGIN_ROOT}`（這個 plugin 的目錄） | repo 根目錄 |
| `$REF`：共用說明（這份、`evidence.md`、`snapshot.md`） | `${CLAUDE_PLUGIN_ROOT}/references` | `plugins/core/references` |
| 同一個 plugin 的其他 skill | `${CLAUDE_SKILL_DIR}/../<skill>/SKILL.md` | `plugins/<plugin 目錄>/skills/<skill>/SKILL.md` |

- 在 repo 裡，每個 plugin 目錄的 `catalog`、`tools`、`rules`、`env`、`references` 是指回 repo 的 symlink；從 git marketplace 安裝時會被複製成實際檔案，所以 `${CLAUDE_PLUGIN_ROOT}/catalog/...` 兩種情況都讀得到。開始前用 `ls "$AT/catalog/map.yaml"` 確認路徑對。
- **`docs/` 只在 repo clone 裡有**，plugin 不帶網站原始檔。skill 寫 `$AT/docs/<路徑>.md#<錨點>` 而檔案不存在時，改讀網站 `https://hahasula.github.io/alert-trust/<路徑>/#<錨點>`（`index.md` 對到目錄本身；非 `.md` 的檔案在網站同一個路徑）。例：`docs/contracts.md#exporter-deadline` → `https://hahasula.github.io/alert-trust/contracts/#exporter-deadline`。
- 使用者的檔案（env、快照、報告）放在**目前工作目錄**，不寫進 `$AT`。

## 全程規則 {#rules}

- **唯讀**：只讀 ruler 的 API 與快照。**不要連資料庫或主機**，不執行 SQL、不改 exporter 帳號權限；需要資料庫端的資訊（GRANT、版本、複寫狀態）時，給使用者要執行的唯讀查詢，請他自己跑、遮蔽後貼回。
- **隱私**：DSN、帳號、主機名、資料庫名稱不寫進報告，用 env 的 target id 指稱。不要請使用者貼 DSN、密碼或 token。
- **數字標來源**：`量測`（快照檔與欄位，或查詢式）、`使用者說`、`推算`（公式與代入值）。目錄的門檻是小規模實驗的經驗值，不是對方環境的事實。
- **引用證據**：每個結論附卡片訊號或契約連結、`evidence` 等級與 `refs` 註腳，格式見 `$REF/evidence.md`。沒有依據的判斷寫「推論」。
- 每輪最多 3 個單一問題；用使用者的語言。

## 先決條件 {#prerequisites}

1. **監控體系已確認**：評估規則的是 Prometheus 還是 vmalert、版本、資料從哪裡來、有沒有外部觀察者。沒有 `env/<name>.yaml` 時先用 core 的 `env-profile`，或依 `rule-review` 第 1 步問清楚。
2. **基礎設施的覆蓋已檢查**：已經跑過 `$AT/tools/check_coverage.py`（沒有 API 時先用 `$AT/tools/rules_to_snapshot.py` 把 rule 檔轉成快照），並先回報第 1 級缺口。

使用者只丟 rule 檔要你「檢查」時，改走 core 的 `rule-review`，不要直接從這份開始。

## 讀卡片 {#read-card}

1. `$AT/catalog/map.yaml` 的 `cards.<卡片 id>`：`plugin`、`page`（元件頁）、`guide`（資料庫的「怎麼監控」頁）、`architecture`，以及五段各有哪些 id：`usage`、`observe`、`misuse`、`bad_queries`、`blind`。這是五段的目錄；內容在卡片裡。
2. 卡片本身：`$AT/catalog/targets/<id>.yaml`（資料庫）或 `$AT/catalog/components/<id>.yaml`（監控鏈、主機）。要用的欄位：`model`、`role`、`versions_checked`、`silent_failures`、`usage`、`contracts`、`signals`（`section`、`tier`、`signal`、`metrics`、`key_metrics`、`applies_when`、`versions`、`rules`、`upstream.note`）、`bad_queries`、`blind_spots`（`covered_by`、`options`）、`not_applicable_aspects`。
3. 資料庫卡另讀 `$AT/catalog/target-pattern.yaml`：每張 target 卡都要回答的標準問題（`id`、`min_tier`、`question`、`why`、可能有 `contract`）。卡片的 `model` 決定用哪一組：`exporter`（沒寫就是 exporter）用 `required_signals`，`native`（資料庫自己提供 /metrics）用 `required_signals_native`。卡片裡同 id 或 `aliases` 的訊號就是答案。
4. 契約：`$AT/catalog/contracts.yaml`（資料庫多半是 `exporter-deadline`，表在 `$AT/docs/contracts.md#exporter-deadline`）；exporter 的連線池、collector 排隊、錯誤指標的差異在 `$AT/docs/architecture/exporters.md`。
5. 規則範本：訊號 `rules:` 指到的 `$AT/rules/...`。範本的 `job="mysql"` 這類是佔位符。

env 檔的 `type` 是卡片 id，或 `$AT/tools/check_coverage.py` 的 `TYPE_ALIASES` 裡的別名（例如 `valkey` → `redis_exporter`、`postgresql` → `postgres_exporter`）。目錄沒有對應卡時，只用 target-pattern 的骨架逐題檢查，報告標明「沒有專屬卡」。

## 快照裡要讀的檔案 {#snapshot}

env 檔 target 的 `scraped_by` 是抓它的 ruler id。讀那個 ruler 的 `<scraped_by>/targets.json`（實際 job 名稱、`scrapeInterval`、`scrapeTimeout`、`lastError`、`lastScrapeDuration`、`scrapeUrl` 是否是 `/probe`）、`peaks.json`（`scrape_duration_peak{job}`、`up_min{job}`）、`metric_jobs.json`，以及**所有** ruler 的 `rules.json`（規則可能在觀察者上）。結構見 `$REF/snapshot.md`。

能連 ruler 時，用 GET 查詢補量測（唯讀）：

```promql
count by (__name__) ({job="<job>"})                    # 現在有哪些 metric 族
<collector_success_metric>{job="<job>"} == 0           # 哪個 collector 失敗；metric 名稱從卡片讀
max_over_time(scrape_duration_seconds{job="<job>"}[7d])
```

## 五段 {#sections}

依序做，每段的項目以 `map.yaml` 列的 id 為準。`applies_when`、`versions` 依環境篩選；卡片的 `versions_checked` 和使用者的版本不同時，標「未在此版本驗證」。

### 1 架構 {#architecture}

讀卡片 `architecture` 指到的架構頁段落（`$AT/docs/...`，沒有就讀網站），確認這個環境有哪些角色、拓撲與會留下的痕跡（例如複寫、叢集角色與數量）。這決定後面哪些 `applies_when` 適用。拓撲不知道時列為第一題。

### 2 正確使用 {#usage}

逐項對照卡片 `usage`（設定、帳號權限、exporter 參數、版本限制）。能從快照或使用者給的設定判斷就判斷；判斷不了的寫成唯讀查詢請使用者跑。`usage` 只寫原始碼推理、本機小實驗或標明的官方文件，回報時照它的 `evidence` 標。

### 3 正確觀測 {#observe}

看到的數字是不是真的：卡片 `section: observe` 的訊號，加上 target-pattern 的標準問題。

**model: exporter（`required_signals`）**

1. **exporter-up**：有沒有 `up{job=<實際 job>} == 0` **加上** `absent(...)` 的規則？只看 exporter 自己的 `*_up`（上游 mixin 常見）是缺口：exporter 停機時那條 series 整條消失，規則回傳空。
2. **db-up**：有沒有看 exporter 輸出的資料庫連線 metric（名稱從卡片讀）？它和 exporter-up 要一起看。
3. **collectors-present**：服務停機或帳號被鎖時，collector 的結果整組消失。規則要寫成 `up == 1 unless on(job, instance) <collector_success>`，`== 0` 的寫法會回傳空。
4. **collector-success**：每個 collector 都成功嗎？權限不足、表或 extension 不存在、collector 參數沒開時，只有那部分消失，依賴它的規則安靜地回傳空。卡片的 `silent_failures` 列了已知陷阱。
5. **scrape-budget**（契約 `exporter-deadline`）：`exporter 自己的查詢期限 < scrape_timeout < scrape_interval`。`scrapeTimeout`、`scrapeInterval` 從 `targets.json`（量測）；exporter 的期限依卡片與 `docs/contracts.md#exporter-deadline`。違反時症狀從「資料庫停機」變成「exporter 停機」。建議 `scrape_duration_seconds > 0.8 × scrape_timeout` 預警。
6. **probe-blast-radius**：`targets.json` 的 `scrapeUrl` 是 `/probe` 時適用。一台 multi-target exporter 掛掉，背後所有 target 同時消失；規則要分得出是 exporter 還是服務。
7. **exporter-query-timeout、replication-lag、role-detection**：照卡片的訊號與 `why`。

**model: native（`required_signals_native`）**

資料庫自己回應 `/metrics`，沒有 exporter 這一層。**`up` 只代表 process 回應了 HTTP**：失去 quorum、失去 leader、副本唯讀時，`up` 常常仍是 1（卡片的 lab 參考寫了在哪些資料庫量到）。

1. **endpoint-up**：`up == 0` 加 `absent`。
2. **health-second-path**（最重要）：三個訊號交叉判斷，缺任何一個都要報缺口：`up`；blackbox 從另一條路徑探測健康端點（端點與要比對的回應內容從卡片讀；有些端點永遠回 200 或只看關機旗標，卡片會寫哪些不能用）；資料庫自己的健康 metric（例如 leader、quorum、唯讀副本數；有些只在 leader 上有值，卡片會寫）。判斷表：`up` 0、探測 1 → scrape 本身的問題；兩者都 0 → process 或主機；`up` 1、健康 metric 異常或探測失敗 → 服務不健康。
3. **metric-families-present**：關鍵 metric 每個 instance 都在嗎？版本改名、功能沒開時整組消失。
4. **scrape-budget**：`scrape_duration_seconds` 對 `scrape_timeout`，加上 `sample_limit`／`body_size_limit`：超過時整次 scrape 失敗、所有 series 一起消失。
5. **component-count**：每種角色的節點數對嗎（預期數量來自第 1 段的拓撲）。
6. **endpoint-blocking**：產生 metric 時會不會被資料庫內部的鎖卡住；照卡片的結論。
7. **replication-lag、role-detection**：照卡片。

**job 名稱**：上游規則與 alert-trust 範本都依 job 名稱比對；名稱不符時規則照常載入、永遠回傳空。用 `targets.json` 的實際 job 名稱對照 env 檔的 `targets[].job` 與所有 `rules.json` 的 selector。`check_coverage.py --live` 回報 `rule_no_data` 時先查這個。

### 4 不正確使用時的觀測（含 bad_queries） {#misuse}

誤用或故障時看得到什麼：卡片 `section: misuse` 的訊號（依 `applies_when` 篩選），以及 `bad_queries`：每個寫得不好的查詢（資料庫的 SQL／命令；監控系統的 PromQL／MetricsQL／LogsQL 與 rule）對到哪些訊號。逐項回答「這個環境有沒有規則或面板看得到」，沒有的附卡片 `rules:` 的範本。上游或社群規則要直接套用前，先看訊號的 `upstream.note`：多張卡都量到官方規則 metric 名稱錯、label 不存在、或以 leader 為前提而永遠不響。

### 5 目前無法觀測的痕跡 {#blind}

列卡片 `blind_spots`：有痕跡但 exporter／endpoint 沒輸出的部分。有 `covered_by` 的寫明要用哪張卡或哪個工具補（例如主機卡）；`options` 只一行點到為止，不展開設計。

## 深度界線 {#depth}

範圍只到 exporter 或 endpoint 實際輸出的那一層：資料庫到 exporter 送出的查詢與 metric（或內建 `/metrics`）以及資料庫提供的系統表；主機只到 node_exporter、cAdvisor、kube-state-metrics 的輸出；監控鏈到元件自己的 `/metrics`、API 與設定。更下層的實作只引用架構頁說明「痕跡從哪來」，不往下挖。

## 通用檢查（元件 plugin 沒裝時） {#generic}

上層 skill 發現某個 target 的 plugin 沒裝時：

1. 照[讀卡片](#read-card)讀 `map.yaml` 與卡片（catalog 由 core 提供，一定讀得到）。
2. 只做第 3 段[正確觀測](#observe)：標準問題與 `section: observe` 的訊號。第 2、4、5 段不做，報告寫「未檢查：安裝 `<plugin>` 後執行 `/<plugin>:<skill>`」，plugin 名稱取 `map.yaml` 的 `cards.<id>.plugin`。

## 訪談（最多 3 個單一問題一輪，已知的跳過）

依「答案會改變幾個結論」排序。常見的有：拓撲（決定 `applies_when` 與 component-count）；exporter 帳號權限（請使用者自己跑唯讀查詢，遮蔽後貼回）或有沒有 blackbox 探測健康端點；exporter 期限相關 flag 或 native endpoint 的開關。各 plugin 的 skill 會列這個資料庫的具體題目。

## 回報 {#output}

每張卡一份五段結果：

| 段 | 項目（`map.yaml` 的 id） | 狀態（covered／self_only／rule_no_data／missing／partial／not_applicable／needs_input／未檢查） | 依據（量測／使用者說／推算，附訊號連結與 refs） | 建議 |
|---|---|---|---|---|

- 第 3 段每列對到 target-pattern 的標準問題 id；第 4 段 bad_queries 用 `case` 當項目。
- 缺口附卡片 `rules:` 指到的範本，提醒換掉佔位符 job 名稱與門檻，並用 `promtool check rules` 驗證。
- 最後 1 到 3 句「這張卡最重要的發現」，結果交回呼叫的上層 skill（`selfcheck` 或 `rule-review`），由它匯集成「建議先處理的 3 件事」。
