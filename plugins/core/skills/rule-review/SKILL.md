---
name: rule-review
description: Review someone's alerting rule files (rule.yml, VMRule, PrometheusRule). Advice first - the first reply already gives what the files alone show (rules that can never fire, job mismatches, missing self-monitoring of the ruler, scrape durations near the timeout) together with the first batch of questions; then it refines the same report round by round. Analysis order stays fixed - monitoring stack, where every metric comes from (never assumed), monitor-of-monitor coverage, per-rule timing contracts, rule style - and every finding is a recommendation with evidence and cost, never a verdict. Use whenever the user hands over rule files or asks "are these rules OK / can I trust them". 檢查別人的 rule 檔、審 alert 規則時使用：第一次回覆就給從檔案看得出的建議，再逐輪補問與更新。
when_to_use: 檢查 rule.yml、審 alert rule、這些規則能不能信、review PrometheusRule／VMRule、rule 時間契約、scrape 該設多少
argument-hint: "<rule files...>"
---

# 檢查 rule 檔

**輸出是建議，不是判決。**alert-trust 能證明的是「這樣設定，在什麼條件下會失效」；要不要改、往哪個方向改，取決於使用者的偵測需求、成本與 rule 的語意。每個發現都寫成：契約或訊號 → 依據（證據等級）→ 建議 → 代價，讓使用者選。

**分析順序固定，對話不用等。**分析一定照第 1～5 步的順序（體系 → metric 來源 → 基礎設施 → 時間契約 → 寫法），因為後面的結論建立在前面之上。但**第一次回覆就要給建議**：先做「第 0 輪」，把只看檔案就說得出的結論交出去，同時附上第一組問題；之後每輪回答都更新同一份報告。

路徑（詳見 `$REF/five-sections.md#paths`）：`$AT` 底下有 `catalog/`、`tools/`、`rules/`、`env/`，裝成 plugin（`alert-trust-core`）時是 `${CLAUDE_PLUGIN_ROOT}`，在 alert-trust repo clone 裡直接用（不裝 plugin，例如 opencode）時是 repo 根目錄；`$REF` 是共用說明，plugin 裡是 `${CLAUDE_PLUGIN_ROOT}/references`，repo 裡是 `plugins/core/references`。`$AT/docs/` 只在 repo clone 有，裝成 plugin 時改讀網站 `https://hahasula.github.io/alert-trust/`（`docs/tools/baseline.md` → `tools/baseline/`，範本檔在網站同一個路徑）。其他 core skill 在 `${CLAUDE_SKILL_DIR}/../<skill>/SKILL.md`（repo 裡 `plugins/core/skills/<skill>/SKILL.md`）。直接讀 repo 的 AI 也照這份走，見 `AGENTS.md` 的「檢查使用者的 rule 檔」。

## 全程規則

