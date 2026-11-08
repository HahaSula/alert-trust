# Pending 抖動：多組數據

驗證[視窗契約](../../docs/contracts.md#range-window)背後的機制，不只重現某一組數字。

**機制來自原始碼**（見各圖的註腳）：

- Sample 的時間戳是 scrape 開始的時間，要等 scrape 結束、寫入後才查得到。
- 查詢視窗內不足 2 個 sample 時 `rate` 回傳空。
- Evaluation 成功但結果裡沒有這個 alert 時，pending 被刪除，`for` 從頭計時。

**這個 lab 量的是**：在不同的 `scrape_interval`（S）、scrape 耗時（d）、range（R）、offset（O）下，rule 每次執行時實際看得到幾個 sample，有多少比例回傳空，alert 有沒有 firing，pending 被重新開始幾次。並和模型（`combos.py` 的 `predict_empty_fraction`）的預測比較。

## 怎麼跑

```bash
./run.sh                 # 約 26 分鐘；DURATION=秒 可調整
```

1 個假 exporter（`exporter.py`，回應延遲由 scrape params 決定）、Prometheus 2.51.2 與 3.13.5 各一台。每個組合是一個 job 加一個 rule group：

- `probe:visible_samples`：每次 evaluation 記下 `count_over_time(...)`，也就是 rule 當下看得到的 sample 數。
- `KnownTrue_<組合>`：`rate(...) >= 0`，永遠成立，有足夠 sample 就應該 firing。

結果寫在 `results/run-<日期>.md` 與 `.json`。

## 限制

- 用 HTTP 延遲模擬 scrape 耗時，而 `scrape_timeout` 不能大於 `scrape_interval`，所以只能做 `d < S`。`d > S`（tick 被丟掉、實際間隔拉長）的情況要用真的大 target 才做得出來。
- 每個組合只跑一次，evaluation 與 scrape 的相位由 Prometheus 自己決定，所以實測比例會在預測值附近波動。
- 模型用左開右閉的視窗（3.x 的語意）；2.x 的 range 是閉區間，邊界上剛好有一個 sample 時會多算一個。
