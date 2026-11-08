# 參與與擴充

這頁說明怎麼新增一個元件或 target、怎麼引用證據、怎麼加實驗。給 AI agent 的完整指引在 repo 根目錄的 `AGENTS.md`。

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
