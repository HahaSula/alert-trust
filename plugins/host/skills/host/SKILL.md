---
name: host
description: Host and container pack of alert-trust (node_exporter, cAdvisor / kubelet, kube-state-metrics). Reads the host card from catalog/ at run time and checks it in the five sections (architecture, correct usage, correct observation, observation of misuse, blind spots), stopping at what the exporters expose; database cards point their host-level blind spots here. 主機與容器檢查：照五段讀 host 卡，只到 node_exporter、cAdvisor、kube-state-metrics 的輸出。
argument-hint: "[env/<name>.yaml]"
---

# 主機與容器（alert-trust-host）

檢查資料庫與監控元件所在的主機與容器資源。卡片：`host`（`$AT/catalog/components/host.yaml`，索引在 `$AT/catalog/map.yaml` 的 `cards.host`）。

## 路徑

- `$AT`：底下有 `catalog/`、`tools/`、`rules/`、`env/`。裝成 plugin 時是 `${CLAUDE_PLUGIN_ROOT}`（這個 plugin 的目錄；repo 裡是 symlink，從 git marketplace 安裝時已複製成實際檔案）；在 alert-trust repo clone 裡直接用（不裝 plugin，例如 opencode）時是 repo 根目錄。
- `$REF`：共用說明。plugin：`${CLAUDE_PLUGIN_ROOT}/references`；repo clone：`plugins/core/references`。
- `$AT/docs/` 只在 repo clone 有；裝成 plugin 時改讀網站，對照規則見 `$REF/five-sections.md#paths`。

## 流程

照 `$REF/five-sections.md` 做：先決條件 → 讀卡片 → 五段 → 回報。host 卡不是 target 卡，不套 `target-pattern.yaml`：第 3 段只看卡片 `section: observe` 的訊號。**訊號清單每次從卡片讀。**

## 主機專屬

- 深度界線：只到 node_exporter、cAdvisor（或 kubelet `/metrics/cadvisor`）、kube-state-metrics 的輸出，不進 Linux 核心；機制只引用 `$AT/docs/architecture/db-resources.md#host-layer`。
- 各資料庫卡的 `blind_spots` 有 `covered_by: host` 的項目（連線、記憶體、IO、磁碟等主機層的痕跡），由這個 skill 回答；被資料庫 skill 或上層 skill 呼叫時，先處理它們列出的項目。
- 先確認每台主機與容器有沒有被抓：只看資料庫或監控元件自己的 metric，主機層的失效（OOM、磁碟唯讀、記憶體接近 limit）看不到。
- 規則範本 `$AT/rules/host/`；job 名稱與 label（`instance`、`container`、`pod`）要對到使用者的 config。

## 訪談（每輪最多 3 個單一問題，已知的跳過）

1. 部署在實體機／VM、Docker 還是 Kubernetes？
2. 有沒有 node_exporter、cAdvisor 或 kubelet cadvisor、kube-state-metrics？job 名稱是什麼？
3. 容器有沒有設記憶體與 CPU limit？

## 回報

照 `$REF/five-sections.md#output` 一份五段表，交回呼叫的上層 skill 匯集。
