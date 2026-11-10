# MongoDB 監控實驗

回答：Percona mongodb_exporter v0.54.0 在 MongoDB 8.0.34 的 3 member replica set 上，預設輸出什麼、帳號權限不足或伺服器忙時哪些 metric 安靜地消失、選舉與失去多數時「角色」與「延遲」怎麼變，以及 PMM 與 awesome-prometheus-alerts 的規則在這個版本上會不會響。
結果用在 [MongoDB 怎麼監控](../../../docs/targets/mongodb.md)、[元件卡](../../../catalog/targets/mongodb_exporter.yaml)、[文件型架構](../../../docs/architecture/db-doc.md)。

## 環境

| 元件 | 版本／設定 |
|---|---|
| MongoDB | `mongo:8.0.34` ×3（m1～m3），replica set `rs0`，`--wiredTigerCacheSizeGB 0.25`、`--oplogSize 2`（2MB）、`--setParameter enableTestCommands=1`（`sleep` 指令要用）、keyFile 驗證；每台 `mem_limit` 640MB |
| exporter | `percona/mongodb_exporter:0.54.0`：e1～e3 每 member 一個（`--collector.diagnosticdata --collector.replicasetstatus --compatible-mode`）；e-default（不加旗標）、e-norole（帳號只有 app 庫的 read）、e-all（`--collect-all`）都指向 m1；e-multi 一個 exporter 帶三個 URI（`/scrape?target=` 與 `/scrapeall`） |
| 帳號 | exporter：admin 的 `clusterMonitor`＋local 的 `read`（README 的建議）；appreader：只有 `app` 的 `read` |
| Prometheus | `prom/prometheus:v3.13.5`，`scrape_interval` 5s、`scrape_timeout` 4s，規則是執行時複製進去的 `rules/targets/mongodb.yml` 與 `prom/rules/upstream.yml`（PMM v3.10.0 範本與 awesome dd70d7b 照抄），只開 `127.0.0.1:32090` |

compose project 是 `at-mongo-lab`，對外只用 port 32090。全部容器約 1GB 記憶體。

```bash
./run.sh    # 約 20 分鐘，results/run-<日期>.txt；結束會 docker compose down -v 並列出殘留的容器與 volume（應為 0）
```

`results/run-20261110.txt` 是引用的那一次完整執行。另外保留兩份原始輸出：`aborted-run-20261110.txt`（背景 writer 用了預設的 majority write concern，E 情境中卡住、中止；結尾附中止前查到的預設 write concern）、`run-20261110-a.txt`（keyFile 在 restart 時寫不進去，G 與 H 的「重新啟動後」無效；已在 compose 修正後重跑。只引用它 G2 的一點：一個 member 已停、再 pause 另一個時 `/scrapeall` 整個逾時）。

`q.py` 是查 Prometheus 與直接抓 exporter 的小工具（`q.py snap` 每個 member 一列）。

## 情境

| 代號 | 做法 |
|---|---|
| A | 基線：五種 exporter 設定的 metric 名稱與 sample 數；沒有 clusterMonitor 的 exporter；上游規則用到的 metric 與 label 在不在；閒置時的延遲；`rs_state` label；上游規則在健康叢集上的狀態 |
| C | `--oplogSize 2` 下寫入 2000 筆 5KB：oplog window、什麼時候截斷 |
| D | m3 `db.fsyncLock()` 40 秒，primary 每 100ms 寫一筆：延遲（三個 exporter 各自的看法）、flow control、w:majority |
| E | m2、m3 都 `fsyncLock` 70 秒，primary 每 50ms 寫一筆 5KB（w:1）：majority commit point、wtimeout、沒有 wtimeout 的 w:majority、flow control、oplog 大小 |
| F | primary 上 `db.adminCommand({sleep:1, secs:12, lock:"w"})`（global X 鎖）：每 member 一個的 exporter、`/scrape?target=`、`/scrapeall` |
| G | `docker compose kill` primary：新 primary 出現的時間（survivor 的 `hello`）、角色計數、延遲 series、上游規則；重新啟動後 |
| G2 | `docker compose pause` 一個 secondary 25 秒：它的 exporter、multi-target、`/scrapeall` |
| H | 停掉兩個 secondary（primary 還活著）：primary 自己降級的時間、寫入、`mongodb_up`、角色規則、延遲 series |

## 結果摘要

| 情境 | `up` | `mongodb_up` | 角色 | 延遲 series | 其他 |
|---|---|---|---|---|---|
| A 預設旗標 | 1 | 1 | — | — | 46 個 sample（只有 `mongodb_up`、go、process） |
| A 沒有 clusterMonitor | 1 | 1 | — | — | 49 個 sample；log 只有 replSetGetStatus 的 ERROR；primary 的 user assert 每分鐘 +374 |
| A 三個旗標 | 1 | 1 | 1 個 PRIMARY | 每個 secondary 被 3 個 exporter 各報一份 | 6108 個 sample；閒置時延遲在 0 與 10 之間跳 |
| C 2MB oplog 寫 10MB | 1 | 1 | — | — | window 95 秒；150 秒內沒截斷（10.6MB） |
| D m3 `fsyncLock` | 1 | 1 | 不變 | 26～35（三個 exporter 各自的看法） | flow control 0 |
| E m2、m3 都 `fsyncLock` | 1 | 1 | 不變 | 67～70 | commit point 落後 70 秒；不指定 WC 的 insert 8 秒沒回；`isLagged` 1、`targetRateLimit` 100；oplog 5.4MB → 6.7MB（上限 2MB） |
| F primary 持 global X 鎖 12 秒 | 1 | **不存在**（3.0 秒、44 個 sample） | 之後被選掉 | — | `/scrapeall` 少了 m1；約 25 秒後新 term，m1 降級 |
| G kill primary | 1 | 0 | 9.5 秒後新 primary | 那台消失 | awesome 的 PrimaryChanged 不響（`rs_state` 換 label） |
| G2 pause secondary | 1 | 0（3.0 秒） | 不變 | 那台消失 | `/scrapeall` 少那台；`-a` 兩台連不上時整個逾時 |
| H kill 兩個 secondary | 1 | 存活的 1 | 12.3 秒後 primary 降級 | `-a` 0 條 | 寫入 `NotWritablePrimary`；NoPrimary 規則都響 |

上游規則：PMM 的 tickets 與 oplog window（24h）從開機到結束一直 firing；PMM 的 replication lag 是 Prometheus 的語法錯誤；awesome 的 tickets（`txn_rw_type`）、lag 與 headroom（`on (set)`）永遠空，primary changed 從沒響過。

## 已知限制

- **這台機器的容器單調時鐘比牆鐘慢約 30%。**`electionTimeoutMillis`、heartbeat、checkpoint 間隔、Prometheus 的 `for` 與 `scrape_duration_seconds` 都用單調時鐘，所以 G、H 的秒數比真實環境長；結論以原始碼為準，秒數只當量級。主機的 `date` 與 mongod log 的時間是牆鐘。
- 只有 replica set。sharded cluster（mongos、config server、balancer、chunk 搬移）沒有做，相關結論只讀碼。
- 寫入量很小（每秒 10～20 筆），WiredTiger cache 的 eviction 壓力、ticket 排隊、連線額滿沒有重現；這些只讀碼。
