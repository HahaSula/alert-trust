# 參與與擴充

這頁說明怎麼新增一個元件或 target、怎麼引用證據、怎麼加實驗。給 AI agent 的完整指引在 repo 根目錄的 `AGENTS.md`。

## 五段框架

每張卡（監控系統或被監控的 target）都回答同樣的五段。範圍的界線是深度：只到 exporter 或 endpoint 實際輸出的那一層。

| 段落 | 回答什麼 | 卡片欄位 | 依據 |
|---|---|---|---|
| 1. 架構 | 使用時會留下哪些痕跡 | `architecture`（連到架構頁的段落） | 原始碼 |
| 2. 正確使用 | 怎麼設定、限制在哪 | `usage: [{id, text, evidence, refs}]`，`evidence` 只能是 `tested`（本機小實驗）、`code`（原始碼推理）、`doc`（官方文件） | 原始碼推理、本機小實驗 |
| 3. 正確觀測 | 看到的數字是不是真的 | 訊號的 `section: observe` | 原始碼、本機小實驗 |
| 4. 不正確使用時的觀測 | 誤用或故障時看得到什麼，包含寫得不好的查詢（資料庫的 SQL／命令，監控系統的 PromQL、MetricsQL、LogsQL 與 rule） | 訊號的 `section: misuse` | 原始碼、本機小實驗 |
| 5. 目前無法觀測的痕跡 | 有痕跡但 exporter／endpoint 沒輸出 | `blind_spots`；方案寫在 `options`，一行、點到為止 | 原始碼；方案標為建議 |

驗證順序以第 2 段為起點：先依原始碼確認「該怎麼用」，再判斷哪些痕跡代表看得準（第 3 段）、哪些代表用錯了（第 4 段）。

**同一類訊號在監控系統與 target 的歸屬**：監控系統本身就是觀測的路徑，所以它的存活、重啟、設定 reload、rule 評估是否成功都屬於「正確觀測」；target 的重啟、OOM 則是「不正確使用時的觀測」。

同一個主題同時有「量得準不準」和「數值異常」兩面時，拆成兩個訊號：例如「複寫延遲量的是不是真的」是 `observe`，「延遲太大」是 `misuse`。`validate.py` 會列出還沒有 `section` 的訊號數（遷移中只是警告）。

## 新增一個監控元件或 target

1. 在 `catalog/components/`（監控元件）或 `catalog/targets/`（exporter 加上它背後的服務）新增 `<id>.yaml`，格式照 `catalog/components/prometheus.yaml`。必填：`id`、`name`、`kind`、`family`、`status`、`stages`、`role`、`signals`。
2. 每個訊號要有：`id`、`tier`（1 基本 monitor of monitor／2 預警／3 根因）、`question`、`signal`、`metrics`、`evaluate_on`、`evidence`、`source`、`refs`、`upstream`、`rules`。Target 卡另外要回答 `catalog/target-pattern.yaml` 的標準問題，訊號可用 `aspect` 分成 exporter、stability、usage、ha。
3. 規則放在 `rules/<id>/` 或 `rules/targets/`。每條要有 `labels.tier`（字串）與 `annotations.runbook_url`，指向 `https://hahasula.github.io/alert-trust/components/<卡片 id>/#<訊號 id>`。`x == 0` 這類資料消失時會回傳空的規則，要搭配 `absent`／`unless`，或用 `annotations.absence_covered_by` 指出補位的規則。
4. 新來源放 `catalog/sources.d/<id>.yaml`，新參考放 `catalog/refs.d/<id>.yaml`。
5. 執行：

```bash
python3 tools/validate.py
python3 tools/build_pages.py
python3 -m unittest discover -s tests
promtool check rules rules/<id>/*.yml
```

## 證據怎麼引用

| 證據 | 寫法 |
|---|---|
| 原始碼 | 加到 `catalog/refs-code.src.json`：`"key": [repo, tag, path, 關鍵字]`，再跑 `python3 tools/gen_code_refs.py` 產生釘在行號的連結 |
| 實驗 | 在 `catalog/refs.d/` 加 `kind: experiment`，`url` 指向 `lab/` 的結果檔 |
| 頁面 | 句子後面寫 `[^key]`，建置時自動變成註腳 |
| 元件卡 | 訊號或失效方式加 `refs: [key]` |

只有實際觀察到的結果才標 `tested`。圖說分開寫機制（原始碼）與這組數據（實驗條件）。

## 加一個實驗

- 放在 `lab/<名稱>/`：`README.md`（怎麼跑、限制）、`compose.yml`、腳本、`results/`（原始輸出與摘要）。
- 保持輕量：小容器、短時間、不做負載測試；用自己的 compose project 名稱與 port，結束後清掉。
- 每組條件只跑一次時，要說明相位或抖動造成的波動；能跑多組就跑多組。

## 公開範圍

可以放可重現的實驗、機制、版本比較與通用範本。不要放特定環境的數字、主機名稱、拓撲或內部決策；真實環境的 `env/*.yaml` 與報告不要 commit。
