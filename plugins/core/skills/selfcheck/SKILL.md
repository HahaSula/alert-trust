---
name: selfcheck
description: Entry point of the alert-trust self-check for a monitoring stack (Prometheus, Thanos, VictoriaMetrics/vmalert/vmagent/VictoriaLogs, Grafana Alerting, Alertmanager, database targets). Runs the 3-stage flow from docs/model.md - env profile, read-only snapshot, coverage gaps - dispatches to the per-component packs and assembles the final report. Use when someone wants to know whether their alerts can be trusted or which monitor-of-monitor signals are missing. 自檢整條 alert 鏈、rule 可信度、monitor of monitor 缺口時使用。
when_to_use: 自檢監控、alert 能不能信、alert 不觸發、假 resolved、monitor of monitor、meta-monitoring、dead man's switch、Watchdog、vmalert 自檢、Prometheus 自檢、Alertmanager 自檢、exporter 盲點
argument-hint: "[env/<name>.yaml]"
---

# alert-trust 自檢

幫監控 owner 回答兩個問題：

1. 整條 alert 鏈（採集 → 寫入 → 查詢 → 評估 → 通知路由 → 接收）壞在哪一段時，有沒有人知道？
2. 在實際尖峰下，rule 還能不能信？

**知識在目錄裡，不在這份 skill 裡。**訊號清單、契約、門檻、證據都從 `catalog/` 讀；不要憑記憶列訊號，也不要把卡片內容抄進報告以外的地方。

## 路徑

| 變數 | 裝成 plugin（`alert-trust-core`） | 在 alert-trust repo clone 裡直接用（不裝 plugin，例如 opencode） |
|---|---|---|
| `$AT`：`catalog/`、`tools/`、`rules/`、`env/` | `${CLAUDE_PLUGIN_ROOT}`（repo 裡這些是 symlink，從 git marketplace 安裝時複製成實際檔案） | repo 根目錄 |
| `$REF`：共用說明（`five-sections.md`、`evidence.md`、`snapshot.md`） | `${CLAUDE_PLUGIN_ROOT}/references` | `plugins/core/references` |
| 其他 core skill | `${CLAUDE_SKILL_DIR}/../<skill>/SKILL.md` | `plugins/core/skills/<skill>/SKILL.md` |
| 資料庫與主機的 skill | 另一個 plugin：`/alert-trust-<db>:<db>`（讀不到它的檔案，只能呼叫） | `plugins/<db>/skills/<db>/SKILL.md`（直接讀來照做） |

開始前用 `ls "$AT/catalog/map.yaml"` 確認路徑對。`$AT/docs/` 只在 repo clone 有；裝成 plugin 時改讀網站，對照規則見 `$REF/five-sections.md#paths`。

使用者的檔案放在**目前工作目錄**，不要寫進 `$AT`：

| 檔案 | 位置 | 誰產生 |
|---|---|---|
| 環境描述 | `env/<name>.yaml` | `env-profile` |
| 快照 | `selfcheck-data/<name>-<YYYYMMDD>/` | `tools/collect.py` |
| 覆蓋矩陣 | `reports/<name>-<YYYYMMDD>/coverage.{md,json}` | `tools/check_coverage.py` |
| 報告 | `reports/<name>-<YYYYMMDD>/selfcheck-report.{md,json}` | 本 skill |

## 全程規則

- **唯讀。**只用 HTTP GET。不改設定，不呼叫 `/-/reload`、`/-/quit`、Prometheus admin API、VM 的 `/api/v1/admin/*` 與 `/internal/*`，不建立或刪除 Alertmanager silence，不 push 任何檔案，除非使用者明確要求。會影響線上的測試（例如暫停 Watchdog 路由）只寫成建議，由使用者決定並自己執行。
- **不要外傳資料。**env 檔、快照、報告可能含內部主機名、label、拓撲與設定。留在本機，不放公開位置。確認工作目錄的 `.gitignore` 有 `env/*.yaml`、`selfcheck-data/`、`reports/`，沒有就提醒加上；**env 檔絕對不要 commit 到公開 repo**（包括 alert-trust 本身）。使用者要分享報告時，先提醒去識別化。報告裡不重現 receiver URL、token、密碼。
- **Token 只走環境變數**（`AT_TOKEN_<ID>` 或 `PROM_BEARER_TOKEN` 等，見 `$REF/snapshot.md`）。不要請使用者把 token 貼進對話。
- **能量的就不要問。**先偵測、先跑腳本，只問查不到的事。一次一組，**每組最多 3 題**，每題說明為什麼要問、不知道時怎麼查。不知道就記「未知」，繼續往下。
- **數字要標來源**，三種之一：`量測`（附快照檔名與欄位，或查詢式）、`使用者說`、`推算`（附公式與代入值）。目錄裡的門檻是小規模實驗的經驗值，不能當成對方環境的事實。
- **結論要引用證據**，格式見 `$REF/evidence.md`。
- **用使用者的語言回答。**

