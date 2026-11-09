# alert-trust

**Rule 回報正常，不代表 alert 正確。**實驗中一條永遠成立的 alert 跑 25 分鐘都沒有 firing，而 `rule_evaluation_failures_total`、`iterations_missed_total` 都是 0，`up` 也是 1。

alert-trust 回答兩個問題：

1. **整條 alert 鏈壞在哪一段時，有沒有人知道？**這是 monitor of monitor。每個元件一張[元件卡](components/index.md)，列出分級的訊號：第 1 級少一項，那個元件壞掉時就沒有人會知道。
2. **在實際尖峰下，rule 還能不能信？**這是[信任契約](contracts.md)：一組時間不等式，可以用[計算器](tools/calculator.md)代入自己的數字。

## 怎麼用

| 你想做的事 | 從這裡開始 |
|---|---|
| 拿某個元件當監控 infra，要知道最少該看什麼 | [監控元件 → 元件卡總覽](components/index.md)，看第 1 級訊號 |
| 檢查別人的 rule 檔 | [檢查 rule 檔的流程](tools/rule-review.md)：先確認監控體系與基礎設施，再看每條 rule |
| 寫 rule 前確認 range、offset、`for` 夠不夠 | [信任契約](contracts.md)、[契約計算器](tools/calculator.md) |
| 想知道某個元件內部哪裡會卡住 | [架構總覽：單序點對照](architecture/index.md) |
| 監控資料庫：穩定、效能、HA、叢集、備份、容量 | [MySQL／MariaDB](targets/mysql.md)、[PostgreSQL](targets/postgres.md) |
| 收到告警，想知道它在保護什麼 | 告警的 `runbook_url` 會連到對應的訊號 |
| 檢查自己的環境缺什麼 | [共同模型與檢查流程](model.md#flow) |

## 和其他專案的關係

| 專案 | 做什麼 | alert-trust 怎麼用它 |
|---|---|---|
| Prometheus／Alertmanager mixin、VM 官方規則、kube-prometheus | 各元件本身健不健康 | 當第 2、3 級訊號的來源；每張卡標出它們沒涵蓋的第 1 級 |
| [awesome-prometheus-alerts](https://github.com/samber/awesome-prometheus-alerts) | 依元件收集的規則 | 參考與比對門檻 |
| [cloudflare/pint](https://github.com/cloudflare/pint) | 單條 rule 寫得對不對 | 「規則在但沒資料」與它的 `promql/series` 同概念；時間契約是 pint 沒有的部分 |

alert-trust 疊在這些專案之上：它們回答「元件健康嗎」「這條 rule 語法對嗎」，alert-trust 回答「整條鏈有沒有人在看」「尖峰時還能不能信」。

## 證據與參考

每個結論都附參考，點註腳就能看到依據：原始碼連到**釘在版本與行號**的位置，實驗連到結果檔。標「未公開」的實驗，原始報告還含有特定環境的資訊，整理後會公開。

證據分四級：<span class="at-chip at-ev-tested">實測</span> 故障注入實驗量到、<span class="at-chip at-ev-code">讀碼</span> 由原始碼或官方文件確認、<span class="at-chip at-ev-upstream">上游</span> 來自上游官方規則、<span class="at-chip at-ev-inferred">推論</span> 尚未單獨驗證。

實驗都是小規模的單機 Docker，**驗證的是機制和訊號出現的順序**，門檻和時間不能直接套到大型環境。
