# 引用證據與標示數字

## 每個結論都要指回目錄

目錄裡每個 signal、contract、silent_failure 都有：

| 欄位 | 意思 | 怎麼解析 |
|---|---|---|
| `evidence` | 證據等級 | `tested` 實測、`code` 讀碼、`upstream` 上游、`inferred` 推論 |
| `source` | 來源 key | `$AT/catalog/sources.yaml` 或 `$AT/catalog/sources.d/*.yaml` 的 `sources.<key>`；`visibility: private` 的只寫名稱，不寫連結 |
| `refs` | 參考 key 清單 | `$AT/catalog/refs.yaml`（實驗）或 `$AT/catalog/refs-code.yaml`（釘在版本與行號的原始碼）的 `refs.<key>.url` |
| `rules` | 規則範本 | `$AT/rules/<path>#<AlertName>` |

報告裡的寫法：

```markdown
- vmalert 查詢失敗時約 2 分鐘後假 resolved（實測）[^exp-vm-faults]。
  訊號：[vmalert#rule-errors](https://hahasula.github.io/alert-trust/components/vmalert/#rule-errors)；
  範本：`rules/victoriametrics/meta.yml#MetaRuleErrors`

[^exp-vm-faults]: VictoriaMetrics 實驗：…（從 refs.yaml 的 title 與 url 抄）
```

- 訊號的網站連結：`https://hahasula.github.io/alert-trust/components/<card id>/#<signal id>`。
- 契約的網站連結：`https://hahasula.github.io/alert-trust/contracts/#<contract id>`。
- 名詞（`scrape_interval`、`eval_delay`……）的設定位置與預設值：`$AT/catalog/terms.yaml`，網站在 `reference/terms/`。
- 註腳 key 直接用目錄的 ref key，不要自己編號，下次對話才對得起來。
- 目錄沒有依據的判斷，寫「推論」，不加註腳。

## 數字的三種來源

| 標籤 | 用在 | 要附什麼 |
|---|---|---|
| `量測` | 快照檔案或你跑的查詢 | 檔名與欄位，例如 `prom-a/peaks.json: scrape_duration_peak{job="node"}`，以及查詢範圍 |
| `使用者說` | 使用者口述、env 檔裡手填的值 | 誰說的不用寫，但要寫是口述 |
| `推算` | 代入公式得到的值 | 公式與代入的每個值，每個值本身也要標來源 |

- 尖峰只代表查詢範圍內抓得到的值；`max_over_time` 看不到沒被抓到的那幾次。
- 目錄裡的門檻（例如記憶體 80%、比值 0.8）是實驗值，標「實驗值」並提醒規模不同要重新確認。
- 實驗都是小規模單機 Docker，驗證的是機制與訊號出現的順序，時間不能直接套到大型環境。
