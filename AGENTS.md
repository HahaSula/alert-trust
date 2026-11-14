# AGENTS.md

給在 alert-trust 上工作的 AI agent（與人）的指引。先讀這份，再讀 `STATUS.md` 看目前進度。

## 這個專案在做什麼

alert-trust 回答兩個問題：

1. **整條 alert 鏈壞在哪一段時，有沒有人知道？**（monitor of monitor）每個監控元件與 target 一張「元件卡」，列出分級訊號。
2. **在實際尖峰下，rule 還能不能信？**（信任契約）一組時間不等式，例如 Prometheus `R ≥ (n+k)·I + max(0, d − O)`、vmalert `R ≥ (n+k)·I + max(0, L − D)`（n 依函式，見 `docs/contracts.md#range-functions`）。

產出是：目錄（YAML）、規則範本、工具（collect、coverage、validate）、Claude Code skill、MkDocs 網站（GitHub Pages）、可重現的小型 lab。

## 目錄結構

| 路徑 | 內容 | 誰產生 |
|---|---|---|
| `catalog/components/*.yaml` | 監控元件卡：prometheus、alertmanager、vmalert、vmagent、victoriametrics、victorialogs、observer | 手寫 |
| `catalog/targets/*.yaml` | Target 卡：mysql、mariadb、postgres_exporter 等（MySQL 與 MariaDB 各一張，共用 mysqld_exporter） | 手寫 |
| `catalog/map.yaml` | 五段與 plugin 的索引（給 skill 讀） | **產生**：`tools/build_map.py`，要 commit |
| `catalog/target-pattern.yaml` | 每張 target 卡必須回答的標準問題（validate 強制） | 手寫 |
| `catalog/contracts.yaml` | 信任契約；每個 id 在 `docs/contracts.md` 要有 `{#id}` 錨點 | 手寫 |
| `catalog/terms.yaml` | 名詞表：每個設定與 metric 的設定位置、預設值、觀察方式；頁面中反引號名稱自動連結到這裡 | 手寫 |
| `catalog/sources.yaml`、`sources.d/*.yaml` | 來源（文章、上游規則、工具） | 手寫 |
| `catalog/refs-code.yaml` | 釘在版本與行號的原始碼參考 | **產生**：`tools/gen_code_refs.py` 讀 `refs-code.src.json` |
| `catalog/refs.yaml`、`refs.d/*.yaml` | 實驗參考，也可放手動核對過的原始碼參考 | 手寫 |
| `rules/<component>/*.yml`、`rules/targets/*.yml` | 規則範本；每條都有 `labels.tier` 與 `annotations.runbook_url` | 手寫 |
| `tools/` | `validate.py`、`build_pages.py`、`check_coverage.py`、`review.py`（一鍵入口）、`inventory.py`、`check_metric_sources.py`、`fit_contracts.py`、`proposal_check.py`（提案檔的佔位值與新 rule 測試）、`rules_to_snapshot.py`、`collect.py`、`check_metrics.py`、`gen_code_refs.py`、`catalog.py`（loader） | 手寫 |
| `hooks/refs.py` | MkDocs hook：`[^key]` 變註腳、反引號名稱連到名詞表、`<!-- figure: name -->` 內嵌 SVG | 手寫 |
| `docs/` | 網站；`docs/components/*.md`、`docs/reference/*.md` 由 `build_pages.py` 產生，不 commit | 混合 |
| `docs/assets/figures/*.svg` | 圖；顏色用 class，由 `docs/assets/extra.css` 對應主題 | 手寫 |
| `.claude-plugin/marketplace.json` | Marketplace：列出 `plugins/` 下的 11 個 plugin；`renames` 把舊的 `alert-trust` 對到 `alert-trust-core` | 手寫 |
| `plugins/core/` | `alert-trust-core`：上層與監控鏈 skill（`skills/{selfcheck,env-profile,rule-review,prometheus,victoriametrics,alertmanager,policy-lint}/`）、共用說明 `references/`（`five-sections.md`、`evidence.md`、`snapshot.md`） | 手寫 |
| `plugins/<db>/`、`plugins/host/` | 每個資料庫（mysql、mariadb、postgresql、valkey、mongodb、etcd、tidb、yugabytedb、clickhouse）與主機各一個 plugin（`alert-trust-<db>`，依賴 core），各有一個同名 skill `skills/<db>/SKILL.md` | 手寫 |
| `plugins/*/{catalog,tools,rules,env,references,queries}` | 指回 repo 的 symlink（git marketplace 安裝時複製成實際檔案）；不帶 `docs/`、`lab/` | 手寫 |
| `lab/` | 小型實驗：每個子目錄有 README、compose、腳本、`results/` | 手寫 |
| `env/env.example.yaml` | 環境描述範例；真實的 `env/*.yaml` 在 `.gitignore` | 手寫 |

