# 檢查 rule 檔的流程

拿到別人的 rule 檔時，**先確認監控基礎設施，再看每條 rule**。只看 rule 的時間契約，會漏掉更根本的問題：負責評估這些 rule 的 Prometheus 或 vmalert 自己變慢、掛掉、寫不進去時，沒有人知道，rule 寫得再對也沒用。

<!-- figure: rule-review-flow -->

**圖**　`rule-review` 的四個步驟。第 2 步和第 3 步之間是一道關卡：基礎設施的第 1 級缺口沒有處理前，後面的結論都要加註。
{: .at-caption }

## 怎麼跑

用 Claude Code plugin：

```text
/alert-trust:rule-review my-rules/*.yml
```

不用 skill、自己跑工具：

```bash
# 第 2 步，只有檔案時（離線）
python3 tools/rules_to_snapshot.py --out snapshot --ruler prom-a my-rules/*.yml --ruler meta meta-rules/*.yml
python3 tools/check_coverage.py --env env/<name>.yaml --snapshot snapshot --out report

# 第 2 步，連得到 ruler 時（可以檢查 selector 有沒有資料）
python3 tools/collect.py --out snapshot --prom prom-a=http://localhost:9090
python3 tools/check_coverage.py --env env/<name>.yaml --snapshot snapshot --out report --live
```

第 3 步的公式見[信任契約](../contracts.md)，可以用[契約計算器](calculator.md)代入數字。

## 為什麼順序不能反過來

- **體系決定公式。**Prometheus 與 vmalert 的視窗契約不同，Prometheus 2.53 以前也沒有 `query_offset`。不先確認體系，第 3 步會用錯公式。
- **基礎設施壞了，rule 一起失效而且不報錯。**實驗中 scrape 比 interval 慢時，一條永遠成立的 alert 跑 25 分鐘都沒有 firing，`rule_evaluation_failures_total` 是 0、`up` 是 1。只有 scrape 耗時與 rule group 耗時的告警看得出來。
- **離線也能做第 2 步。**`rules_to_snapshot.py` 把 rule 檔轉成快照，覆蓋檢查器就能找出「缺少」與「只在自己身上」；「規則在但沒資料」要連到真的 ruler（`--live`）。
