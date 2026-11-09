# Valkey／Redis 監控實驗

回答：redis_exporter v1.97.0 在 Valkey 9.1.3 主從加 Sentinel 上，哪些故障看得到、哪些在最需要時消失；Valkey 和 Redis 7.2／8.10 的 metric 差在哪裡。
結果用在 [Valkey／Redis 怎麼監控](../../../docs/targets/valkey.md)、[元件卡](../../../catalog/targets/redis_exporter.yaml)、[記憶體 KV 架構](../../../docs/architecture/db-kv.md)。

## 環境

| 元件 | 版本／設定 |
|---|---|
| Valkey | `valkey/valkey:9.1.3-alpine`：vk1 主節點（`maxclients 40`、`repl-backlog-size 1mb`、`repl-timeout 5`、`latency-monitor-threshold 100`）、vk2 複本、kg 單機（key group 測試，`save ""`、slowlog 門檻 1ms） |
| Sentinel | s1～s3（Valkey 9.1.3），quorum 2、`down-after-milliseconds 10000`、`failover-timeout 30000` |
| Redis | `redis:7.2.17-alpine`、`redis:8.10.3-alpine` 各一台單機，只比對 metric 名稱與預設值 |
| exporter | `oliver006/redis_exporter:v1.97.0`，每台一個；e-vk1／e-vk2 用 `--script=queries/redis_exporter/valkey-extra.lua`；另有 `--connection-timeout=2s` 的 e-short、壞 script 的 e-bad、multi-target 的 e-multi、Sentinel 的 e-s1、key group 的 e-kg；第二輪另加 heartbeat 的 e-hb 與沒有 CONFIG 權限的 e-noconf |
| Prometheus | `prom/prometheus:v3.13.5`，`scrape_interval` 5s、`scrape_timeout` 4s，只開 `127.0.0.1:27090` |

compose project 是 `at-vk-lab`，port 只用 27001（vk1，給連線風暴）與 27090。vk2 同時接兩個網路：從 default 拔掉時複寫與 Sentinel 斷線，exporter（只在 mon）仍抓得到它。

```bash
./run.sh    # 約 6 分鐘：A～L，results/run-<日期>.txt
./run2.sh   # 約 4 分鐘：R0～R5，results/run2-<日期>.txt（profile run2、prometheus-run2.yml）
```

兩個腳本結束時都 `docker compose down -v` 並列出 `at-vk-` 開頭的殘留容器（應為空）。

## 情境

| 代號 | 做法 |
|---|---|
| A | 基線；Valkey／Redis 7.2／8.10 的 metric 名稱差異；mixin 依賴的 `redis_total_system_memory_bytes` |
| B | `DEBUG SLEEP 0.05` ×3、`0.3` ×1（vk1 與 r72）：slowlog、latency monitor |
| C | `BLPOP` 25 秒：blocked clients |
| D | `DEBUG POPULATE` 後把 maxmemory 設成比用量少 2MB：noeviction → volatile-lru（沒有 TTL）→ allkeys-lru |
| E | vk1 `DEBUG SLEEP 7`：預設 exporter、2s 逾時的 exporter、multi-target |
| F | 從主機開 45 條連線（maxclients 40）15 秒 |
| G | vk2 從網路拔掉 25 秒，vk1 期間寫 3MB（backlog 1MB） |
| H | vk1 的 `/data` 改成 555 後 `BGSAVE`；觀察 MISCONF 與 Sentinel |
| I | `docker pause` 主節點 25 秒 |
| J | 停掉 s2、s3 25 秒 |
| K | 壞 script 的 exporter |
| L | kg 放 40 萬個 key，`--check-key-groups` 與 `--count-keys` 的成本 |
| R0 | Sentinel 是否在 TILT |
| R1 | heartbeat key（主節點每秒 `SET at:heartbeat <TIME>`，複本端 `--check-single-keys`），vk2 斷線 20 秒 |
| R2 | ACL 拿掉 `config` 的 exporter 帳號 |
| R3 | BGSAVE 失敗時每 4 秒讀 `SENTINEL MASTER` |
| R5 | `docker pause` 主節點 45 秒 |
| R4 | `docker compose restart vk2` |

## 已知限制

- **這台機器的容器時鐘比牆鐘慢約 30%，而且會跳動。**Sentinel 每 25～30 秒進一次 TILT（R5 的 log 有 5 次 `+tilt`），所以 I、J、R3、R5 的 sdown、odown、failover **都沒有發生**；這些行為只讀碼。TILT 本身與「TILT 期間 status 仍是 ok」是實測。
- `DEBUG SLEEP 7` 的 7 秒是容器時鐘；主機牆鐘量到的 HTTP 回應是 9.5 秒。
- D 的填充資料用 `DEBUG POPULATE`，不會複寫到 vk2；複本端的記憶體沒有對照意義。
- Cluster 模式、AOF、碎片、過期清理沒有做實驗。

結果摘要在 `results/summary.md`。