- 唯讀，不改使用者的檔案，除非對方要求。
- 數字標來源：量到的、使用者說的、推算的、假設的。
- **不要假定 metric 從哪裡來。**來源只能是：使用者給的 Prometheus config 裡的 scrape job、live 快照、使用者的回答。目錄卡片寫「這個 metric 通常由 mysqld_exporter 產生」只是提示。
- **缺資料不要卡住。**用下面「不知道時的假設」先跑完，報告開頭寫明假設了什麼、補上哪一項會改變哪個結論。
- **提問預算：每輪最多 3 個「單一問題」，不是 3 組題組。**一題只問一件使用者要想才答得出的事（例如「`[1m]` 是刻意的嗎？」）；「版本、旗標、dedup、有沒有 VL、Alertmanager 有沒有被抓」這樣的一串算 5 題。依「答案可能推翻幾個結論 × 錯了的代價」排序，**不是依影響幾條 rule**：只影響一兩條、卻會讓建議變成錯誤建議的題目（刻意的視窗、exporter 期限旗標、資料延遲 L）要排在前面。可以問任何一步的問題（例如第 4 步要的耗時數字，第一輪就可以問）。工具（`inventory.md`、`summary.md`）已經照這個分數排好，照它的前 3 題問。
- **用工具排名前 3 的題目；要換掉其中一題，在回覆裡寫一句為什麼**（例如「第 2 題換成 X，因為使用者上一輪已經說不知道」）。不要換成「檔案完整嗎」「在哪裡跑」這類情境題；同一類的確認（例如兩個檔案是不是完整的）合併成一題。
- **換掉的題目不會消失。**被換掉的必問題（卡片 `must_ask`）下一輪自動回到第 1 題：用 `--merge` 重跑時，上一輪在前 3 題、還沒回答的 must_ask 記進 inventory 的 `deferred`，在回答或明確不問之前一直排在最前面（標「上一輪延後」）。其他要延後的題目寫進 answers.yaml 的 `deferred: [題目 id]`。不要在回覆說「下輪問」卻不問；不問就寫進 `answered` 或 `unknown` 並說理由。
- **還沒回答的必問題先用卡片的預設值。**`must_ask[].default`（例如 etcd `--metrics` 預設 basic）在回答前就套用，標「假設：預設值」：effects 照預設生效，讀那些 metric 的 rule 在 `metric-sources.md` 是「這個設定下沒有資料（假設：預設值…）」、`contracts-fit.md` 和覆蓋檢查都算進「沒有資料」，不是 0 條。第一次回覆就把它講成結論（「預設下這條沒有資料（推論，待確認）」），不要只放在問題裡；提案檔裡依賴這個未確認事實的 rule 加註解。
- **「不知道／沒權限」要結構化記錄，不要換個問法再問。**寫進 answers.yaml 的 `unknown: [題目 id 或 answer key]`（例如 `unknown: [external_observer, version]`），或把值寫成 `unknown`（`facts: {etcd: {metrics-level: unknown}}`）。工具不再問這題（同一個 answer key 的其他問法也不問），在 `summary.md` 的「假設」寫出改用的假設（例如「當成沒有外部觀察者」、must_ask 有預設時用預設）。用 kubectl、別的指令問同一件事也算重問。
- **有 critical rule、使用者沒給通知時限時，問「這類故障要在幾分鐘內通知到人？」**：工具把它列為必問（一定在前 3 題）。答案寫進 answers.yaml 的 `notify_within: 5m`（或 `--notify-within`、env 的 `critical.notify_within`）重跑；`summary.md` 會把推算的最壞偵測＋通知時間和它比，超過的 rule 逐條列出。沒有時限時只能說「會不會響」，不能說「來得及」。
- **短視窗先問「這個視窗是刻意的嗎？」，回答前預設保留。**rule 的視窗很短（R ≤ 2 × `scrape_interval`，或低於契約要的長度）而使用者沒說過是不是刻意的時候：這一題一定在前 3 題；回答前**不要改視窗**，也不要把「改視窗」排在第一個建議。並列「保留視窗」的做法（縮短該 job 的 `scrape_interval`、`query_offset`、`anchored`，後兩者依版本）與「改視窗」，各附代價。**`keep_range` 只記使用者對「這個視窗是刻意的嗎？」的直接回答。**使用者說「方向可以」「照提案改」是同意提案，寫進 answers.yaml 的 `approved: [{rule, change, round}]`，不要推成 `keep_range: false`；沒有 range selector 的 rule（例如 `x == 0` 這種 instant selector）沒有視窗可保留，不問也不記 `keep_range`（工具會忽略並在「假設」寫「沒有採用」）。`fit_contracts.py --write-fitted` 不會寫入拉長的視窗（那條 rule 標 `keep_range: unknown`）。視窗和使用者原本的 rule 不同、使用者又沒確認可以改的，報告寫「語意已改，未確認」，不能說「符合」（`review.py`／`fit_contracts.py --original <原檔>`，或 inventory 的 `sources.original_rules`）。
- **元件的必問題**：`$AT/catalog/map.yaml` 的 `cards.<id>.must_ask`（例如 etcd 的 `--metrics`、postgres_exporter／redis_exporter 的期限旗標、vmalert 的資料延遲 L、VictoriaMetrics 的 dedup、mongodb_exporter 的 collector 旗標、Patroni 的 failsafe_mode／同步複寫）會改變結論；盤點裡有這張卡時，工具會把它排在前面，照問。
- **必問題的回答要改變結論。**卡片的 `must_ask[].effects` 寫了「回答是什麼時，哪些 metric 沒有資料」（例如 etcd `--metrics` 是 basic 時沒有 `grpc_server_handling_seconds_bucket`）。回答寫進 answers.yaml 的 `facts`（巢狀 `facts: {etcd: {metrics-level: basic}}` 和扁平 `facts: {etcd.metrics-level: basic}` 都可以）重跑後，讀那些 metric 的 rule 在 `metric-sources.md` 是「這個設定下沒有資料」，`contracts-fit.md` 算「沒有資料」，`summary.md` 的偵測那一列和覆蓋檢查（`rule_no_data`）也跟著改。交付的報告還寫「讀得到資料」時，是回答沒有寫回或 key 寫錯，先修正再回覆。
- **每個回答都寫回，再重跑，才回覆。**使用者答了任何一題（版本、旗標、L、dedup、視窗是否刻意、exporter 期限、耗時），先寫進 `answers.yaml`（`version`、`target_versions.<job>`、`keep_range.<rule>: true|false`、`latency`、`dedup_interval`、`eval_delay`、`exporter_deadline.<job>`、`measured.<job>`、`facts.<卡片>.<id>`）或 inventory（值改好並加 `confirmed: true`），用 `--answers`／`--merge`（有 env 時加 `--env`）重跑，再寫下一次回覆。版本也可以寫在 `env/<name>.yaml` 的 `components[].version`，所有工具用同一種讀法。答過的題目不會再問；使用者說「不知道／沒權限」的題目，把題目 id 寫進 `answers.yaml` 的 `unknown: [...]`（決定不問的寫 `answered: [...]`）。交付的報告要和口頭結論一致。
- **事實清單不要用問的，請使用者貼一次唯讀輸出或填 inventory.yaml。**版本、旗標、exporter、功能這類清單算一題，給確切的唯讀查詢，請對方原樣貼回：
  - Prometheus 版本與旗標：`curl -s <prometheus>/api/v1/status/buildinfo`、`curl -s <prometheus>/api/v1/status/flags`；vmalert：`curl -s <vmalert>/metrics | grep -E '^(vm_app_version|flag)'`
  - 實際載入的 rule：`curl -s <ruler>/api/v1/rules`（只要 group 名稱與 file）
  - 某個 job 有哪些 metric：`count by (__name__) ({job="x"})`；exporter 與版本：`group by (job, __name__, version) ({__name__=~".+_build_info"})`
  - scrape 耗時：`docs/tools/baseline.md#procedure` 的三個查詢
  - 或直接在 `inventory.yaml` 把標「未知」的欄位填好（例如 `jobs[].features`），用 `--merge` 重跑。
