---
name: valkey
description: Valkey/Redis target pack of alert-trust (redis_exporter). Reads the redis_exporter card from catalog/ at run time and checks it in the five sections (architecture, correct usage, correct observation, observation of misuse incl. bad queries, blind spots); exporter model - redis_up vs up, connection timeout longer than scrape_timeout, ACL limits that silently drop config series, replication and Sentinel. Valkey／Redis 監控檢查：照五段讀 redis_exporter 卡。
argument-hint: "[env/<name>.yaml] [target-id]"
---

# Valkey／Redis（alert-trust-valkey）

檢查 env 檔 `targets[]` 裡 `type` 是 `redis_exporter` 的 target（別名：`valkey`、`redis`）。卡片：`redis_exporter`（`$AT/catalog/targets/redis_exporter.yaml`，索引在 `$AT/catalog/map.yaml` 的 `cards.redis_exporter`）；model：exporter（redis_exporter），以卡片的 `model` 欄位為準。

## 路徑

- `$AT`：底下有 `catalog/`、`tools/`、`rules/`、`env/`、`queries/`。裝成 plugin 時是 `${CLAUDE_PLUGIN_ROOT}`（這個 plugin 的目錄；repo 裡是 symlink，從 git marketplace 安裝時已複製成實際檔案）；在 alert-trust repo clone 裡直接用（不裝 plugin，例如 opencode）時是 repo 根目錄。
- `$REF`：共用說明。plugin：`${CLAUDE_PLUGIN_ROOT}/references`；repo clone：`plugins/core/references`。
- `$AT/docs/` 只在 repo clone 有；裝成 plugin 時改讀網站，對照規則見 `$REF/five-sections.md#paths`。

## 流程

照 `$REF/five-sections.md` 做：先決條件 → 讀卡片 → 五段（架構 → 正確使用 → 正確觀測 → 不正確使用時的觀測（含 `bad_queries`）→ 目前無法觀測的痕跡）→ 回報。全程規則（唯讀、不連資料庫、隱私、數字標來源、引用證據）與深度界線也在那份。**訊號清單每次從卡片讀，不要從這份 skill 抄。**這份只寫 Valkey／Redis 專屬的部分。

「怎麼監控」頁：`$AT/docs/targets/valkey.md`；元件頁：`$AT/docs/components/redis_exporter.md`（產生的頁面，repo 裡要先跑 `tools/build_pages.py`；沒有就讀網站）。

## Valkey／Redis 專屬

- 卡片 id 是 `redis_exporter`；env 的 `valkey`、`redis` 都對到它。
- scrape-budget：redis_exporter 的連線逾時預設比常見的 `scrape_timeout` 長（卡片 `silent_failures`、`docs/contracts.md#exporter-deadline`），伺服器忙時症狀會變成「exporter 停機」。
- exporter 帳號的 ACL 不足時 `redis_up` 仍可能是 1，只有部分 series 消失；照卡片 `silent_failures` 對照。
- 有 Sentinel 時，Sentinel 相關結論多數只讀碼（卡片 `evidence`），回報照標。
- 自訂查詢範例在 `$AT/queries/redis_exporter/`。

## 訪談（每輪最多 3 個單一問題，已知的跳過）

1. 拓撲：單機、主從、Sentinel、Cluster？（決定 `applies_when`）
2. exporter 用的帳號有哪些 ACL 限制？請使用者自己跑唯讀命令確認（卡片 `usage` 列了需要的權限），遮蔽後貼回。
3. redis_exporter 的 `--connection-timeout` 與其他 collector 旗標有沒有改過？

## 回報

照 `$REF/five-sections.md#output` 每個 target 一份五段表。被 `selfcheck` 或 `rule-review` 呼叫時，把結果交回它匯集；使用者直接呼叫時，最後列這張卡「建議先處理的 3 件事」。
