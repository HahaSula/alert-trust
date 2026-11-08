# alert-trust

**Rule 回報正常，不代表 alert 正確。**實驗中一條永遠成立的 alert 跑 25 分鐘都沒有 firing，而 `rule_evaluation_failures_total`、`iterations_missed_total` 都是 0，`up` 也是 1。

alert-trust 是一份 **monitor of monitor 元件目錄**，加上檢查 **rule 信任契約**的工具。它回答兩個問題：

1. 整條 alert 鏈壞在哪一段時，有沒有人知道？
2. 在實際尖峰下，rule 還能不能信？

網站：<https://hahasula.github.io/alert-trust/>

## 內容

| 位置 | 內容 |
|---|---|
| `catalog/components/` | 監控元件卡：Prometheus、Alertmanager、vmalert、vmagent、VictoriaMetrics、VictoriaLogs、外部觀察者 |
| `catalog/targets/` | Target 卡：exporter 加上它背後的服務，例如 MySQL／MariaDB |
| `catalog/contracts.yaml` | 信任契約（時間不等式） |
| `catalog/sources.yaml`、`catalog/sources.d/` | 每個結論的來源 |
| `rules/` | 依元件分的規則範本，每條都有 `runbook_url` 連到網站上的對應訊號 |
| `tools/` | 驗證目錄、產生網站頁面、蒐集快照（`collect.py`）、覆蓋檢查（`check_coverage.py`）、比對 metric 名稱 |
| `skills/`、`.claude-plugin/` | Claude Code plugin：每個元件一包 skill |
| `lab/` | 輕量實驗 |
| `docs/` | 網站內容；`docs/components/` 由目錄產生 |

每張卡的訊號分三級：**第 1 級**是基本 monitor of monitor，少一項那個元件壞掉時就沒有人知道；**第 2 級**是接近極限的預警；**第 3 級**用來查根因與規劃容量。

## 使用

```bash
pip install pyyaml
python3 tools/validate.py                      # 檢查目錄、規則與兩者之間的連結
python3 tools/build_pages.py                   # 由目錄產生網站頁面
python3 -m unittest discover -s tests          # 離線測試
python3 tools/check_metrics.py prometheus=http://localhost:9090/metrics
```

本機預覽網站：

```bash
python3 tools/build_pages.py
docker run --rm -it -p 8000:8000 -v "$PWD":/docs squidfunk/mkdocs-material:9.5.39
```

## Claude Code plugin

```text
/plugin marketplace add HahaSula/alert-trust
/plugin install alert-trust@alert-trust
/alert-trust:selfcheck
```

`selfcheck` 會先建立環境描述（`env-profile`），再唯讀蒐集快照、檢查覆蓋缺口，依元件交給 `prometheus`、`victoriametrics`、`alertmanager`、`database-targets` 各包處理，最後輸出報告。前身 prometheus-selfcheck-skills 的使用者可以直接改用這個 plugin。

不用 Claude 也能跑：

```bash
python3 tools/collect.py --out snapshot --prom prom-a=http://localhost:9090 --am am=http://localhost:9093
python3 tools/check_coverage.py --env env/<name>.yaml --snapshot snapshot --out report --live
```

## 新增一個元件或 target

1. 在 `catalog/components/` 或 `catalog/targets/` 新增 `<id>.yaml`，格式照 `catalog/components/prometheus.yaml`。
2. 引用的新來源放在 `catalog/sources.d/<id>.yaml`。
3. 規則放在 `rules/<id>/` 或 `rules/targets/`，每條要有 `labels.tier` 與 `annotations.runbook_url`。
4. 執行 `python3 tools/validate.py`，再用 `promtool check rules` 或 `vmalert -dryRun` 檢查規則。

## 和其他專案的關係

- **Prometheus／Alertmanager mixin、VM 官方規則、kube-prometheus**：元件本身健不健康。alert-trust 拿它們當第 2、3 級訊號的來源，並標出它們沒涵蓋的第 1 級訊號。
- **[awesome-prometheus-alerts](https://github.com/samber/awesome-prometheus-alerts)**：依元件收集的規則，用來比對門檻。
- **[cloudflare/pint](https://github.com/cloudflare/pint)**：單條 rule 的 linter。「規則在但沒資料」與它的 `promql/series` 是同一個概念；用實際尖峰值算時間契約，是 pint 沒有的部分。

## 公開範圍

這個 repo 與網站是公開的。可以放的內容：可重現的 lab 實驗、版本比較、機制、通用規則範本。**不要放的內容**：特定環境的數字、主機名稱、拓撲、內部決策，以及你的 `env/*.yaml` 和對真實環境跑出來的報告。這些檔案已列在 `.gitignore`。

## 證據、參考與限制

每個結論都標了證據等級（實測、讀碼、上游、推論），並附參考：

- `catalog/refs-code.yaml`：釘在版本與行號的原始碼位置，由 `tools/gen_code_refs.py` 依 `catalog/refs-code.src.json` 產生，改版時重跑，找不到關鍵字就失敗。
- `catalog/refs.yaml`：實驗結果的連結。
- 頁面寫 `[^key]`，`hooks/refs.py` 在建置時補上註腳；元件卡用 `refs: [key]`。
- `tools/validate.py` 會檢查每個參考都存在，並列出標了實測或讀碼卻沒有參考的項目（`--refs`）。

實驗都是小規模的單機 Docker，**驗證的是機制和訊號出現的順序**，門檻和時間不能直接套到大型環境。

前身是 [prometheus-selfcheck-skills](https://github.com/HahaSula/prometheus-selfcheck-skills)，只涵蓋 Prometheus 與 Alertmanager。

## 給 AI agent

在這個 repo 工作前先讀 [AGENTS.md](AGENTS.md) 與 [STATUS.md](STATUS.md)。

## License

[MIT](LICENSE)