- **版本決定能給哪些方案。**ruler 版本未知時，不要把 `query_offset`、`anchored` 當成一般選項：寫成「`query_offset`（需要 Prometheus ≥ 2.53（版本未知））」、「`anchored`（需要 Prometheus ≥ 3.x 實驗功能（版本未知））」，或先不列；工具的報告已經這樣標。vmalert 的 `eval_delay` 等旗標也先確認版本。
- **說「可以動手」之前，提案檔要交付在 `proposed/`。**每個要改的檔（rule、`prometheus.yml`、`alertmanager.yml`）都要有一份完整的提案檔，不能只在對話裡描述「4 項修改」；對提案跑 `promtool check rules`／`promtool check config`、`amtool check-config`、`amtool config routes test`（critical 與 Watchdog 各一筆）與 `promtool test rules`，回覆寫出跑的是哪個檔、結果是什麼。
- **本機 promtool 的主版本和目標 ruler 不同時要說。**例如本機 2.x、目標 3.x：`promtool test rules` 是 2.x 的語意（閉區間），3.x 的 range 視窗左開，邊界上的結果可能不同（`$AT/docs/contracts.md#range-functions`）。`review.py`／`fit_contracts.py` 偵測到時會在假設裡警告（`--promtool-version` 可指定），回覆照轉，不要把 2.x 的測試結果說成目標環境的證據。
- **刻意失敗的示範測試要標清楚。**用來示範原 rule 寫法錯誤、預期會失敗的 promtool 測試，檔名用 `*.expect-fail.test.yml`（或放 `tests/demo-failing/`），檔頭第一行註解「預期失敗：示範原本的 rule ……」；回覆列測試結果時分開寫「應該通過」與「預期失敗」兩組，避免使用者以為提案壞了。
- **提案裡新加的 rule 每條都要有 promtool 測試**：至少一個 firing、一個不 firing 的情境（`alert_rule_test` 有 `exp_alerts` 與 `exp_alerts: []` 各一）。從上游（mixin）抄來的 rule，從釘住的 ref（`$AT/catalog/refs.d/` 的 url：tag 與行號）**逐字複製**，在註解寫 url；沒有逐字複製就標「依結構改寫，非原文」。`review.py --original <原檔> --tests <測試檔>`（或 `proposal_check.py`）會列出沒有測試、沒有標來源的新 rule。
- **佔位值列成「上線前要換」，不是環境的發現。**`review.py`／`proposal_check.py` 會把交付檔案裡的 `.invalid`、`*.example`、`example.com` 佔位值自動列成「上線前要換」清單（檔案、行），佔位的 Alertmanager 位址不會被當成「沒有被 scrape」。回覆照轉這份清單。
- **E 指名是哪一個，改 S 要一起看 group `interval`。**rule group 有 `interval` 時，這個 group 的時間推算用 group `interval` 當 E（報告的假設寫「global `evaluation_interval` 30s；group `interval` 覆寫：x 15s」），不要寫成 global 的值。提案只改了 `scrape_interval` 沒改 group `interval`（或反過來）時，`review.py --original <原 rule> --original-config <原 prometheus.yml>` 會標「E 與 S 不一致」：一起改，或在回覆說明。
- **交付的檔案要能直接驗證。**寫提案的 `alertmanager.yml`、scrape 設定時，佔位值要合法：URL 用 `https://dms.example.invalid/ping`、`https://pager.example.invalid/hook`，email 用 `oncall@example.invalid`，需要 SMTP 時補 `smtp_smarthost: smtp.example.invalid:587` 與 `smtp_from`；不要用 `<...>`。說「`amtool check-config`／`promtool check config` 通過」時，跑的必須是交付的那份檔案。
- **「一次 scrape 失敗會讓 `for` 重新計時」要有對的證據。**用 promtool 測試時，scrape 失敗一定寫 `stale`，不要寫 `_`（`_` 只是沒有 sample，lookback 內仍看得到前一個值，原本的 rule 也不會重置）。測試要成對：原本的 rule 在 `stale` 下被重置、提案的 `last_over_time` 在 `stale` 下照常 firing。範本：`$AT/docs/tools/templates/for-reset.rules.yml`、`for-reset.test.yml`（說明見 `$AT/docs/tools/rule-review.md#for-reset-test`）。
- 不合契約時給出路：在目前環境下成立的是哪些 → 保留視窗的做法（改環境：縮短該 job 的 S、`query_offset`、`anchored`）與方案 A 改 rule 並列 → 方案 B 一次改環境，代價都寫出來。視窗是否刻意還沒問過時，保留視窗排在前面。使用者沒選之前不要替他改檔。
- **k 的來源要分清楚。**只有失敗次數（`up==0` 7 天幾次、「任 5 分鐘最多 3 次」）時，k 是**上限／假設**，不是「觀察到的連續失敗」：寫進 answers.yaml 的 `measured.<job>.failures_per_window`（或用 `--job-k-assumed <job>=<N>`），並問「最長連續失敗幾次？」（工具附查詢）。量到最長連續失敗才寫 `measured.<job>.max_consecutive_failures`（或 `--job-failrun`），報告才標「觀察到的連續失敗」。回覆裡列 k = 1、2、3 各要多長的視窗。
- **改了 τ、d 沒有重新量時，停在「需要重新量」。**舊 τ 下量到的 d_max 被截斷（≈ 舊 τ）時，不要拿 d = τ 推「τ 要 ≥ d ÷ 0.9」：d = τ 時這個條件永遠不成立，會變成循環。工具在這種情況下不推 τ 的下限、方案 B 寫「需要重新量」，基準值那一列也只寫「需要重新量」；回覆照轉，請使用者在新的 τ 下量 7 天再定。
- **推算的時間要列出每個輸入的來源與敏感度。**`--notify-within` 的第 6 節列出 S、d、E、O／L、D、`for`、k、`group_wait` 各自來自 file／default／assumed／user，以及 L、d、k 各多一步時總時間多多少；離需求不到 30 秒時工具會警告，回覆裡要轉述「邊際只剩 X 秒」與還沒量的項目，不要只說「做得到」。提案改了 τ 時，舊 τ 下量到的 d_max 標「需要重新量」，不能拿來判「符合」。
- **ruler 是 vmalert 時，標出證據的語意。**`promtool test rules` 的結果寫成「Prometheus 語意」（stale、lookback 是 Prometheus 的；vmalert 的 queryStep、evalDelay 另外推算）；`vmalert -dryRun` 寫成「只驗語法」。不能把兩者說成「vmalert 上的行為已驗證」。
- 結論引用目錄：`$AT/catalog/` 的訊號 id、契約 id，網站連結 `https://hahasula.github.io/alert-trust/components/<卡片>/#<訊號>`、`https://hahasula.github.io/alert-trust/contracts/#<契約>`。

