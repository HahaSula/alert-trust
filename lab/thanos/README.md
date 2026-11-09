# Thanos 輕量 lab

驗證 Thanos 元件卡（`catalog/components/thanos.yaml`）裡和 Ruler、Querier、Compactor 有關的說法。一個 compose project（`at-thanos`），沒有負載，整輪約 9 分鐘。

| 元件 | 版本 | Host port |
|---|---|---|
| Prometheus（external labels `cluster=lab, replica=a`，2h block、關掉本地 compaction） | v3.13.5 | 25090 |
| Thanos sidecar、store、query、rule、compact | v0.42.5 | 25091–25096 |
| Alertmanager（null receiver） | v0.27.0 | 25094 |

物件儲存用 Thanos 的 `FILESYSTEM`（共用 volume），沒有用 MinIO：這幾個情境只需要 bucket 存在，不需要 S3 API。

## 執行

```bash
python3 lab/thanos/run.py      # 結果寫到 lab/thanos/results/run.json 與 run.log；結束時 docker compose down -v
```

中斷時手動清理：`docker compose -p at-thanos -f lab/thanos/compose.yml --profile compact down -v`。

## 怎麼量

- Ruler 的 rule 檔（`configs/rules.yml`）有兩個 group，`g-abort`（`partial_response_strategy: abort`）與 `g-warn`，各兩條永遠成立的 alert：
  `LabSeriesPresent`＝`count(up{job="prometheus"}) > 0`（要讀 sidecar 的資料）、`LabConstant`＝`vector(1)`（不讀任何 StoreAPI）。
- Ruler `--eval-interval=15s`、`--resend-delay=15s`，所以 `EndsAt = 評估時間 + 4 × 15s = +60s`。
- 每個情境後抓一次 Ruler `/api/v1/rules`（health、lastError）、Alertmanager `/api/v2/alerts`（active 的 alert 與 `startsAt`、`updatedAt`、`endsAt`）、
  Querier `/api/v1/query`（`partial_response=false` 與 `true`）、Querier 的 `thanos_store_nodes_grpc_connections`、Ruler 的
  `prometheus_rule_evaluation_failures_total` 與 `thanos_rule_evaluation_with_warnings_total`。
- 這台機器的容器時鐘不準（見 `lab/vm/README.md`），這裡只用**狀態與數值**下結論，秒數只是量級。

## 結果

完整取樣在 `results/run.json`，摘要在 `results/run.log`。每個情境只跑一次。

### S1 store loss

**停掉 sidecar（store gateway 還在，但沒有涵蓋最近時間的 block）：**

| 取樣 | Querier `partial_response=false` | Ruler health | Alertmanager |
|---|---|---|---|
| 停止前 | success，`1` | 4 條都 `ok` | 4 條 active |
| +2s | 逾時（driver 等 10s） | 4 條都 `ok` | 4 條 active |
| +22s | **success，空結果** | 4 條都 `ok` | **兩條 `LabSeriesPresent`（abort 與 warn）都消失**，`LabConstant` 照常續送 |
| +62s | success，空結果 | 4 條都 `ok` | 同上 |

- 整個過程 abort group 只失敗 1 次、warn group 只有 1 次 warning（`thanos_rule_evaluation_with_warnings_total{strategy="warn"} 1`），都在 sidecar 還在 querier 名單上的那幾秒；之後兩種策略都正常評估、結果為空。
- `LabSeriesPresent` 在 EndsAt（+60s）之前就消失，表示 Ruler 送的是**真正的 resolved**，不是過期。
- Querier 的 `thanos_store_nodes_grpc_connections{store_type="sidecar"}` 在 +2s 就消失，只剩 `store_type="store"`。

**再停掉 store gateway（querier 一個 StoreAPI 都沒有）：**

- Querier `partial_response=false` 回 422 `No StoreAPIs available`；`partial_response=true` 回 success、空結果。
- `g-abort/LabSeriesPresent` 變 `err`，lastError 是 `no query API server reachable`（**看不出真正原因是沒有 store**）；`g-warn` 照常 `ok`。
- `thanos_store_nodes_grpc_connections` 整條消失（不是 0）。
- 兩條 `vector(1)` 照常 firing：不讀 StoreAPI 的 Watchdog 看不到 store 全部消失。

恢復後兩條 `LabSeriesPresent` 重新 firing，`startsAt` 是新的時間（新的 alert）。

### S2 querier down

停掉 querier，Ruler 的 4 條 rule 在下一輪評估都變 `err`（`no query API server reachable`），`prometheus_rule_evaluation_failures_total` 兩個 strategy 都增加；`thanos_rule_evaluation_with_warnings_total` 不動。

| 取樣 | Ruler | Alertmanager active |
|---|---|---|
| +15s | abort 2 條 `err`，warn 還沒評估 | 4 |
| +30s | 4 條 `err` | 4（`endsAt` 仍是最後一次送出 +60s） |
| +45s | 4 條 `err` | 2（abort 兩條的 `endsAt` 已過） |
| +60s 到 +120s | 4 條 `err` | **0** |

- Alertmanager 在最後一次送出的 `EndsAt` 之後把 4 條都當成 resolved；Ruler 這段時間沒有送任何東西，內部狀態也沒有變（querier 回來後 `startsAt` 沿用原本的值，例如 `LabConstant` 仍是第一次 firing 的時間）。
- 這和 vmalert、Prometheus 查詢失敗時的假 resolved 相同。

### S3 compactor halt

用 `promtool tsdb create-blocks-from openmetrics` 做一個 block，補上 Thanos 的 `meta.json` 欄位，再複製成另一個 ULID，放進 bucket，讓兩個 block 的時間範圍與 external labels 完全相同。

- Compactor（`--wait --consistency-delay=0s`）第一輪就 log `critical error detected; halting ... pre compaction overlap check: overlaps found`。
- 60 秒後：`thanos_compact_halted 1`、`thanos_compact_iterations_total 0`、`thanos_compact_todo_compactions 1`、`thanos_compact_group_compactions_failures_total 1`；**容器仍在 running，HTTP `/metrics` 照樣回應**（所以 `up` 是 1）。

## 限制

- 每個情境只跑一次，沒有 HA 的 querier、沒有 strict endpoint、沒有 Receive。
- 沒有測 sidecar 上傳中斷（要等 2h block）；相關說法只讀碼。
- Alertmanager 用 null receiver，只看 `/api/v2/alerts` 的 active 清單，沒有看通知內容。
