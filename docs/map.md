# 範圍與結構

每張卡（監控系統或被監控的 target）都用同一個五段框架回答。元件頁照這五段排；skill 照 `catalog/map.yaml` 找出每張卡屬於哪個 plugin、每段有哪些項目。

<!-- figure: map-five-sections -->

<p class="at-caption">元件卡（YAML）產生元件頁的五段；<code>tools/build_pages.py</code> 同時產生 <code>catalog/map.yaml</code>，決定每張卡屬於哪個 plugin。每個資料庫與主機的 plugin 都依賴 <code>alert-trust-core</code>。</p>

## 五段 {#sections}

| 段落 | 回答什麼 | 卡片欄位 | 元件頁錨點 | 依據 |
|---|---|---|---|---|
| 1 架構 | 使用時會留下哪些痕跡 | `architecture`（連到架構頁的段落） | `#architecture` | 原始碼 |
| 2 正確使用 | 怎麼設定、限制在哪 | `usage` | `#usage`，每項 `#usage-<id>` | 原始碼推理、本機小實驗；官方文件要標「文件」 |
| 3 正確觀測 | 看到的數字是不是真的 | 訊號 `section: observe` | `#observe`，每個訊號 `#<訊號 id>` | 原始碼、本機小實驗 |
| 4 不正確使用時的觀測 | 誤用或故障時看得到什麼，包含寫得不好的查詢 | 訊號 `section: misuse`、`bad_queries` | `#misuse`、`#bad-query-table` | 原始碼、本機小實驗 |
| 5 目前無法觀測的痕跡 | 有痕跡但 exporter／endpoint 沒輸出 | `blind_spots`（`options` 一行） | `#blind` | 原始碼；方案只是建議 |

- 驗證順序以第 2 段為起點：先依原始碼確認「該怎麼用」，再判斷哪些痕跡代表看得準（第 3 段）、哪些代表用錯了（第 4 段）。
- 監控系統本身就是觀測的路徑：它的存活、重啟、reload、rule 評估是否成功屬於第 3 段；target 的重啟、OOM 屬於第 4 段。
- 同一個主題同時有「量得準不準」和「數值異常」兩面時，拆成兩個訊號（例如複寫延遲）。
- 告警的 `runbook_url` 指到 `components/<卡片 id>/#<訊號 id>`，訊號移到哪一段，錨點都不變。

欄位格式見[參與與擴充](contributing.md#five-sections)。

## 深度界線 {#depth}

範圍只到 exporter 或 endpoint 實際輸出的那一層，不往資料庫核心或作業系統內部挖：

| 元件 | 到哪一層為止 |
|---|---|
| 監控鏈（Prometheus、VictoriaMetrics、Alertmanager…） | 元件自己的 `/metrics`、API 與設定 |
| 主機與容器 | node_exporter、cAdvisor、kube-state-metrics 的輸出，不進 Linux 核心 |
| 資料庫 | exporter 送出的查詢與 metric（或內建 `/metrics`），以及資料庫提供的系統表；之下的實作只在架構頁說明「痕跡從哪來」 |

exporter 沒輸出的痕跡放第 5 段，方案點到為止（例如「用 query-exporter 查某個系統表」）。

## Plugin 與卡片 {#plugins}

下表由 `catalog/map.yaml` 產生，數字是各段的項目數。

<!-- map: plugins -->

- `alert-trust-core` 放監控鏈的卡片與上層 skill（rule-review、selfcheck 等）：盤點環境、依 `map.yaml` 找出每個元件的 plugin、匯集結果。
- 資料庫與主機各自一個 plugin（`plugins/<db>/`），依賴 core；沒裝時上層 skill 只做通用的正確觀測檢查，並提示可以安裝哪個 plugin。目錄與 symlink 的做法見[參與與擴充](contributing.md#plugins)。

## `catalog/map.yaml` {#map-yaml}

產生的檔案，不要手改；改卡片後執行 `python3 tools/build_pages.py`，`tools/validate.py` 會在它過期時報錯。這份檔案要 commit，skill 直接讀它。

```yaml
sections:            # 五段的順序與元件頁上的錨點
  - {id: usage, title: 正確使用, field: usage, anchor: usage}
plugins:
  alert-trust-postgresql: {title: PostgreSQL, dir: plugins/postgresql, cards: [postgres_exporter],
                           skills: ['alert-trust-postgresql:postgresql'], depends: [alert-trust-core]}
cards:
  postgres_exporter:
    plugin: alert-trust-postgresql
    page: components/postgres_exporter.md
    guide: targets/postgres.md        # 手寫的「怎麼監控」頁（資料庫才有）
    architecture: architecture/db-relational.md
    usage: [exporter-pg-monitor, ...]   # 第 2 段的 id
    observe: [exporter-up, ...]          # 第 3 段的訊號 id
    misuse: [replication-slots, ...]     # 第 4 段的訊號 id
    bad_queries: [{case: ..., signals: [...]}]
    blind: [...]                         # 第 5 段每項的 what
```

Plugin 與卡片的對應寫在 `tools/build_map.py` 的 `PLUGINS`；新增卡片時要加進去，否則 validate 會報錯。