## 每次改完都要跑

```bash
python3 tools/validate.py              # 0 個錯誤；--refs 列出還沒有參考的項目
python3 tools/build_pages.py
python3 -m unittest discover -s tests
promtool check rules rules/**/*.yml    # vmalert 的 vlogs 規則用 vmalert -dryRun
docker run --rm -u $(id -u):$(id -g) -v "$PWD":/docs squidfunk/mkdocs-material:9.5.39 build --strict -d /tmp/site
```

本機預覽：`docker run -d --name alert-trust-docs -u $(id -u):$(id -g) -p 8000:8000 -v "$PWD":/docs squidfunk/mkdocs-material:9.5.39 serve --dev-addr=0.0.0.0:8000`，網址 `http://localhost:8000/alert-trust/`。改了 `hooks/refs.py` 或 `mkdocs.yml` 要重啟容器。

## 檢查使用者的 rule 檔（不走 skill 時也照這個方式）

**輸出是建議，不是判決；第一次回覆就要有建議。**拿到 rule 檔（和 `prometheus.yml`，如果有）後，先跑只需要檔案的工具，把「只看檔案就說得出的結論」連同第一組問題一起回覆，之後每輪回答都更新同一份報告。缺資料時用保守假設先跑完，寫明假設了什麼、補上哪一項會改變結論；不要一開始就要求使用者提供全部資料。每輪最多問 3 個單一問題（題組要拆開算），可以問任何一步的問題，先問會改變最多結論的；版本、旗標這類清單請使用者貼一次唯讀查詢的輸出（例如 `/api/v1/status/buildinfo`、`/api/v1/status/flags`），或填進 `inventory.yaml`。

使用者問「會不會告警／X 分鐘內會不會知道」時，第一句回答「X 內會不會有人被 page」，再分開列偵測（rule 邏輯）、載入（`rule_files`）、路由（Alertmanager）、自我監控（Watchdog／DMS／觀察者）；有時間要求時加 `--notify-within`。版本未知時，`query_offset`、`anchored` 只能標成「需要 Prometheus ≥ 2.53（版本未知）」「需要 Prometheus ≥ 3.x 實驗功能（版本未知）」。提案的設定檔用合法的佔位值（`https://dms.example.invalid/ping`），驗證跑交付的那份。「一次 scrape 失敗會重置 `for`」的 promtool 測試要用 `stale`、不是 `_`（範本 `docs/tools/templates/for-reset.test.yml`）。