## 流程

使用者只提供 rule 檔、要你「檢查這些規則」時，改走 `rule-review`。不論哪一條路，**都先確認監控體系與基礎設施的覆蓋，最後才看每條 rule 的時間契約**。

三個階段依序產出環境描述、快照、覆蓋矩陣，最後合成報告。流程的定義在 `$AT/docs/model.md` 的「檢查流程」，開始前讀一次。使用者只想看其中一項時，直接跳到對應的 pack，但仍要有 env 檔（至少階段 1 的第 1 輪）。

### 階段 1：環境描述

參數給了 `env/<name>.yaml` 而且檔案存在時，讀它，問使用者有沒有變動，然後跳到階段 2。否則使用 **`env-profile`**。

### 階段 2：快照

依 env 檔組出 collect 指令。**每個 `--xxx` 都要帶 env 檔裡的 id**，check_coverage 靠 id 對應：

| env 檔的 `type` | collect 參數 |
|---|---|
| `prometheus`（含 `observers` 裡 type 是 prometheus 的） | `--prom <id>=<url>` |
| `vmalert` | `--vmalert <id>=<url>` |
| `victoriametrics` | `--vm <id>=<url>`；cluster 給 vmselect 的查詢前綴 `http://vmselect:8481/select/0/prometheus` |
| `vmagent` | `--vmagent <id>=<url>` |
| `victorialogs` | `--vlogs <id>=<url>` |
| `alertmanager` | `--am <id>=<url>`；HA 時每台各一個 id |
| `thanos`（Ruler 或 Querier） | `--prom <id>=<url>`：Prometheus 相容的查詢與 `/api/v1/rules` 拿得到；Prometheus 專有的 flags、config、targets 會記成錯誤，屬正常 |
| `grafana_alerting` | collect 不支援。請使用者存下 `/api/prometheus/grafana/api/v1/rules` 的回應，放到 `<快照>/<id>/rules.json` |

```bash
python3 "$AT/tools/collect.py" --out selfcheck-data/<name>-<YYYYMMDD> --range 7d \
  --prom prom-a=http://localhost:9090 --vmalert vmalert-biz=http://localhost:8880 \
  --vm vm=http://localhost:8428 --am am=http://localhost:9093
```

- `--range` 要涵蓋尖峰。問尖峰何時發生；保留期不足或查詢太重時縮短。
- 跑完讀 `manifest.json` 與每個 `<id>/errors.json`，把讀不到的項目告訴使用者，決定要不要補 port-forward 或改成貼資料。
- 不能連 API 的元件，請使用者貼對應的輸出，你照 `$REF/snapshot.md` 的檔名存進快照目錄（例如 `/api/v1/rules` 的原始 JSON 存成 `<id>/rules.json`），check_coverage 一樣讀得到。

快照的目錄結構與每個檔案的內容見 `$REF/snapshot.md`。

### 階段 3：缺口

先跑覆蓋檢查（需要 `pip install pyyaml`）：

```bash
python3 "$AT/tools/check_coverage.py" --env env/<name>.yaml \
  --snapshot selfcheck-data/<name>-<YYYYMMDD> --out reports/<name>-<YYYYMMDD> [--live]
```

- 預設只檢查第 1 級訊號；要連第 2 級一起看時加 `--tiers 1,2`。
- `--live` 會用 env 檔裡每個 ruler 的 URL 查 rule selector 的 series 數，才判得出 `rule_no_data`。只能連到快照、不能連線上時不要加。
- 讀 `coverage.json`（給你）與 `coverage.md`（給人）。狀態：

| 狀態 | 意思 | 你要做什麼 |
|---|---|---|
| `covered` | 有規則、有資料、在獨立觀察者上評估 | 交給 pack 檢查契約與門檻 |
| `self_only` | 規則在被監控的元件自己身上 | 列為缺口：它壞掉時規則跟著停 |
| `rule_no_data` | 規則載入了，selector 是空的 | 最優先：常見原因是 job 名稱不符 |
| `missing` | 沒有規則引用這個訊號 | 列為缺口，附 `rules/` 裡的範本 |
| `not_applicable` | 環境沒有這個情況 | 確認理由寫得出來 |
| `needs_input` | 只能靠問的訊號（故障域、告警路徑、DMS） | 用 pack 的訪談題補問，再更新 env 檔重跑 |

然後依 env 檔裡出現的元件，依序使用這些 pack（core 的 pack 叫 `/alert-trust-core:<pack>`；資料庫與主機見下面的「分派到元件 plugin」）：

