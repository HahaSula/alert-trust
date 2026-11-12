---
name: etcd
description: etcd target pack of alert-trust (native /metrics). Reads the etcd card from catalog/ at run time and checks it in the five sections (architecture, correct usage, correct observation, observation of misuse incl. bad queries, blind spots); native model - up stays 1 when the cluster loses quorum, so cross-check up, a blackbox health probe and etcd's own leader metrics, plus member count. etcd 監控檢查：照五段讀 etcd 卡。
argument-hint: "[env/<name>.yaml] [target-id]"
---

# etcd（alert-trust-etcd）

檢查 env 檔 `targets[]` 裡 `type` 是 `etcd` 的 target。卡片：`etcd`（`$AT/catalog/targets/etcd.yaml`，索引在 `$AT/catalog/map.yaml` 的 `cards.etcd`）；model：native（etcd 自己的 /metrics），以卡片的 `model` 欄位為準。

## 路徑

- `$AT`：底下有 `catalog/`、`tools/`、`rules/`、`env/`、`queries/`。裝成 plugin 時是 `${CLAUDE_PLUGIN_ROOT}`（這個 plugin 的目錄；repo 裡是 symlink，從 git marketplace 安裝時已複製成實際檔案）；在 alert-trust repo clone 裡直接用（不裝 plugin，例如 opencode）時是 repo 根目錄。
- `$REF`：共用說明。plugin：`${CLAUDE_PLUGIN_ROOT}/references`；repo clone：`plugins/core/references`。
- `$AT/docs/` 只在 repo clone 有；裝成 plugin 時改讀網站，對照規則見 `$REF/five-sections.md#paths`。

## 流程

照 `$REF/five-sections.md` 做：先決條件 → 讀卡片 → 五段（架構 → 正確使用 → 正確觀測 → 不正確使用時的觀測（含 `bad_queries`）→ 目前無法觀測的痕跡）→ 回報。全程規則（唯讀、不連資料庫、隱私、數字標來源、引用證據）與深度界線也在那份。**訊號清單每次從卡片讀，不要從這份 skill 抄。**這份只寫 etcd 專屬的部分。

「怎麼監控」頁：`$AT/docs/targets/etcd.md`；元件頁：`$AT/docs/components/etcd.md`（產生的頁面，repo 裡要先跑 `tools/build_pages.py`；沒有就讀網站）。

## etcd 專屬

- 第一張 `model: native` 的卡：沒有 exporter 層，`up` 只代表 process 回應 HTTP。失去 quorum 時 `up` 仍是 1（卡片的 3 節點 lab），health-second-path 的三方交叉判斷最重要；健康端點與 leader metric 從卡片讀。
- component-count：成員數要和第 1 段的拓撲一致。

## 訪談（每輪最多 3 個單一問題，已知的跳過）

1. 叢集有幾個成員？是給誰用的（Kubernetes、Patroni、其他）？
2. 有沒有 blackbox 從另一條路徑探測健康端點？探測的是哪個端點、比對什麼內容？
3. scrape 的是 client port 還是獨立的 metrics port？

## 回報

照 `$REF/five-sections.md#output` 每個 target 一份五段表。被 `selfcheck` 或 `rule-review` 呼叫時，把結果交回它匯集；使用者直接呼叫時，最後列這張卡「建議先處理的 3 件事」。