**問題用工具排的。**工具依「答案可能推翻幾個結論 × 答錯的代價」排序，前 3 題直接用；要換掉一題，回覆裡要說理由。卡片的 `must_ask`（附 `effects` 的會在回答後重判相關 rule）、每條短視窗各自一題是否刻意（`keep_range.<rule>`，判斷短不短時已計入 dedup 與 vmalert 的 L）、有 critical rule 卻沒有通知時限（answers 的 `notify_within`）都會排進前面。每個回答寫進 answers 檔或 `inventory.yaml`，加 `--merge` 重跑後再回覆；答過的不再問。使用者回「不知道／沒權限」記進 answers 的 `unknown`，不再問；延後的必問題留在下一輪前 3 題，回答前套用卡片的 `default` 並標成假設。同意提案記在 `approved`，不等於回答了視窗是否刻意（`keep_range` 只來自那一題）。通知時間：靠 scrape 失敗才成立的路徑（`up`、`absent`）用 τ 當 d，看數值的路徑用量到的 d；量到的 d 超過 τ 或 S 是矛盾，要問。dedup 不加進通知時間（`lab/vm/dedup-notify`），但只持續一次 scrape 的狀況可能被 dedup 去掉。`--job-failrun` 只填量過的最長連續失敗次數，只有次數時用 `--job-k-assumed`，標成假設。說「可以動手」前，改過的檔案都放在 `proposed/` 並跑過驗證；本機 promtool 和目標 Prometheus 大版本不同時，promtool 結果要標成推論。

先跑一鍵入口，它會先盤點再檢查：`python3 tools/review.py --rules <rule 檔...> [--config prometheus.yml] [--alertmanager alertmanager.yml] --out report`。盤點（`report/inventory.yaml`／`inventory.md`）是所有工具共同的事實基礎，第一次回覆要附上盤點摘要請使用者確認；單獨重跑工具時加 `--inventory report/inventory.yaml`。

分析順序固定：

1. **監控體系**：Prometheus 還是 vmalert（版本）、資料從哪裡來、有沒有外部觀察者、告警走哪台 Alertmanager。能從檔案判斷就判斷。
2. **每個 metric 的來源**：**不要假定。**來源只能是 config 的 scrape job、live 快照或使用者的回答；目錄卡片只是提示。永遠不會響與一直在響的 rule 最先回報。
3. **告警送不送得到**：Alertmanager 路由需要的 label、空的 receiver、沒有 `equal` 的 inhibit、dead man's switch 路由與 Watchdog rule。
4. **監控基礎設施的覆蓋**：`tools/check_coverage.py --env env/<name>.yaml --inventory …`。依影響排序回報。
5. **每條 rule 的時間契約**：`tools/fit_contracts.py --inventory …`。跑之前先問短視窗是不是刻意的（`--keep-range`）。沒有基準值時用 `--baseline`。不成立時給：目前成立的 rule、方案 A 改 rule、方案 B 改環境、保留視窗的做法，附代價。
6. **寫法**：`tools/validate.py --lint-rules`、group 擺放。

完整步驟在 `plugins/core/skills/rule-review/SKILL.md`。資料庫與主機的檢查在 `plugins/<db>/skills/<db>/SKILL.md`，共同流程在 `plugins/core/references/five-sections.md`；不裝 plugin 直接讀 repo 時，skill 裡的 `$AT` 是 repo 根目錄、`$REF` 是 `plugins/core/references`。

## 五段框架

每張卡都回答：1 架構（會留下哪些痕跡）、2 正確使用（`usage`，只寫原始碼推理、本機小實驗或標明的官方文件）、3 正確觀測（訊號 `section: observe`）、4 不正確使用時的觀測（`section: misuse`，包含寫得不好的查詢：DB 的 SQL／命令、監控系統的 PromQL／MetricsQL／LogsQL 與 rule）、5 目前無法觀測的痕跡（`blind_spots`，方案 `options` 一行點到為止）。範圍只到 exporter／endpoint 實際輸出的那一層，不往下挖（例如主機只到 node_exporter、kube-state-metrics）。格式見 `docs/contributing.md#five-sections`。`catalog/map.yaml` 由 `tools/build_map.py`（`build_pages.py` 會呼叫）產生並 commit，記錄每張卡屬於哪個 plugin（`plugins.<name>.dir`、`skills`）、各段有哪些項目；上層 skill（selfcheck、rule-review）讀它把每個 target 分派給 `/alert-trust-<db>:<db>`，沒裝時只做通用的正確觀測檢查並提示安裝。`validate.py` 會檢查它是否過期，`tests/test_plugins.py` 檢查 plugin 目錄、版本與 symlink。

