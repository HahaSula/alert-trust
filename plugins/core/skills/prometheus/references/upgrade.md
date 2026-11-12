# Prometheus 升級路徑

從 `prometheus` pack 進來。版本從快照 `<id>/buildinfo.json` 取（量測）。

**先做：** 查 [Prometheus 版本週期](https://prometheus.io/docs/introduction/release-cycle/) 確認目前有效的 LTS，並查 CHANGELOG。下面的表寫於 2026-11，版本狀態可能已變。

## 基本判斷

**建議升級，但不要等升級才修 rule。** 實測沒有 offset、視窗不足的 rule，在 2.51.2、2.55.1、3.13.5、3.15.1 都同樣無法 firing。

| 改善 | 解決什麼 | 版本 | 價值 |
|---|---|---|---|
| `query_offset` | Rule 查到還沒寫入的資料，不用逐條改表達式 | 2.53 | 高 |
| 官方回退點 | 3.x 出問題時有官方支援的退路 | 2.55 | 中，只影響升級安全 |
| `scrape_failure_log_file` | Scrape 失敗歷史查不到 | 2.55 | 中 |
| 自動 `GOMAXPROCS`、`GOMEMLIMIT` | Throttling、接近 limit 才 GC | 3.0 預設，2.x 可用 flag | 高，但不必升級就能開 |
| Rule 依賴判斷修正 | 無法判斷依賴時仍併行 | 3.1 | 只有開 `concurrent-rule-eval` 才相關 |
| `retention.percentage` | 以磁碟百分比設定 retention | 3.11 | 中 |
| 每台 Alertmanager 獨立 queue | 一台慢拖累全部 | 3.10（3.5 LTS 沒有） | 多台時有用 |
| LTS 支援 | 有修補可用 | 依版本 | 高 |
| Alert 恢復只查一次 | 重啟恢復 `for` 的查詢數 | 2.53 | 低，3 萬實例實測只差不到 1 秒 |

**升級解決不了：**
- Scrape 比 interval 慢、rule group 太重。相同負載實測下，2.51.2、2.55.1、3.13.5 的 CPU、scrape 與 rule 耗時沒有明顯差異，只有 2.55 以上記憶體約少 10%（30 萬 series，單次量測）。
- WAL 半筆寫入：截至 2026-11 的 main 仍未修復。
- Runtime 損毀時 `wal_corruptions_total` 為 0：修正在 3.16 rc。
- Reload timer 被 compaction 訊號重置：修正只在 main，還沒有正式版。

## 升級會改變執行模型嗎

單序的環節大多不變，所以對應的瓶頸升級後還在。

| 層級 | 2.51、2.55 | 3.x |
|---|---|---|
| Scrape、WAL、Head、查詢引擎 | 相同 | 相同 |
| Rule group | 預設單序；flag 併行時依賴判斷有問題 | 預設仍單序；3.1 修正依賴判斷，3.2 起分批併行 |
| Notifier | 一條 queue，等所有 Alertmanager | 3.4 起批次可調；**3.10 起每台各一條** |
| TSDB 維護 | 單一 goroutine | 仍是單一 goroutine；reload timer 問題仍在，實測 3.13.5 同樣歸零 |
| `GOMAXPROCS`、`GOMEMLIMIT` | 要開 feature flag | 3.0 起預設對齊 |

## 路徑

```text
現況 2.x ──► 2.55.x ──► 目前有效的 3.x LTS
```

**為什麼經過 2.55：**
- 官方文件寫明 3.x 的 TSDB 只保證 2.55 以上讀得懂，也只能回退到 2.55。
- 把風險拆開：到 2.55 的 PromQL 行為不變，可以先換成 `query_offset`；到 3.x 才面對不相容變更。

**實際差異比文件描述小。** 目前所有版本寫 index 都還是 `FormatV2`，實測 3.13.5 → 2.51.2 直接切換，資料筆數相同。但這不在官方保證內，範圍也有限（500 條 gauge、正常關機）。**不要拿這個當作跳過備份的理由。**

## 3.x 檢查清單

對照[官方遷移文件](https://prometheus.io/docs/prometheus/latest/migration/)，至少檢查：

- [ ] Range 與 lookback 改成左開區間，樣本少的視窗可能再少一個點。檢查 `R` 剛好是 I 整數倍的 rule。
- [ ] 正則的 `.` 會比對換行。
- [ ] `holt_winters` 改名，且要 feature flag。
- [ ] Scrape 協定更嚴格，Content-Type 有問題的 target 需要 `fallback_scrape_protocol`。
- [ ] `le`、`quantile` 的 label 值格式改變，影響手寫比對這些 label 的 rule 和 dashboard。
- [ ] 移除的 flag 與 feature flag。
- [ ] Alertmanager v1 API 已不支援。
- [ ] 用目標版本的 `promtool check rules` 和 `check config` 驗證。

## 上線步驟

1. 先把 monitor of monitor 上線，才能比較前後差異。
2. 用目標版本的 `promtool` 驗證設定與 rule。
3. 備份 TSDB，或確認 volume snapshot 可還原。
4. 先升非關鍵的 Prometheus，或用雙跑比較 alert 結果。
5. 比較 CPU、記憶體、scrape 耗時、group 耗時、`for` 重置次數。
6. 穩定後才升關鍵環境。

## 互動問題

1. 目前版本？能接受多長的維護時間？
2. 有用 remote write、remote read、Thanos 或其他相容元件嗎？它們的版本相容性要另外確認。
3. Rule 或 dashboard 有寫死 `le="1.0"` 這類 label 值嗎？
