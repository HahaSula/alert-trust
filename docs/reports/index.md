# 實驗報告

目錄裡標 <span class="at-chip at-ev-tested">實測</span> 的結論來自這些實驗。原始報告寫在環境 survey 裡，這裡是去除特定環境資訊後的公開版本：保留實驗結果、機制、版本比較與讀碼結論，環境數字改成標明「例子」的假設值。

所有數字都來自單機、小規模的實驗（多半是 Docker，每個情境只跑一次），用來說明機制和訊號出現的順序，不能直接當門檻。

| 報告 | 涵蓋 | 版本 |
|---|---|---|
| [Prometheus Rule 信任邊際](prometheus-trust.md) | 鏈路盲點、pending 抖動、CPU／記憶體／IOPS／磁碟邊際、視窗矩陣、blackbox 與 K8s 驗證、告警規則、升級與跨版本比較 | 2.51.2–3.15.1 |
| [VictoriaMetrics Rule 信任邊際](victoriametrics-trust.md) | VM／VL single 與 cluster 的 26 個故障情境、延遲矩陣、log rule 視窗、假 resolved、meta 規則、operator 注意事項、參數附錄 | VM v1.155.0、VL v1.53.0 |
| [Alertmanager 監控指南](alertmanager.md) | 通知失敗計數、email 假成功、`group_interval` 對告警視窗的影響、告警規則、0.27–0.34.2 版本 survey 與升級 | 0.27.0–0.34.2 |
| [Alert Pending 抖動分析](pending-jitter.md) | 慢 scrape 讓 pending 反覆重置的機制、參數關係、rate 與 irate、通知送出路徑、rule group 執行模型、修正的資源影響 | 2.51.2、2.55.1、3.15.1 |

Prometheus 部分的實驗腳本、原始結果與 `EVIDENCE.md` 在 [`lab/prometheus/`](https://github.com/HahaSula/alert-trust/tree/main/lab/prometheus)（從前身 repo prometheus-selfcheck-skills 搬來）；VictoriaMetrics、Alertmanager、資料庫與 pending 抖動的實驗在 `lab/` 的其他子目錄。