## 第 0 輪：只看檔案，第一次回覆就給

拿到檔案後，先不問問題，**先盤點、再檢查**。一個指令跑完全部：

```bash
python3 "$AT/tools/review.py" --rules <rule 檔...> [--config prometheus.yml|vmagent 設定 --config-kind prometheus|vmagent] \
    [--alertmanager alertmanager.yml] [--answers env/<name>.sources.yaml] [--notify-within 5m] --out report
```

它會依序跑：盤點（`tools/inventory.py`，產生 `report/inventory.yaml` 與 `inventory.md`）→ metric 來源 → presence lint → 時間契約（沒有數字時用 Prometheus 預設值並寫明）→ 覆蓋（給 `--env` 時），並寫出 `report/summary.md`：建議先處理的 3 件事、盤點確認、假設、下一輪的問題。之後要單獨重跑某個工具時，加 `--inventory report/inventory.yaml`，所有工具都從同一份盤點取資料。

**盤點是共同的事實基礎。**`inventory.md` 列出從檔案理解的環境：每個 job 的 interval 與實際生效的 timeout、target label 從哪來、被丟掉的 metric、每條 rule 對到哪個 job、需要哪些 label、會被送到哪個 receiver、哪些元件沒被抓。每個值標來源（檔案、預設、提示、使用者、未知）。第一次回覆要附上盤點摘要，請使用者確認「這是我從檔案理解的環境，哪裡不對？」。使用者的更正寫進盤點檔（標 `confirmed: true`），之後用 `--merge` 重跑會保留。