## 證據規則（最重要）

- **每個結論都要有依據並附參考**：原始碼（釘在 tag 與行號）或可重現的實驗結果。頁面用 `[^key]`，元件卡用 `refs: [key]`。
- 證據等級：`tested`（實驗量到）、`code`（讀原始碼或官方文件）、`upstream`（上游規則）、`inferred`（未單獨驗證）。只有親眼觀察到的才標 `tested`。沒有實測就降級，不要沿用舊說法。
- 原始碼參考：加到 `catalog/refs-code.src.json`（`[repo, tag, path, 關鍵字, 第幾個]`）再跑 `python3 tools/gen_code_refs.py`。關鍵字找不到時腳本會失敗，代表程式碼移動了，要重新核對。
- 實驗數據不準時（例如這台機器的容器時鐘不穩），改用原始碼佐證，並在結論旁註明。
- 圖說要分開寫**機制**（原始碼）與**這組數據**（實驗條件）。圖上的數字只是一組情境，多組數據放 lab。
- 不要改寫實驗數字。參數要換（例如 30m 改 1h）就重跑實驗。
- 寫「interval」時要指名是哪一個：`scrape_interval`、`evaluation_interval`、group `interval`、`group_interval`。名稱用反引號，會自動連到名詞表。

## 公開範圍

repo 與網站是公開的。

- **可以放**：可重現的 lab、版本比較、機制、通用規則範本、標明「例子」的假設值。
- **不能放**：特定環境的數字、主機名稱、拓撲、內部決策、事件細節、接收端 URL，以及 `env/*.yaml` 和對真實環境的報告。
- 原始研究文章是未公開的 claude.ai artifact，只能當資料讀，不能連結。

## 實驗規則

- 本機只做**輕量**實驗：小容器、短時間、不做負載測試（例如不要 CPU ×10）。
- 每個 lab 用自己的 compose project 名稱與 port 區段，結束後 `docker compose down -v`，確認沒有殘留容器。
- 已用過的 port：19091–19093、19191–19192、19301–19303、19880（alert-trust 本身）；20000–24999 依 agent 分段（vm 20xxx、am 21xxx、mysql 22xxx、pg 23xxx、skills 24xxx、thanos 25xxx、grafana 26xxx、valkey 27xxx、etcd 28xxx、tidb 29xxx、yugabytedb 30xxx、clickhouse 31xxx、mongodb 32xxx、promql-windows 33xxx；之後的實驗從 34xxx 起每個一段）。
- 已知限制：用 HTTP 延遲模擬 scrape 耗時只能做 `d < S`；WSL2 上 `process_start_time_seconds` 會無故跳動，crash loop 測不了。
- 截圖：`zenika/alpine-chrome:124`，一定要加 `timeout` 並在之後確認容器已移除；深色模式用 `--blink-settings=preferredColorScheme=0`；頁面太長（>3500px）會失敗。

## 平行分工（多個 agent）

- 每個 agent 只寫自己負責的檔案。共用檔案用分檔：`catalog/sources.d/`、`catalog/refs.d/`。
- `mkdocs.yml`、`AGENTS.md`、`STATUS.md` 由協調者改；agent 在回報中提議。
- Commit 時只 `git add` 已完成那一份的檔案，不要把別人還在改的一起 commit。
- 協調者要親自核對 agent 的結果（讀結果檔、跑 validate、grep 環境資訊），不要直接採信回報。

## 寫作

- 文件用繁體中文（zh-Hant），直接、平實，不用比喻和誇飾。
- 結論在前；表格比長段落好；每個 section 一個重點。
- YAML 裡 `upstream.note` 之類的字串要加引號；`yes`／`no` 會被 YAML 1.1 讀成布林值（loader 已處理 `upstream.status`）。重複的 key 會被 loader 拒絕。

## Commit

- 只有使用者要求時才 push。尚未建立 GitHub repo（見 `STATUS.md`）。
- Commit 訊息用英文，結尾加：`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