| env 檔有 | Pack | 回答什麼 |
|---|---|---|
| `prometheus`（含觀察者） | `prometheus` | 尖峰、rule 時間契約、資源邊際、升級路徑 |
| `vmalert`、`victoriametrics`、`vmagent`、`victorialogs` | `victoriametrics` | vmalert 視窗契約、log rule、partial response、假 resolved、唯讀 |
| `alertmanager` | `alertmanager` | 路由、DMS、失敗告警視窗、HA、送達失敗 |
| `thanos` | 沒有專屬 pack：讀 `$AT/catalog/components/thanos.yaml` 與 `$AT/docs/architecture/thanos.md`，逐條看第 1 級訊號與 `silent_failures` | store 少掉時查詢仍回成功（`partial_response_strategy` 擋不住）、Querier 掛掉的假 resolved、Compactor 停擺 `up` 仍是 1、sidecar 上傳 |
| `grafana_alerting` | 沒有專屬 pack：讀 `$AT/catalog/components/grafana_alerting.yaml` 與 `$AT/docs/architecture/grafana-alerting.md` | NoData／Error 設成 OK 會悄悄 resolved、`execErrState` 預設、重送 30s 寫死的 EndsAt、HA |
| `targets` 裡任何 type、主機 | 各自的 plugin（`/alert-trust-<db>:<db>`、`/alert-trust-host:host`），見下節 | 照 `$REF/five-sections.md` 的五段；資料庫依卡片的 `model`：exporter 型的三層存活與查詢期限，native 型的 `up`／探測／健康 metric 交叉判斷 |
| 任何 ruler，且使用者要可重複的規則 | `policy-lint` | 依 policy 列出違反項與建議值 |

### 分派到元件 plugin

資料庫與主機的檢查在各自的 plugin（都依賴 `alert-trust-core`）。對 env 檔的每個 target（與主機，env 有主機、容器或 Kubernetes 時）：

1. **找卡片與 plugin**：`type` 是卡片 id，或 `$AT/tools/check_coverage.py` 的 `TYPE_ALIASES` 裡的別名（`mysqld_exporter`、`mysqld` 要先問是 MySQL 還是 MariaDB）。讀 `$AT/catalog/map.yaml`：`cards.<卡片 id>.plugin` 是 plugin 名稱，`plugins.<plugin>.skills` 是要呼叫的 skill，`plugins.<plugin>.dir` 是 repo 裡的目錄。目錄沒有這張卡時，照 `$REF/five-sections.md` 只用 target-pattern 的骨架檢查，標「沒有專屬卡」。
2. **plugin 有裝**（你可用的 skill 清單裡有 `alert-trust-<db>:<db>`）：呼叫它，帶 env 檔與 target id。同一個 plugin 的多個 target 一次呼叫。
3. **在 repo clone 裡、沒裝 plugin**：`plugins/<dir>/skills/<skill>/SKILL.md` 讀得到，直接讀來照做。
4. **都沒有**：只做 `$REF/five-sections.md#generic` 的通用檢查（第 3 段「正確觀測」：target-pattern 的標準問題與 `section: observe` 的訊號；卡片由 core 的 `catalog/` 提供，一定讀得到）。在報告與回覆裡告訴使用者：「`<target id>` 只做了通用的正確觀測檢查；安裝 `<plugin>`（`/plugin install <plugin>@alert-trust`）後執行 `/<plugin>:<skill>` 可以補上正確使用、不正確使用時的觀測與無法觀測的痕跡。」

每個元件的結果都是一份五段表（`$REF/five-sections.md#output`）；匯集時只取每份的缺口與「最重要的發現」，依下面的「判斷優先順序」和監控鏈的缺口一起排序，寫進報告的「必須先處理」（格式見 `references/output-format.md`）；回覆使用者時先講前 3 件。

`observers` 由本 skill 處理：讀 `$AT/catalog/components/observer.yaml`，`needs_input` 的訊號用它的 `question` 直接問（每組最多 3 題）。

每完成一個 pack，用 1 到 3 句告訴使用者最重要的發現，再問要不要繼續。

### 報告

全部完成後依 [references/output-format.md](references/output-format.md) 寫 `selfcheck-report.md` 與 `selfcheck-report.json`。JSON 讓下一次對話能直接接續：開始時看到舊報告，先讀它，只重做有變動的部分。

## 判斷優先順序

1. **Alert 已經不會觸發：**`rule_no_data`、`for` 反覆重置、視窗契約邊際小於 1、log rule 延遲超過 `D + E`。有事也不會通知，最優先。
2. **沒有人知道監控壞了：**缺外部 DMS、Watchdog 和業務 rule 放在同一個設了 `resendDelay` 的 vmalert、觀察者和被監控對象同一故障域或走同一台 Alertmanager、第 1 級訊號是 `self_only`。
3. **接近失效：**第 2 級訊號的比值超過 0.8、資源接近 limit、磁碟接近唯讀門檻。
4. **可觀測性與升級：**第 3 級訊號與版本規劃。