第一次回覆的格式：

0. **使用者問「會不會告警」「X 分鐘內會不會知道」時，第一句回答端到端的結果：「X 內會不會有人被 page」**，不是「偵測得到」。接著分開列四段，任何一段不成立或未知，第一句就不能說「會」：
   - **偵測**（rule 的邏輯）：條件成立時 rule 會不會變成 firing、最壞要多久。
   - **載入**（`rule_files`／vmalert `-rule`）：這些 rule 有沒有被這台 ruler 載入。
   - **路由**（Alertmanager）：alert 會不會到會 page 的 receiver（label、route、空 receiver、只寄信）。
   - **自我監控**（Watchdog／dead man's switch／外部觀察者）：ruler 或 Alertmanager 自己停了，誰知道、多久知道。
   使用者給了時間（「5 分鐘內」）時加 `--notify-within 5m`（或寫進 env 的 `critical.notify_within`）：`contracts-fit.md` 第 6 節列出每條 critical rule 的最壞時間 `S + d + E（+ O；vmalert 是 + max(L, D)）+ ⌈for/E⌉·E + group_wait`，`group_wait` 取自 inventory 的 route，公式寫出來並標**推算**；`summary.md` 開頭的「會不會通知到人（端到端）」就是這四段。
1. **建議先處理的 3 件事**：依影響排序。常見的有：
   - 永遠不會響的 rule（job 對不上、被 `metric_relabel_configs` 丟掉、要求 config 沒加的 label）。
   - 一直在響的 rule（`absent()` 包住對不上的 job）。
   - 評估這些 rule 的 Prometheus／vmalert 沒有任何自我監控（沒有 Watchdog、沒有外部 dead man's switch、Alertmanager 沒被 scrape）；config 沒有 `rule_files` 或 `alerting`。
   - 告警送不到該送的人：路由需要的 label rule 沒有（例如 critical 只會寄信，或被送進空的 receiver）、inhibit 沒有 `equal`、有 dead man's switch 路由卻沒有 Watchdog rule。
   - config 裡 `scrape_timeout` 與 `scrape_interval` 的關係、明顯太短的視窗。
   - 不適用這個部署的 rule：工具標「這條 rule 不適用這個部署」並寫原因（功能或 label），例如 per-host exporter 卻有多 target `/probe` 的 rule、沒有 heartbeat 表卻有 heartbeat 的 rule、沒有 semi-sync 卻有 semi-sync 的 rule、`by (cluster)` 但 target 沒有 `cluster` label。回報成「不適用」，不要只說「沒有資料」；建議移除或改寫，annotation 寫的是另一種部署模式時也要指出。
   - Watchdog 沒有專用 route：它會繼承上層的 `repeat_interval`／`group_interval`，DMS 要等很久（`deadman-wait`）。給專用 route 的範本（inventory.yaml 的 `alertmanager.watchdog_route.template`）；DMS 等待超過使用者的時間要求時，說明 ruler 停機要靠外部觀察者的 `up` 才能及時知道。
   使用者問了具體問題（例如「失去 quorum 會不會告警」）時，**第一句就直接回答**（照上面第 0 項的端到端格式），再列這 3 件事。
