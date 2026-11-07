# 實驗報告

目錄裡標 <span class="at-chip at-ev-tested">實測</span> 的結論來自這些實驗。原始報告寫在環境 survey 裡，含有特定環境的資訊，正在去除後逐篇整理到這裡。

| 報告 | 涵蓋 | 狀態 |
|---|---|---|
| Prometheus Rule 信任邊際 | 鏈路盲點、pending 抖動、CPU／記憶體／IOPS／磁碟邊際、視窗矩陣、跨版本（2.51.2–3.15.1） | 整理中 |
| VictoriaMetrics Rule 信任邊際 | VM／VL single 與 cluster 的 26 個故障情境、延遲矩陣、log rule 視窗 | 整理中 |
| Alertmanager 監控指南 | 通知失敗計數、email 假成功、`group_interval` 對告警視窗的影響、0.27–0.34.2 版本 survey | 整理中 |

前身 repo [prometheus-selfcheck-skills](https://github.com/HahaSula/prometheus-selfcheck-skills) 的 `EVIDENCE.md` 與 `lab/` 已公開 Prometheus 部分的實驗數據與腳本。
