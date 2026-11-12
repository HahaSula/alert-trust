# 自檢輸出格式

同一份結果輸出兩種格式，放在 `reports/<name>-<YYYYMMDD>/`（和 `coverage.md`、`coverage.json` 同一層）。

## selfcheck-report.md（給人）

```markdown
# 監控自檢報告：<env name>，<日期>

## 結論
這些 alert 目前能不能信，一到三句話。

## 環境摘要
元件、版本、模式（single／cluster、HA 台數）、觀察者與故障域、DMS。
每個數字標「量測／使用者說／推算」。

## 必須先處理
最多 5 項，依「判斷優先順序」排序。每項：
- 問題（附訊號或契約連結與證據等級）
- 影響哪些 alert
- 怎麼修（附 rules/ 範本或設定片段）
- 怎麼驗證修好了

## 元件 plugin
| target 或主機 | 卡片 | plugin | 檢查方式（元件 skill／repo 裡直接讀／只做通用的正確觀測） | 沒裝時要裝的 plugin |
只做通用檢查的列，寫明第 2、4、5 段未檢查，以及安裝後要執行的 `/<plugin>:<skill>`。

## 邊際總表
| 元件 | 契約 | 代入值 | 邊際 | 狀態 |
邊際依契約定義（右邊 ÷ 左邊），大於 1 才成立。代入值逐一標來源。

## 依元件分組的缺口
從 coverage.json 帶入，每個元件一節：第 1 級全部列出，第 2、3 級只列非 covered 的。
| 訊號 | 級 | 狀態 | 評估在哪裡 | 建議 |

## 建議順序
先修不會觸發的 rule → 補「沒有人知道」的缺口 → 處理接近失效的邊際 → 升級與第 3 級訊號。

## 沒有確認的部分
查不到的資料、errors.json 裡的失敗、使用者答「未知」的題目、needs_input 還沒回答的訊號、沒有實驗依據的推論。

## 參考
註腳（目錄的 ref key）。
```

## selfcheck-report.json（給下一次對話）

```json
{
  "env": "env/<name>.yaml",
  "date": "YYYY-MM-DD",
  "snapshot": "selfcheck-data/<name>-<YYYYMMDD>",
  "coverage": "reports/<name>-<YYYYMMDD>/coverage.json",
  "catalog_commit": "<$AT 的 git rev-parse --short HEAD；裝成 plugin 時寫 plugin.json 的 version；都拿不到就寫 unknown>",
  "verdict": "一句話結論",
  "must_fix": [
    {"id": "vmalert-biz/watchdog", "component": "vmalert-biz", "card": "vmalert", "signal": "watchdog",
     "contract": "resend-endsat", "problem": "...", "fix": "...", "verify": "...", "refs": ["exp-vm-resend"]}
  ],
  "margins": [
    {"component": "prom-a", "contract": "range-window", "object": "<group>/<alert>",
     "inputs": {"R": {"value": "5m", "source": "measured", "from": "prom-a/rules.json"}},
     "margin": 0.8, "status": "fail"}
  ],
  "gaps": [{"component": "am", "signal": "delivery-failing", "tier": 1, "status": "missing"}],
  "plugins": [{"target": "db-a", "card": "mysql", "plugin": "alert-trust-mysql", "checked": "generic_observe",
               "install": "alert-trust-mysql"}],
  "unknowns": ["觀察者的故障域"],
  "next": ["下一次要先確認的事"]
}
```

`source` 用 `measured`、`user`、`inferred`。`plugins[].checked` 用 `skill`（呼叫了元件 skill）、`repo`（repo 裡直接讀 SKILL.md）、`generic_observe`（只做通用的正確觀測）；`install` 只在 `generic_observe` 時寫。`status` 用 `ok`、`warn`（邊際 1 到 1.25，或比值超過 0.8）、`fail`、`unknown`。