2. **盤點摘要**（`inventory.md` 的重點）：從檔案判斷出的監控體系與環境，判斷不了的部分標「未知」，請使用者更正。
3. **我做了哪些假設**，以及補上哪一項會改變哪個結論。
4. **第一組問題**（最多 3 個單一問題，照「全程規則」的提問預算）。通常是：
   - 版本：請貼 `/api/v1/status/buildinfo` 與 `/api/v1/status/flags` 的輸出（算一題）。
   - 每個 job 的 scrape 耗時 p99／最大值與 `up == 0` 次數：請貼 `inventory.md` 問題裡的三個查詢的輸出（算一題）。
   - 「這個視窗是刻意的嗎？」（有短視窗時必問，一定在前 3 題）；有沒有外部觀察者。
   - 元件卡的 `must_ask`（工具已排序；例如 etcd `--metrics`、exporter 期限旗標、vmalert 的 L）。
   - 「這類故障要在幾分鐘內通知到人？」（有 critical rule、沒給時限時必問）。
   照工具排名的前 3 題問；換掉任何一題時在回覆裡寫理由。檔案裡看得到的事（例如 job 名稱）不要問，寫在盤點摘要請對方確認即可。

沒有 config 時，第 0 輪仍然可以給：presence 寫法、明顯太短的視窗（用假設的 S）、沒有自我監控的規則；並在問題裡要 config。

## 不知道時的假設

| 不知道 | 先用 | 補上後會改變什麼 |
|---|---|---|
| Prometheus 版本 | 不知道：`query_offset` 標「需要 Prometheus ≥ 2.53（版本未知）」、`anchored` 標「需要 Prometheus ≥ 3.x 實驗功能（版本未知）」 | 2.53 以前沒有 `query_offset`，方案 B 少一個選項；3.x 以前沒有 `anchored` |
| 使用者要求的通知時間 | 沒有，只列四段；有 critical rule 時「這類故障要在幾分鐘內通知到人？」是必問 | 給了就寫進 `notify_within`（或 `--notify-within`），第一句回答「X 內會不會被 page」，超過的 rule 逐條列出 |
| 外部觀察者 | 沒有 | monitor of monitor 的結論 |
| scrape 耗時 d | 最壞情況 d = S | 每條 rule 需要的最短視窗 |
| 最長連續失敗 | k = 1，標「假設」；只有失敗次數時標「上限／假設」並問「最長連續失敗幾次？」 | 最短視窗 |
| exporter 自己的期限 | 卡片寫的預設值 | exporter-deadline 是否成立 |
| 視窗是否刻意 | 未知：不改視窗（`keep_range: unknown`），保留視窗的做法與改視窗並列，保留排前面 | 是否提方案 A；回答前 `--write-fitted` 不拉長 |
| vmalert 資料延遲 L | L = S（假設，工具標 assumed） | 視窗長度與通知時間；用 must_ask 問 |
| 拓撲（複本、heartbeat、`/probe` 等） | 從 config 看得出的直接判斷（例如 job 直接抓 `host:9104`、沒有 `__param_target` → 多 target `/probe` 的 rule 不適用）；看不出的標「需要回答」，請使用者在 `inventory.yaml` 的 `jobs[].features` 一次填 | 哪些 rule 與訊號不適用 |

## 第 1 步：監控體系

答案寫進 `env/<name>.yaml`（格式見 `$AT/env/env.example.yaml`；`targets[].job` 填 **config 裡實際的 job 名稱**，`features` 填這個 target 有哪些功能，例如複本、heartbeat、`/probe`，讓不適用的訊號能被排除）。

| 問題 | 怎麼判斷 | 為什麼重要 |
|---|---|---|
| 評估 rule 的是 Prometheus 還是 vmalert？版本？ | `type: vlogs`、`eval_delay`、`concurrency` 是 vmalert；`query_offset`、`keep_firing_for` 是 Prometheus；CRD 名稱；config 的 `rule_files` | 公式不同：Prometheus `R ≥ (n+k)·I + max(0, d − O)`，vmalert `R ≥ (n+k)·I + max(0, L − D)`；n 依函式（`$AT/docs/contracts.md#range-functions`） |
| 資料從哪裡來？ | config 的 `scrape_configs`；vmagent → VictoriaMetrics | 決定用哪些元件卡 |
| 有沒有外部觀察者？告警走哪台 Alertmanager？ | config 的 `alerting`；問 | 沒有外部觀察者時，monitor of monitor 全部只在自己身上 |

## 第 2 步：每個 metric 的來源

`check_metric_sources.py` 把每個 metric selector 對到 config 的 scrape job：recording rule、內建（`up`、`scrape_*`、`ALERTS`）、live 看得到、使用者確認、**job 對不上**、**被丟掉**、**一直在響**（`absent()` 對不上 job）、live 查不到、**沒有來源**（`vmalert_*`、`alertmanager_*`、`prometheus_*` 這類前綴指名元件的 metric，config 裡沒有任何 job 在抓那個元件：「沒有來源（config 沒有抓 vmalert）」，不是要問人）、要問人（含 rule 要的 label config 沒加）。

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
- **資料庫與主機分派到元件 plugin**：盤點或 env 裡每個資料庫 target（與主機），用 `$AT/catalog/map.yaml` 的 `cards.<卡片 id>.plugin` 找出 plugin、`plugins.<plugin>.skills` 找出 skill（`type` 的別名見 `$AT/tools/check_coverage.py` 的 `TYPE_ALIASES`）。可用的 skill 清單裡有 `alert-trust-<db>:<db>` 時呼叫它，帶 `--inventory` 與 env；在 repo clone 裡沒裝 plugin 時讀 `plugins/<dir>/skills/<skill>/SKILL.md` 照做；兩者都沒有時，只做 `$REF/five-sections.md#generic` 的通用正確觀測檢查（卡片由 core 的 `catalog/` 提供），並在回覆裡寫「`<target>` 只做了通用檢查；安裝 `<plugin>` 後執行 `/<plugin>:<skill>` 可以補上其他四段」。各元件 skill 交回的五段結果只取缺口與最重要的發現，和這一步的監控鏈缺口一起排序，放進「建議先處理的 3 件事」。不要因為等元件 skill 而延後第 0 輪的回覆：第 0 輪先用通用檢查，元件 skill 的結果在下一輪更新。
- 基礎設施的缺口沒有處理前，第 4 步的結論加一句：「評估這些 rule 的 ruler 本身沒有被監控，以下結論在它變慢或掛掉時不成立。」

## 第 4 步：每條 rule 的時間契約

需要的數字：`scrape_interval`（S）、`scrape_timeout`（τ）、尖峰與平常的 scrape 耗時（d_max、p99）、最長連續失敗（k）、exporter 的期限（X）、`evaluation_interval`（E）、尖峰 group 耗時（T）；vmalert 另外要 `eval_delay`（D）與資料延遲（L）。S、τ 從 config 讀（`--config`）；其他問使用者或從快照取，拿不到就用假設。

**先問「這個視窗是刻意的嗎？」，再改任何視窗**：像 `[1m]` 常常就是要「最近 1 分鐘」，拉長會改變語意。答案寫進 answers.yaml 的 `keep_range: {<rule>: true|false}`（或 `--keep-range <rule>`、`--change-range <rule>`、annotation `keep_range: "true"`）後重跑。沒有答案時工具不寫拉長的視窗，報告把保留視窗的做法和改視窗並列。拿提案檔重跑時加 `--original <原本的 rule 檔>`（或用 `--merge` 舊 inventory，工具會記住原檔）：改過視窗、沒確認的 rule 是「語意已改，未確認」，不是「符合」。

```bash
python3 "$AT/tools/fit_contracts.py" --inventory report/inventory.yaml [--job-duration <job>=<d_max>] \
    [--job-p99 <job>=<p99>] [--job-failrun <job>=<量到的連續失敗 N>] [--job-k-assumed <job>=<只有次數時的上限 N>] \
    [--exporter-deadline <job>=<X>] [--group-duration <T>] [--promtool-version <本機版本>] \
    [--keep-range <rule>] [--change-range <rule>] [--original <原本的 rule 檔...>] [--answers answers.yaml] [--env env/<name>.yaml] \
    [--prom-version <版本>] [--dedup-interval <dedup>] [--latency <L> --eval-delay <D>] \
    --out report [--write-fitted report/fitted-rules.yml]
```

- 使用者還沒有基準值（不知道 S、τ 該設多少）時，加 `--baseline`，依 `$AT/docs/tools/baseline.md` 由量到的耗時推出建議值。官方沒有 timeout 餘裕的建議；0.8、0.9 是 alert-trust 依契約訂的預警線，回報時說明。
- 報告順序：環境本身（`d ≤ S`、`τ < S`、`X < τ`、耗時是否被 τ 截斷、p99、`E ≥ S`、`T ≤ E`、lookback；提案改了 τ 時舊的 d_max 標「需要重新量」）→ 在目前環境下成立的 rule（「語意已改，未確認」另列，不算成立）→ 不成立的 rule 的方案 A／B → 保留視窗的做法（縮短該 job 的 S、`query_offset`、Prometheus 3.x `anchored`），和改視窗並列 → 一次讓所有 rule 成立的環境設定。
- 沒有 range 的門檻 rule 加 `for` 時，一次 scrape 失敗就讓 series 變成 stale、`for` 重新計時；工具會提示，工具第 5 節會建議改用 `last_over_time(x[R])`（R 依 `range-window-1sample`）；`keep_firing_for` 只在已經 firing 之後才有幫助。這一項是推論。
- 驗證這個推論時照「全程規則」：promtool 測試用 `stale` 模擬失敗，原本與提案成對（範本 `$AT/docs/tools/templates/for-reset.test.yml`）。
- 代價一起說：A 視窗變長，短暫尖峰反應變慢、語意可能改變；B 的 `scrape_interval` 變短會增加 scrape 負載，`query_offset` 讓每條 rule 都晚判斷。

其他契約（resend／EndsAt、Alertmanager 告警視窗、exporter 期限）照各 pack：Prometheus `${CLAUDE_SKILL_DIR}/../prometheus/SKILL.md` 第 2 節；vmalert／VictoriaLogs `${CLAUDE_SKILL_DIR}/../victoriametrics/SKILL.md` 第 1、2 節；Alertmanager `${CLAUDE_SKILL_DIR}/../alertmanager/SKILL.md` 第 2 節；資料庫與主機：各自 plugin 的 skill（`/alert-trust-<db>:<db>`，見第 3 步的分派；`exporter-deadline` 在 `$REF/five-sections.md#observe` 的 scrape-budget）。

## 第 5 步：規則本身的寫法

- `validate.py --lint-rules` 找出 `x == 0`、`x != 1` 這類資料消失時回傳空的寫法。
- 關鍵 alert 與昂貴的 recording rule 放在不同 group。
- 被 `metric_relabel_configs` 丟掉的 metric 會讓之後加入的範本失效，提醒使用者。

## 回報格式（每輪更新同一份）

0. 使用者問了「會不會告警／X 內會不會知道」時：端到端的第一句與四段（偵測、載入、路由、自我監控）
1. 建議先處理的 3 件事（依影響排序）
2. 監控體系（第 1 步，標明判斷依據）
3. 讀不到資料或一直在響的 rule（第 2 步）
4. 基礎設施的 monitor of monitor 缺口（第 3 步，第 1 級優先）
5. 時間契約：在目前環境下成立的、不成立的與方案（第 4 步）
6. 寫法建議（第 5 步）
7. 假設與沒有確認的部分，以及下一輪要問的問題
