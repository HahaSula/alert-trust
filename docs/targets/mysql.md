# MySQL／MariaDB 本體怎麼監控

[元件卡](../components/mysqld_exporter.md)回答「exporter 真的有在量嗎」。這頁回答另一個問題：**MySQL 與 MariaDB 本身該看哪些訊號**，每個訊號從資料庫的哪裡來、mysqld_exporter v0.21.0 有沒有輸出、預設有沒有開、要什麼權限。

依據是 mysqld_exporter、MySQL 8.4.7、MariaDB 11.4.13 的原始碼與官方手冊，加上一組小實驗：兩種資料庫各一組 primary + async replica（GTID），開長交易、做 deadlock、讓 replica 延遲三種方式、讓 replication 因衝突停下、讓 semi-sync 退回 async、打滿連線、kill -9 primary、做一次 failover 並讓舊 primary 回來。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha]叢集、資料保護與容量三個面向另外做了一組：三台 Group Replication 與三台 Galera 各暫停兩台、MariaDB 沒有 replica 時的 binlog 過期、MySQL 的 information_schema 統計快取。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp]

## 先講結論

1. **連線打滿時，連線數規則會消失。**max_connections 用完時 exporter 的一般帳號也收到 Error 1040，`mysql_up` 變 0，`threads_connected` 消失，「連線數 / max_connections > 0.8」回傳空。看得到的只有 `mysql_up` 規則，以及連線降下來之後 `connection_errors_total{error="max_connections"}` 的增加量。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha][^mysql-conn-reserved]
2. **Seconds_Behind 在 replica 斷線時是 NULL。**IO thread 停了、SQL thread 已追完，`Seconds_Behind_Source` 就是 NULL，exporter 不輸出；heartbeat 延遲同時已經 27–29 秒。延遲要用 heartbeat 表，Seconds_Behind 只當輔助。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha][^mysql-sbs-null]
3. **failover 會讓三種東西失效。**升級的 replica 做了 `RESET REPLICA ALL`，所有 `mysql_slave_status_*` 消失；靜態的 `role="primary"` label 指向舊節點；heartbeat 寫入程式若是 primary 上的 EVENT，在新 primary 上是停用的，heartbeat 從此停住。舊 primary 回來時兩台都可寫，只有「從 metric 推角色、按 cluster 計數」的規則看得到。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha]
4. **errant GTID 完全看不到。**在 replica 上誤寫一筆後，MySQL 的 `GTID_SUBSET(replica, primary)` 變 0，MariaDB 的 `gtid_binlog_pos` 出現 replica 自己的 server_id，但 exporter 只輸出 `gtid_mode` 這類開關，`gtid_executed`、`gtid_binlog_pos` 是字串、直接被跳過。MariaDB replica 的 `Gtid_Slave_Pos` 有被拆成 `mysql_slave_status_gtid_slave_pos{domain_id,server_id}`，但那是「套用到哪裡」，errant 交易不在裡面。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha][^my-global-variables-parse][^my-trace-ex-slave-gtid]
5. **Semi-sync 退回 async 時 commit 照常成功。**唯一的變化是 `rpl_semi_sync_*_status` 從 1 變 0。MariaDB 內建 semi-sync，沒開時 status 也是 0，所以要和 `enabled == 1` 一起看。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha]
6. **MySQL 與 MariaDB 的同一個訊號名稱不同。**Undo history、deadlock 在 MariaDB 是預設就有的 global status；MySQL 沒有這兩個 status，只能開 `info_schema.innodb_metrics`，而且 deadlock 的 metric 在 MySQL 有 `_total`、在 MariaDB 沒有。規則要用 `or` 把兩種名稱接起來。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha][^my-innodb-metrics-name][^mariadb-innodb-deadlocks]
7. **很多關鍵訊號預設沒開。**長交易（`info_schema.innodb_trx`）、MySQL 的 undo history 與 deadlock（`info_schema.innodb_metrics`）、metadata lock 等待（`info_schema.processlist`）、heartbeat、每帳號連線數（`sys.user_summary`、`mysql.user`）都預設關。預設只開 6 個 collector。[^my-default-collectors]
8. **失去 quorum 時 exporter 照常回應。**Group Replication 三台暫停兩台後，倖存節點自己仍是 ONLINE、`mysql_up` 1，寫入卻卡住（client 逾時的那筆在恢復後其實 commit 了）；Galera 倖存節點變 non-Primary、讀取回 ERROR 1047。要用成員數或 `wsrep_cluster_status` 判斷，不能靠 `mysql_up`。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp]
9. **單機 MariaDB 的 binlog 不會過期刪除。**`slave_connections_needed_for_purge` 預設 1，沒有 replica 連著時 `binlog_expire_logs_seconds` 到期也不刪，binlog 會一直長到磁碟滿。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp][^my-asp-mdb-purge-needs-slave]
10. **備份與損毀，exporter 都看不到。**沒有備份 collector，頁面損毀沒有 status 計數；備份要由備份 job 自己輸出最後成功時間，損毀要看 error log。MySQL 的 information_schema 統計（auto_increment、表大小）最多晚一天。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp][^my-asp-my-is-stats-expiry]

## 誰看得到什麼

```mermaid
flowchart LR
  subgraph PRI[Primary]
    HBW["heartbeat 寫入程式<br/>（pt-heartbeat 或 EVENT）"]
    GS1["SHOW GLOBAL STATUS<br/>SHOW GLOBAL VARIABLES"]
  end
  subgraph REP[Replica]
    RS["SHOW REPLICA STATUS<br/>（MariaDB：SHOW ALL SLAVES STATUS）"]
    HBT["heartbeat 表（複製過來）"]
  end
  HBW -- binlog --> HBT
  RS -->|"*_running、last_sql_errno<br/>Seconds_Behind：IO 停時是 NULL"| R1((規則))
  HBT -->|"now - stored<br/>IO、SQL 停都會變大：最可靠"| R1
  GS1 -->|"read_only、uptime、連線數<br/>連線打滿時整組消失"| R2((規則))
  RS -.->|"gtid_executed 是字串<br/>exporter 不輸出"| X((盲點))
```

## 九個面向一覽 {#aspects}

訊號依[元件卡](../components/mysqld_exporter.md)的 `aspect` 分成九組，每節最後有一張**證據鏈**表：規則 → 資料庫原始碼（值在哪裡產生）→ 表或變數 → exporter 原始碼與 SQL → metric。更細的逐行追蹤在[附錄](mysql-appendix.md)。

| 面向 | 訊號數 | 主要盲點 | 一節 |
|---|---|---|---|
| 監測自檢 | 10 | exporter 或資料庫停機時 series 整組消失 | [exporter](#exporter) |
| 可用性 | 3 | 單次 OOM kill 只看得到 uptime 歸零 | [availability](#availability) |
| 工作量與效能 | 7 | 執行中的語句是哪一個（MariaDB 沒有 digest、quantile） | [workload](#workload) |
| 資源與飽和 | 8 | 連線打滿時連線數 metric 消失 | [saturation](#saturation) |
| 複寫與 HA | 8 | Seconds_Behind 斷線時是 NULL；errant GTID 看不到 | [replication](#replication) |
| 叢集與共識 | 6 | 狀態是 label 或字串；沒開 Galera 的 MariaDB 也有 wsrep_* | [cluster](#cluster) |
| 資料維護與完整性 | 5 | 頁面損毀沒有任何 metric | [maintenance](#maintenance) |
| 資料保護 | 3 | exporter 沒有備份 metric | [protection](#protection) |
| 容量與成長 | 5 | MySQL 的 information_schema 統計值最多晚一天 | [capacity](#capacity) |

## 監測自檢（exporter） {#exporter}

exporter 有三層「活著」：`up`、`mysql_up`、`mysql_exporter_collector_success`，每一層壞掉時下一層的 series 整組消失。規則、門檻與實驗都在[元件卡](../components/mysqld_exporter.md)的 exporter 一組，這頁不重複。和資料庫本體有關的只有兩點：

- **連線打滿時 exporter 自己也進不來**，`mysql_up` 變 0，所以飽和類規則要搭配 `MySQLNotReachable`（見[資源與飽和](#saturation)）。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha]
- **exporter 的 SQL 與原始碼一致**，`general_log` 記到的語句和 collector 檔案裡的常數一字不差。[^exp-mysql-trace]

## 叢集與共識 {#cluster}

「叢集與共識」在這裡指**成員之間要達成多數才能寫入**的架構，和 primary／replica 的 async 複寫不同：

| | MySQL Group Replication（InnoDB Cluster） | MariaDB Galera |
|---|---|---|
| 共識 | XCom（Paxos 系）；每筆交易先在群組內 certification | Galera 的 group communication；每筆 writeset 在各節點 certification |
| 多數 | 每個成員自己判斷：`members − unreachable ≤ members / 2` 時擋住寫入[^my-asp-my-gr-quorum][^my-asp-my-gr-blocked] | 不在 Primary component 的節點拒絕讀寫（`wsrep_cluster_status` = non-Primary） |
| 成員狀態在哪 | `performance_schema.replication_group_members.MEMBER_STATE`：ONLINE、RECOVERING、OFFLINE、ERROR、UNREACHABLE[^my-asp-my-grm-state-col][^my-asp-my-grm-unreachable] | `wsrep_cluster_status`、`wsrep_cluster_size`（server 端）[^my-asp-mdb-wsrep-status-var][^my-asp-mdb-wsrep-size-set]、`wsrep_local_state`（provider） |
| Flow control | applier／certifier 佇列超過門檻（預設 25000）就節流[^my-asp-my-gr-fc-applier][^my-asp-my-gr-fc-certifier] | 收到的佇列超過 `gcs.fc_limit` 就送 pause，整個叢集等最慢的節點 |
| mysqld_exporter | `perf_schema.replication_group_members`、`perf_schema.replication_group_member_stats`（都預設關）[^my-asp-ex-def-grm] | 全在 `SHOW GLOBAL STATUS`，預設就有；provider 的計數經 `wsrep_show_status` 帶出[^my-asp-mdb-wsrep-provider-vars][^my-asp-mdb-wsrep-provider-status] |

| 要看什麼 | 資料庫的來源 | exporter metric（collector） | 預設 | 權限 | 證據 |
|---|---|---|---|---|---|
| GR 成員狀態 | `replication_group_members` | `mysql_perf_schema_replication_group_member_info{member_host,member_state,member_role,...}` = 1 | 關 | SELECT on performance_schema | 實測[^my-asp-exp][^my-asp-ex-grm-labels] |
| GR 佇列 | `replication_group_member_stats`（只查本機） | `mysql_perf_schema_transactions_in_queue`、`..._remote_in_applier_queue` | 關 | 同上 | 實測[^my-asp-exp][^my-asp-ex-grms-query] |
| GR 門檻 | `group_replication_flow_control_*_threshold` | `mysql_global_variables_group_replication_flow_control_applier_threshold` 等 | 開 | — | 實測：兩個都是 25000[^my-asp-exp] |
| Galera 能不能服務 | `wsrep_ready`、`wsrep_local_state` | `mysql_global_status_wsrep_ready`、`..._local_state` | 開 | — | 實測[^exp-mysql][^my-trace-mdb-wsrep-ready] |
| Galera quorum | `wsrep_cluster_status`、`wsrep_cluster_size` | `mysql_global_status_wsrep_cluster_status`（Primary → 1）、`..._cluster_size` | 開 | — | 實測[^my-asp-exp][^my-asp-ex-parse-nonprimary] |
| Galera flow control | `wsrep_flow_control_paused_ns`、`_sent`、`wsrep_local_recv_queue` | `mysql_global_status_wsrep_flow_control_paused_ns` 等 | 開 | — | 讀碼[^my-asp-gal-fc-paused-ns][^my-asp-gal-recv-queue]；實測：有輸出[^my-asp-exp] |

### Group Replication

- **成員狀態是 label。**collector 把每一列的所有欄位都變成 label、值固定 1，所以狀態一變，舊 series 消失、新 series 出現。規則要寫成「`member_state!="ONLINE"` 的 series 存在」，不能寫 `== 0`。<span class="at-chip at-ev-code">讀碼</span>[^my-asp-ex-grm-labels][^my-asp-ex-grm-metric]實驗中每條 series 有 8 個 label（含 `member_id`、`member_version`、`member_communication_stack`）。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp]
- **失去多數時 exporter 照常回應。**三台暫停兩台 24 秒後，倖存的 gr1 看到自己 ONLINE／PRIMARY、另外兩台 UNREACHABLE，`mysql_up` 1、collector 成功；這時 gr1 的 INSERT 卡住，5 秒後 client 逾時。恢復後 29 秒內三台回到 ONLINE，**那筆逾時的 INSERT 其實 commit 了**（表裡有 1 列）。所以「client 逾時」不等於「沒寫入」，`MySQLGroupQuorumLost` 用 UNREACHABLE 的數量判斷，不看自己的狀態。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp]
- **`group_replication_unreachable_majority_timeout` 預設 0**（實驗輸出確認），失去多數的成員會一直等，不會自己退出。
- **被踢出的成員從其他成員的表裡消失**，不會留一列 ERROR；所以另外比「看到的成員數比一小時內少」（`MySQLGroupMembersMissing`）。<span class="at-chip at-ev-code">讀碼</span>（[MySQL 手冊](https://dev.mysql.com/doc/refman/8.4/en/group-replication-monitoring.html)）
- **社群版看不到節流本身。**8.4.7 社群版的 global status 有一組 `Gr_*` 共識計數（實驗中 exporter 輸出 `mysql_global_status_gr_all_consensus_proposals_count` 等 22 個），但沒有 `Gr_flow_control_*`；那是 Enterprise component 提供的，社群版原始碼的 `components/` 裡沒有。只能看佇列長度對門檻的比例。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp]
- **member_stats 只輸出本機**（`WHERE MEMBER_ID=@@server_uuid`），每台都要抓。[^my-asp-ex-grms-query]

### Galera（只有 MariaDB）

- **沒開 Galera 的 MariaDB 也有 wsrep_*。**實驗中普通 MariaDB 輸出 `wsrep_ready` 0、`wsrep_cluster_status` 0（Disconnected）、`wsrep_cluster_size` 0，上游規則 `wsrep_ready != 1` 對每台普通 MariaDB 誤報。所有 Galera 規則都要 `and on (job, instance) mysql_global_variables_wsrep_on == 1`。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql][^my-asp-exp][^my-asp-mdb-wsrep-disconnected]
- **non-Primary 的節點拒絕讀取，但 exporter 照常。**三台暫停兩台 29 秒後，gal1 是 `non-Primary`、size 1、`wsrep_ready` OFF、`wsrep_local_state` 0（Initialized）；exporter 輸出 `wsrep_cluster_status` 0、所有 collector 成功；`SELECT` 回 `ERROR 1047 WSREP has not yet prepared node for application use`。恢復後兩台回來、size 回到 3。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp]
- **字串狀態怎麼變成數字**：MariaDB server 依 provider 狀態把 `wsrep_cluster_status` 設成 `Primary`、`non-Primary` 或 `Disconnected`[^my-asp-mdb-wsrep-primary][^my-asp-mdb-wsrep-nonprimary]；exporter 的 `parseStatus` 把 `primary` 轉 1、`non-primary`／`disconnected` 轉 0。[^my-trace-ex-parse-primary][^my-asp-ex-parse-nonprimary]`wsrep_flow_control_active` 是 `true`／`false`，exporter 不認得，被跳過。[^my-asp-gal-fc-active]
- **flow control 用 `_paused_ns` 的速率。**`wsrep_flow_control_paused` 是「自上次 FLUSH STATUS 以來」的比例，越久越鈍；`wsrep_flow_control_paused_ns` 是累計奈秒，`rate()/1e9` 就是這段時間的暫停比例。<span class="at-chip at-ev-code">讀碼</span>[^my-asp-gal-fc-paused-ns][^my-asp-gal-fc-paused]
- **flow control 沒有重現。**把 gal2 限到 0.05 核、`gcs.fc_limit=1` 後在 gal1 寫 3000 筆單列交易，`flow_control_paused_ns` 與 `flow_control_sent` 都維持 0，寫入時間和不限 CPU 時一樣（43／44 秒，瓶頸是 client 逐筆送）。規則的證據停在讀碼。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp]

### 證據鏈 {#chain-cluster}

| 規則 | DB 原始碼 | 表／變數 | exporter 原始碼與 SQL | metric |
|---|---|---|---|---|
| `MySQLGroupMemberNotOnline`、`MySQLGroupMembersMissing` | MySQL[^my-asp-my-grm-state-col][^my-asp-my-grm-unreachable] | `performance_schema.replication_group_members.MEMBER_STATE` | `SELECT * FROM performance_schema.replication_group_members`，欄位全變 label[^my-asp-ex-grm-query][^my-asp-ex-grm-labels] | `mysql_perf_schema_replication_group_member_info` |
| `MySQLGroupQuorumLost` | 多數判斷[^my-asp-my-gr-quorum] | 同上（`UNREACHABLE` 的數量） | 同上 | 同上 |
| `MySQLGroupApplierQueueHigh` | 欄位[^my-asp-my-grms-queue-col][^my-asp-my-grms-applier-col]、門檻[^my-asp-my-gr-fc-applier][^my-asp-my-gr-fc-certifier] | `replication_group_member_stats.COUNT_TRANSACTIONS_*`、`group_replication_flow_control_*_threshold` | `... WHERE MEMBER_ID=@@server_uuid`[^my-asp-ex-grms-query][^my-asp-ex-grms-queue][^my-asp-ex-grms-applier] | `mysql_perf_schema_transactions_*_queue` |
| `MySQLGaleraNotReady` | MariaDB[^my-trace-mdb-wsrep-ready][^my-trace-mdb-wsrep-ifdef] | `wsrep_ready` | `SHOW GLOBAL STATUS`[^my-trace-ex-gs-query] | `mysql_global_status_wsrep_ready` |
| `MySQLGaleraOutOfSync` | provider 狀態[^my-asp-mdb-wsrep-provider-status] | `wsrep_local_state` | 同上 | `mysql_global_status_wsrep_local_state` |
| `MySQLGaleraNonPrimary` | MariaDB[^my-asp-mdb-wsrep-status-var][^my-asp-mdb-wsrep-primary][^my-asp-mdb-wsrep-nonprimary] | `wsrep_cluster_status` | 字串轉 1／0[^my-asp-ex-parse-nonprimary][^my-asp-ex-gs-wsrep-status] | `mysql_global_status_wsrep_cluster_status` |
| `MySQLGaleraClusterShrunk` | MariaDB[^my-asp-mdb-wsrep-size-var][^my-asp-mdb-wsrep-size-set] | `wsrep_cluster_size` | `SHOW GLOBAL STATUS` | `mysql_global_status_wsrep_cluster_size` |
| `MySQLGaleraFlowControl` | Galera 26.4.27[^my-asp-gal-fc-paused-ns][^my-asp-gal-fc-sent][^my-asp-gal-recv-queue] | `wsrep_flow_control_paused_ns` | 同上 | `mysql_global_status_wsrep_flow_control_paused_ns` |

實驗觀察：GR 與 Galera 的成員狀態、quorum 兩條鏈都在 lab 裡看到 metric 變化；flow control 只看到 metric 存在、值維持 0。[^my-asp-exp]

## 複寫與 HA {#replication}

<a id="ha"></a>

| 要看什麼 | 資料庫的來源 | exporter metric（collector） | 預設 | 權限 | 證據 |
|---|---|---|---|---|---|
| IO／SQL thread | `SHOW REPLICA STATUS`（MariaDB 先試 `SHOW ALL SLAVES STATUS`） | `mysql_slave_status_slave_*_running`（MariaDB）、`..._replica_*_running`（MySQL 8.4） | 開 | MySQL：REPLICATION CLIENT；MariaDB ≥ 10.5.9：SLAVE MONITOR | 實測[^exp-mysql-ha][^my-slave-queries] |
| 停下的原因 | `Last_SQL_Errno`、`Last_IO_Errno` | `mysql_slave_status_last_sql_errno`、`..._last_io_errno` | 開 | 同上 | 實測 |
| 延遲（伺服器算的） | `Seconds_Behind_Master`／`_Source` | `mysql_slave_status_seconds_behind_master`／`_source` | 開 | 同上 | 實測、讀碼[^mysql-sbs-compute] |
| 延遲（heartbeat） | heartbeat 表（pt-heartbeat 格式） | `mysql_heartbeat_now_timestamp_seconds`、`mysql_heartbeat_stored_timestamp_seconds` | 關 | SELECT on heartbeat 表；另外要有寫入程式 | 實測[^my-heartbeat-query] |
| 每個 applier worker 的時間戳 | `performance_schema.replication_applier_status_by_worker` | `mysql_perf_schema_*`（perf_schema.replication_applier_status_by_worker） | 關 | SELECT on performance_schema；**MariaDB 沒有這張表，collector 失敗** | 實測[^my-applier-worker] |
| 能不能寫 | `read_only`、`super_read_only`（MySQL） | `mysql_global_variables_read_only`、`..._super_read_only` | 開 | — | 實測 |
| GTID 開關 | `gtid_mode`、`enforce_gtid_consistency`、`gtid_strict_mode`（MariaDB） | `mysql_global_variables_gtid_mode` 等；只有 ON／OFF 會輸出 | 開 | — | 實測[^my-parse-status] |
| GTID 內容 | `gtid_executed`（MySQL）、`gtid_binlog_pos`（MariaDB） | **沒有**（字串被跳過） | — | — | 實測[^my-global-variables-parse] |
| MariaDB replica 的 GTID 位置 | `SHOW ALL SLAVES STATUS` 的 `Gtid_Slave_Pos`、`Gtid_IO_Pos` | `mysql_slave_status_gtid_slave_pos{domain_id,server_id}`（slave_status 把 GTID 拆成 label） | 開 | SLAVE MONITOR | 實測：在 mdb2 的名稱清單裡[^my-trace-ex-slave-gtid][^my-trace-mdb-gtid-slave-pos][^exp-mysql-ha] |
| Semi-sync | `Rpl_semi_sync_master_status`（MariaDB 內建）、`Rpl_semi_sync_source_status`（MySQL plugin） | `mysql_global_status_rpl_semi_sync_*_status`、`..._no_tx`、`..._clients` | 開（plugin 載入後才有） | — | 實測 |

Galera 與 Group Replication 不是 primary／replica 的 async 複寫，放在[叢集與共識](#cluster)。

### 延遲：heartbeat 與 Seconds_Behind

`Seconds_Behind_Source` 是「現在時間減去 SQL thread 正在套用的事件在 primary 上的時間戳」。[^mysql-sbs-compute]它只在 SQL thread 在跑時有值；SQL thread 追完 relay log 時，IO thread 在跑就是 0、不在跑就是 NULL。[^mysql-sbs-null]MariaDB 的 `Seconds_Behind_Master` 更容易是 NULL：只要 IO thread 不是 `Yes`（`Connecting`、`Preparing`、`No`）就是 NULL，即使 relay log 還有沒套用完的事件。<span class="at-chip at-ev-code">讀碼</span>[^my-trace-mdb-sbm-field][^my-trace-mdb-sbm-null]

其他和 replica 狀態有關的解析細節：<span class="at-chip at-ev-code">讀碼</span>

- MariaDB 的 `Slave_IO_Running` 可能是 `Preparing`，exporter 不認得這個值，該次 scrape 的 series 會短暫消失（推論，未觀察到）。[^my-trace-mdb-io-running-vals][^my-parse-connecting]
- MySQL 8.4 上 slave_status 每次 scrape 先試 8 條舊語法才成功，exporter 帳號的 ER_PARSE_ERROR 每次 scrape 加 8，屬正常現象。[^my-trace-ex-slave-loop][^exp-mysql-trace]
- 以「`.數字`」結尾的字串（如 binlog 檔名 `mysql-bin.000003`）會被跳過，所以沒有 `master_log_file` 這類 metric。[^my-trace-ex-parse-logre]

| 情境 | Seconds_Behind | heartbeat 延遲 | 結果 |
|---|---|---|---|
| SQL thread 等 metadata lock（primary 做 ALTER，replica 有開著的交易） | 21／28 秒 | 21／28 秒 | 一致 |
| IO thread 停 25 秒 | **消失（NULL）** | 27–29 秒 | 只有 heartbeat 看得到 |
| SQL thread 停 20 秒 | **消失（NULL）** | 21–23 秒 | 只有 heartbeat 看得到 |
| 衝突寫入，SQL thread 因 1062 停下 | **消失** | 持續變大 | `last_sql_errno=1062` 帶出原因 |
| primary 的 heartbeat 寫入程式停 25 秒 | 0 | **每台**都 25–28 秒 | heartbeat 誤報，但「每台一起變大」本身就是訊號 |

數字是 MariaDB／MySQL。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha]

heartbeat 的前提是寫入程式活著，而且跟著 primary 走。實驗用 primary 上的 EVENT 寫 heartbeat；EVENT 複製到 replica 時是 SLAVESIDE_DISABLED，failover 後新 primary 不會寫，heartbeat 從此停住。正式環境用 pt-heartbeat 之類的外部程式，由 HA 工具在升級時啟動。

### 角色與 failover

```mermaid
flowchart TD
  A["read_only == 0？"] -->|否| R[replica 或維護中]
  A -->|是| B["有 mysql_slave_status_* series？"]
  B -->|有| W["可寫的 replica：MySQLReplicaWritable"]
  B -->|沒有| P[primary]
  P --> C["每個 cluster 數 primary"]
  C -->|0，且所有節點都連得上| N[MySQLNoWritablePrimary]
  C -->|大於 1| M[MySQLMultipleWritable]
```

- **不要用靜態的 role label 判斷角色。**failover 後 `role="primary"` 指向已停的舊節點，`MySQLPrimaryReadOnly` 查不到任何東西。從 metric 推：可寫、而且沒有 replica 狀態的就是 primary。cluster 要自己用 relabel 加。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha]
- **舊 primary 回來時兩邊都能寫。**實驗中舊 primary 重啟後 `read_only=0`，兩個 cluster 各有 2 台可寫、不在複製的節點，`MySQLMultipleWritable` firing。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha]
- **連線打滿時 `read_only` 也會消失。**一開始的 `MySQLNoWritablePrimary` 在 primary 被打滿時誤報（read_only 不見了，看起來像沒有 primary），規則改成「有節點連不上時不判斷」，交給 `MySQLNotReachable`。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha]
- **Replica 要擋得住寫入。**MySQL 用 `super_read_only=ON`（root 也擋）；MariaDB 沒有 super_read_only，有 READ ONLY ADMIN 權限的帳號可以繞過 `read_only`。在 replica 上寫入時 replication 照常，要等 primary 寫到同一筆才停下。
- **primary 被 kill 時**，replica 的 `io_running=0`、`last_io_errno=2003`；primary 回來後 30 秒內自動重連，errno 歸 0。所以 `last_io_errno` 不適合單獨當告警，規則只用 `last_sql_errno`。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha]

### GTID 一致性

errant transaction（replica 有、primary 沒有的交易）會讓下一次 failover 出錯，exporter 卻看不到。要另外跑查詢（cron、HA 工具或 blackbox 的自訂 script）：

- MySQL：在 primary 上 `SELECT GTID_SUBSET('<replica 的 gtid_executed>', @@gtid_executed)`，0 代表有 errant。（[MySQL 手冊](https://dev.mysql.com/doc/refman/8.4/en/gtid-functions.html)）
- MariaDB：看 replica 的 `gtid_binlog_pos` 有沒有出現 replica 自己的 server_id（實驗中變成 `0-2-645`）；`gtid_strict_mode=ON` 可以在源頭擋掉一部分。（[MariaDB GTID](https://mariadb.com/kb/en/gtid/)）

### Semi-sync

實驗中 primary 開 semi-sync、replica 沒開：MySQL 開啟當下 status=1，第一筆寫入等 ACK 逾時（3 秒）後變 0；之後寫入正常、不報錯，`no_tx` 一直增加。MariaDB replica 開 semi-sync 後 status 回到 1、`clients=1`。MySQL replica 開著 `super_read_only` 時 `INSTALL PLUGIN` 會失敗（ERROR 1290），要先關掉再裝。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha]

### MySQL 8.4 的改名

MySQL 8.4 移除 `SHOW SLAVE STATUS`，exporter 改用 `SHOW REPLICA STATUS`，metric 名稱跟著欄位名變成 `mysql_slave_status_replica_io_running`、`..._seconds_behind_source`。MariaDB 11.4 仍是 `slave_*`、`seconds_behind_master`。規則要兩種名稱一起比對。[^my-slave-queries]


### 證據鏈 {#chain-replication}

| 規則 | DB 原始碼 | 表／變數 | exporter 原始碼與 SQL | metric |
|---|---|---|---|---|
| `MySQLReplicationNotRunning` | MySQL[^my-trace-my-io-running-field][^my-trace-my-io-running-val]、MariaDB[^my-trace-mdb-io-running-vals] | `Replica_IO_Running`、`Slave_SQL_Running` | 依序試 `SHOW ALL SLAVES STATUS`…`SHOW REPLICA STATUS`[^my-slave-queries][^my-trace-ex-slave-loop]；`Connecting` → 0[^my-parse-connecting] | `mysql_slave_status_{slave,replica}_{io,sql}_running` |
| `MySQLReplicationError` | MySQL[^my-trace-my-last-sql-errno]、MariaDB[^my-trace-mdb-last-sql-errno] | `Last_SQL_Errno` | 同上 | `mysql_slave_status_last_sql_errno` |
| `MySQLReplicaStatusMissing` | — | 同上（整組消失） | 非 replica 時沒有任何列[^my-slave-skip] | `mysql_slave_status_*` |
| `MySQLHeartbeatLag` | pt-heartbeat 寫入的表（不是資料庫內建） | `heartbeat.heartbeat.ts` | `SELECT UNIX_TIMESTAMP(ts), UNIX_TIMESTAMP(NOW(6))`[^my-heartbeat-query][^my-trace-ex-hb-now] | `mysql_heartbeat_*_timestamp_seconds` |
| `MySQLReplicationLag` | MySQL[^my-trace-my-sbs-field][^mysql-sbs-null]、MariaDB[^my-trace-mdb-sbm-field][^my-trace-mdb-sbm-null] | `Seconds_Behind_Source`／`_Master` | NULL 解析失敗、不輸出[^my-trace-ex-parse-float] | `mysql_slave_status_seconds_behind_*` |
| `MySQLReplicaWritable`、`MySQLNoWritablePrimary`、`MySQLMultipleWritable` | MySQL[^my-trace-my-readonly][^my-trace-my-super-readonly]、MariaDB[^my-trace-mdb-readonly] | `read_only`、`super_read_only` | `SHOW GLOBAL VARIABLES`[^my-trace-ex-gv-query] | `mysql_global_variables_(super_)read_only` |
| `MySQLSemiSyncDegraded` | MySQL[^my-trace-my-semisync-status]、MariaDB[^my-trace-mdb-semisync-status] | `Rpl_semi_sync_*_status` | `SHOW GLOBAL STATUS` | `mysql_global_status_rpl_semi_sync_*_status` |

## 可用性 {#availability}

<a id="stability"></a>

| 要看什麼 | 資料庫的來源 | exporter metric（collector） | 預設 | 權限 | 證據 |
|---|---|---|---|---|---|
| 服務在不在 | 連線 + ping | `mysql_up`（exporter 本體） | 開 | 能登入即可 | 實測[^exp-mysql] |
| 重啟、crash、OOM kill | `Uptime` | `mysql_global_status_uptime`（global_status） | 開 | — | 實測：kill -9 後 uptime 歸零[^exp-mysql-ha] |
| primary 能不能寫 | `read_only` | `mysql_global_variables_read_only`（global_variables） | 開 | — | 實測[^exp-mysql-ha] |

- **OOM kill 在 metric 上只是一次重啟。**`uptime` 歸零、`mysql_up` 短暫變 0；原因要看 kernel log 或容器事件。停機短於 `MySQLNotReachable` 的 `for` 時，只有 `MySQLRestarted` 會響；起不來的 crash loop 則相反，只有 `mysql_up` 規則看得到，見 [OOM kill](#perf-oom)。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha][^exp-mysql-perf]
- **`mysql_up` 只做 ping**，不代表寫得進去；真正的寫入要外部探測，或 heartbeat 寫入程式自己回報成功時間。
- Group Replication 失去多數、Galera 變成 non-Primary 時 `mysql_up` 仍是 1，寫入或讀取卻被擋住，見[叢集與共識](#cluster)。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp]

| 規則 | DB 原始碼 | 表／變數 | exporter 原始碼與 SQL | metric |
|---|---|---|---|---|
| `MySQLNotReachable` | — | 連線與 `SELECT @@version` | 開連線失敗就讓 `mysql_up` = 0[^my-trace-ex-up][^my-trace-ex-version-query] | `mysql_up` |
| `MySQLRestarted`、`MySQLRestartedRepeatedly` | MySQL[^my-trace-my-uptime]、MariaDB[^my-trace-mdb-uptime] | `Uptime` | `SHOW GLOBAL STATUS`[^my-trace-ex-gs-query] | `mysql_global_status_uptime` |
| `MySQLPrimaryReadOnly` | MySQL[^my-trace-my-readonly]、MariaDB[^my-trace-mdb-readonly] | `read_only` | `SHOW GLOBAL VARIABLES`[^my-trace-ex-gv-query] | `mysql_global_variables_read_only` |

## 資源與飽和 {#saturation}

<a id="usage"></a>

| 要看什麼 | 資料庫的來源 | exporter metric（collector） | 預設 | 權限 | 證據 |
|---|---|---|---|---|---|
| 連線數 | `Threads_connected`、`max_connections` | `mysql_global_status_threads_connected`、`mysql_global_variables_max_connections` | 開 | — | 實測[^exp-mysql-ha] |
| 被拒的連線 | `Connection_errors_max_connections` | `mysql_global_status_connection_errors_total{error="max_connections"}` | 開 | — | 實測[^exp-mysql-ha][^mariadb-conn-errors] |
| 每帳號連線數 | `sys.x$user_summary.current_connections`；上限在 `mysql.user.max_user_connections` | `mysql_sys_current_connections{user}`（sys.user_summary）、`mysql_mysql_max_user_connections{mysql_user}`（mysql.user） | 關 | SELECT on sys、mysql.user；MariaDB 要開 performance_schema | 實測[^exp-mysql-ha][^my-sys-user-summary][^my-mysql-user-maxconn] |
| 執行中的 thread | `Threads_running` | `mysql_global_status_threads_running` | 開 | — | 讀碼[^my-trace-my-threads-running]；實測：metadata lock 情境中升高[^exp-mysql-perf] |
| Row lock 等待 | `Innodb_row_lock_current_waits`、`Innodb_row_lock_time` | `mysql_global_status_innodb_row_lock_current_waits` 等 | 開 | — | 實測[^exp-mysql-ha] |
| Metadata lock 等待 | `information_schema.PROCESSLIST.state` | `mysql_info_schema_processlist_threads{state="waiting_for_table_metadata_lock"}`（info_schema.processlist） | 關 | PROCESS | 實測[^exp-mysql-ha][^my-processlist-query] |
| Table open cache | `Table_open_cache_overflows`、`_misses`、`Opened_tables` | `mysql_global_status_table_open_cache_overflows` 等 | 開 | — | 讀碼[^my-asp-my-toc-overflows][^my-asp-mdb-toc-overflows] |
| Buffer pool 與 redo | `Innodb_buffer_pool_reads`、`_wait_free`、`Innodb_log_waits` | `mysql_global_status_innodb_buffer_pool_reads`、`..._wait_free`、`..._innodb_log_waits` | 開 | — | 實測[^exp-mysql-perf]、讀碼[^my-global-status-re] |
| 暫存表落到磁碟 | `Created_tmp_disk_tables`、`Created_tmp_tables` | `mysql_global_status_created_tmp_disk_tables` 等 | 開 | — | 實測[^exp-mysql-perf] |

- **max_connections 有一條保留給管理帳號的連線。**MySQL 允許 max_connections + 1 條，最後一條只給有 CONNECTION_ADMIN（或 SUPER）的帳號。[^mysql-conn-reserved]實驗中把 CONNECTION_ADMIN 給 exporter 後 `mysql_up` 回到 1，但 exporter 預設開 2 條連線，第二條仍被拒，MySQL 上 global_variables 等 7 個 collector 失敗，`max_connections` 跟著消失，比例規則還是空。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha]要讓 exporter 在打滿時仍看得到，可以考慮 MySQL 8.4 的獨立管理埠（`admin_address`）或 MariaDB 的 `extra_port`／`extra_max_connections`，未實測。
- **被拒的計數要事後才看得到。**打滿當下 exporter 自己也進不來；連線降下後 `connection_errors_total{error="max_connections"}` 才更新，用 `increase(...[10m]) > 0` 寫規則。MariaDB 被拒時同時加 `Aborted_connects`。[^mariadb-conn-errors]高峰本身留在 `Max_used_connections`，見[容量與成長](#capacity)。
- **超過 MAX_USER_CONNECTIONS 不算 Connection_errors。**實驗中第三條連線回 ERROR 1226，只有 `Aborted_connects` +1。要預警得用 sys.user_summary 的目前連線數除以 mysql.user 的上限；兩個 collector 的帳號 label 一個叫 `user`、一個叫 `mysql_user`，要 `label_replace`。exporter 自己用掉 3 條上限中的 2 條（0.67）。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha]

| 規則 | DB 原始碼 | 表／變數 | exporter 原始碼與 SQL | metric |
|---|---|---|---|---|
| `MySQLTooManyConnections` | `max_connections`：MySQL[^my-asp-my-max-connections]、MariaDB[^my-asp-mdb-max-connections] | `Threads_connected`、`max_connections` | `SHOW GLOBAL STATUS`、`SHOW GLOBAL VARIABLES`[^my-trace-ex-gs-query][^my-trace-ex-gv-query] | `..._threads_connected`、`..._max_connections` |
| `MySQLHighThreadsRunning` | MySQL[^my-trace-my-threads-running]、MariaDB[^my-trace-mdb-threads-running] | `Threads_running` | 同上 | `mysql_global_status_threads_running` |
| `MySQLConnectionsRefused` | MySQL[^my-trace-my-connerr-var]、MariaDB[^my-trace-mdb-connerr-var] | `Connection_errors_max_connections` | `connection_errors_` 前綴轉成 label[^my-trace-ex-gs-connerr] | `mysql_global_status_connection_errors_total` |
| `MySQLUserConnectionLimit` | sys view 與 `mysql.user`（未逐行核對） | `sys.x$user_summary`、`mysql.user` | [^my-sys-user-summary][^my-mysql-user-maxconn] | `mysql_sys_current_connections`、`mysql_mysql_max_user_connections` |
| `MySQLRowLockWaits`、`MySQLRowLockWaitTime` | MySQL[^my-trace-my-rowlock-var]、MariaDB[^my-trace-mdb-rowlock-var] | `Innodb_row_lock_current_waits`、`Innodb_row_lock_time` | `SHOW GLOBAL STATUS`[^my-trace-ex-gs-query] | `mysql_global_status_innodb_row_lock_*` |
| `MySQLMetadataLockWaits` | state 字串：MySQL[^my-perf-my-mdl-state]、MariaDB[^my-perf-mdb-mdl-state] | `PROCESSLIST.STATE` | `GROUP BY user, host, command, state`[^my-processlist-query][^my-trace-ex-pl-sanitize] | `mysql_info_schema_processlist_threads` |
| `MySQLInnoDBLogWaits` | MySQL[^my-perf-my-log-waits]、MariaDB[^my-perf-mdb-log-waits] | `Innodb_log_waits` | `SHOW GLOBAL STATUS` | `mysql_global_status_innodb_log_waits` |
| `MySQLBufferPoolWaitFree` | MySQL[^my-asp-my-bp-wait-free]、MariaDB[^my-asp-mdb-bp-wait-free] | `Innodb_buffer_pool_wait_free` | 同上 | `mysql_global_status_innodb_buffer_pool_wait_free` |
| `MySQLBufferPoolDiskReads` | MySQL[^my-asp-my-bp-reads]、MariaDB[^my-asp-mdb-bp-reads] | `Innodb_buffer_pool_reads`、`_read_requests` | 同上 | `mysql_global_status_innodb_buffer_pool_reads` |
| `MySQLTmpDiskTables` | MySQL[^my-asp-my-tmp-disk]、MariaDB[^my-asp-mdb-tmp-disk] | `Created_tmp_disk_tables` | 同上 | `mysql_global_status_created_tmp_disk_tables` |
| `MySQLTableOpenCacheOverflows` | MySQL[^my-asp-my-toc-overflows]、MariaDB[^my-asp-mdb-toc-overflows] | `Table_open_cache_overflows` | 同上 | `mysql_global_status_table_open_cache_overflows` |

## 工作量與效能 {#workload}

這一節看整體的吞吐、錯誤與延遲。要再往下分析到「哪個 session、哪條 SQL、在等什麼」，見[查詢與 session 分析（ASH）](mariadb-ash.md)。

| 要看什麼 | 資料庫的來源 | exporter metric（collector） | 預設 | 權限 | 證據 |
|---|---|---|---|---|---|
| 吞吐量 | `Com_commit`、`Queries` | `mysql_global_status_commands_total{command="commit"}`、`mysql_global_status_queries` | 開 | — | 實測[^exp-mysql-perf][^my-perf-ex-commands] |
| 慢查詢 | `Slow_queries` | `mysql_global_status_slow_queries` | 開 | — | 實測：`SLEEP(30)` 也算[^my-trace-my-slow][^my-trace-mdb-slow] |
| 哪個 SQL 慢 | `events_statements_summary_by_digest` | `mysql_perf_schema_events_statements_*`（perf_schema.eventsstatements） | 關 | SELECT | 實測[^exp-mysql-perf][^my-perf-ex-stmt-query] |
| 語句錯誤 | 同一張表加總 | `mysql_perf_schema_events_statements_sum_errors`（perf_schema.eventsstatementssum） | 關 | SELECT | 實測[^my-perf-ex-stmtsum-query] |
| Deadlock | MariaDB：`Innodb_deadlocks`；兩者：`INNODB_METRICS.lock_deadlocks` | `mysql_global_status_innodb_deadlocks`；`..._lock_lock_deadlocks_total`（MySQL）／`..._lock_lock_deadlocks`（MariaDB） | MariaDB 開；MySQL 關 | PROCESS | 實測[^exp-mysql-ha][^mariadb-innodb-deadlocks] |
| 連線建立失敗 | `Aborted_connects` | `mysql_global_status_aborted_connects` | 開 | — | 實測[^exp-mysql-ha] |
| 此刻在做事的 session | `PROCESSLIST` 的 command、state | `mysql_info_schema_processlist_threads{command,state}`（info_schema.processlist） | 關 | PROCESS | 實測[^my-asp-exp-ash][^my-processlist-query] |

- **Slow_queries 不管 slow log 有沒有開都會計數**，門檻是 `long_query_time`（預設 10 秒）。要知道是哪個查詢，開 `perf_schema.eventsstatements` 或讀 slow log，見[找慢 SQL](#perf-slow-sql)。（[MySQL 手冊](https://dev.mysql.com/doc/refman/8.4/en/server-status-variables.html)）
- **吞吐量掉下來本身不說原因**，先動的通常是 `threads_running`，見[效能與故障情境](#performance)。
- **processlist collector 是一個以 scrape 間隔取樣的簡化 ASH**：只有 command 與 state 兩個維度，等 row lock 的 session 是 `updating`、看不出在等鎖。MariaDB 缺 digest 的 quantile 與 CPU 時間，執行中的語句也沒有 digest；每秒取樣的取樣器原型與它怎麼接到這些訊號，見 [MariaDB 的 ASH 缺口](mariadb-ash.md)。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp-ash]

| 規則 | DB 原始碼 | 表／變數 | exporter 原始碼與 SQL | metric |
|---|---|---|---|---|
| `MySQLThroughputDrop` | `Com_*` 由命令計數陣列產生（未逐行核對） | `Com_commit` | `com_` 前綴轉成 `commands_total{command}`[^my-perf-ex-commands] | `mysql_global_status_commands_total` |
| `MySQLSlowQueries` | MySQL[^my-trace-my-slow]、MariaDB[^my-trace-mdb-slow] | `Slow_queries` | `SHOW GLOBAL STATUS`[^my-trace-ex-gs-query] | `mysql_global_status_slow_queries` |
| `MySQLSlowDigest` | MySQL 摘要表[^my-asp-my-esms-quantile]、MariaDB 摘要表[^my-asp-mdb-esms-locktime] | `events_statements_summary_by_digest` | 依 `SUM_TIMER_WAIT` 取前 250 個[^my-perf-ex-stmt-query][^my-perf-ex-stmt-topn] | `mysql_perf_schema_events_statements_seconds_total`、`..._total` |
| `MySQLStatementErrors` | 同上 | 同上（`SUM_ERRORS` 加總） | `SUM(SUM_ERRORS)`[^my-perf-ex-stmtsum-query][^my-perf-ex-stmtsum-errors] | `mysql_perf_schema_events_statements_sum_errors` |
| `MySQLDeadlocks` | MariaDB[^my-trace-mdb-deadlock-inc]、MySQL `lock_deadlocks`[^my-trace-my-deadlock-mon] | `Innodb_deadlocks`、`INNODB_METRICS` | global status；innodb_metrics 只取 enabled 的計數器[^my-trace-ex-im-colquery][^my-innodb-metrics-name] | `mysql_global_status_innodb_deadlocks`、`..._lock_lock_deadlocks(_total)` |
| `MySQLAbortedConnects` | MySQL[^my-asp-my-aborted-connects]、MariaDB[^my-asp-mdb-aborted-connects] | `Aborted_connects` | `SHOW GLOBAL STATUS`[^my-trace-ex-gs-query] | `mysql_global_status_aborted_connects` |

## 資料維護與完整性 {#maintenance}

| 要看什麼 | 資料庫的來源 | exporter metric（collector） | 預設 | 權限 | 證據 |
|---|---|---|---|---|---|
| 長交易 | `information_schema.INNODB_TRX` | `mysql_info_schema_innodb_trx_oldest_transaction_age_seconds{state}`（info_schema.innodb_trx） | 關 | PROCESS | 實測[^exp-mysql-ha][^my-innodb-trx-query] |
| Undo history（purge 跟不上） | MariaDB：`Innodb_history_list_length`；MySQL：`INNODB_METRICS.trx_rseg_history_len` | MariaDB：`mysql_global_status_innodb_history_list_length`；MySQL：`mysql_info_schema_innodb_metrics_transaction_trx_rseg_history_len` | MariaDB 開；MySQL 關 | MySQL 要 PROCESS | 實測[^exp-mysql-ha] |
| 救援模式 | `innodb_force_recovery` | `mysql_global_variables_innodb_force_recovery` | 開 | — | 讀碼[^my-asp-my-force-recovery][^my-asp-mdb-force-recovery] |
| 表的未使用空間 | `information_schema.TABLES.DATA_FREE` | `mysql_info_schema_table_size{component="data_free"}`（info_schema.tables） | 關 | SELECT | 讀碼[^my-asp-ex-tables-query] |
| 頁面損毀 | 只在 error log | **沒有** | — | — | 讀碼[^my-asp-mdb-page-compress-err] |

- **長交易在沒開 collector 時沒有任何訊號。**`innodb_trx` 在沒有交易時也不輸出 series（不是 0），所以「沒開」和「沒有長交易」看起來一樣，要靠 `collector_success` 確認。實驗中 11–16 秒的交易與等鎖的交易分成 `state="RUNNING"`、`state="LOCK WAIT"` 兩條。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha]
- **Undo history 的下降速度兩邊不同。**consistent snapshot 擋住 purge 時兩邊都到四百多；交易結束後 MariaDB 降到 1，MySQL 一段時間內仍是 500。門檻要抓得比日常高很多，見 [Purge 被擋住](#perf-purge)。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha]MySQL 的 `Innodb_purge_trx_id_age` 只在 debug build 有，正式版看不到。[^my-asp-my-purge-age-debug]
- **`innodb_force_recovery` ≥ 4 時 InnoDB 是唯讀**，但 `read_only` 仍可能是 0；看 `innodb_force_recovery` 本身。（[MySQL 手冊](https://dev.mysql.com/doc/refman/8.4/en/forcing-innodb-recovery.html)）
- **損毀沒有 status 計數。**兩邊都沒有「讀到損毀頁」的計數；MariaDB 的 `Innodb_num_pages_page_compression_error` 只算 page compression。補法：log pipeline 對 error log 的 InnoDB 損毀訊息告警，加上定期 `CHECK TABLE`（`mariadb-check`／`mysqlcheck`）把結果寫成 textfile metric。tablespace 檔案遺失時 crash recovery 失敗、進入 2 秒一次的重啟迴圈，metric 上只看得到 `mysql_up` 0，見[磁碟滿](#perf-disk-full)。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]
- **`info_schema.tables` 是每張表一組 series**，表多時 cardinality 高，用 `--collect.info_schema.tables.databases` 限定 schema；MySQL 的值有統計快取，見[容量與成長](#capacity)。

| 規則 | DB 原始碼 | 表／變數 | exporter 原始碼與 SQL | metric |
|---|---|---|---|---|
| `MySQLLongTransaction` | MySQL[^my-trace-my-trx-started]、MariaDB[^my-trace-mdb-trx-started] | `INNODB_TRX.trx_started` | `MAX(NOW() - trx_started)` 依 state 分組[^my-innodb-trx-query][^my-trace-ex-trx-desc] | `mysql_info_schema_innodb_trx_oldest_transaction_age_seconds` |
| `MySQLInnoDBHistoryLength` | MariaDB[^my-trace-mdb-histlen-var][^my-trace-mdb-histlen-export]、MySQL[^my-trace-my-histlen-mon][^my-trace-my-histlen-val] | `Innodb_history_list_length`／`trx_rseg_history_len` | global status；innodb_metrics[^my-trace-ex-im-colquery] | `..._innodb_history_list_length`、`..._trx_rseg_history_len` |
| `MySQLInnoDBForceRecovery` | MySQL[^my-asp-my-force-recovery]、MariaDB[^my-asp-mdb-force-recovery] | `innodb_force_recovery` | `SHOW GLOBAL VARIABLES`[^my-trace-ex-gv-query] | `mysql_global_variables_innodb_force_recovery` |
| `MySQLTableFragmented` | MySQL 統計快取[^my-asp-my-is-stats-expiry] | `information_schema.TABLES.DATA_FREE` 等 | `SELECT ... ifnull(DATA_FREE, '0') ... WHERE TABLE_SCHEMA = ?`[^my-asp-ex-tables-query][^my-asp-ex-tables-datafree] | `mysql_info_schema_table_size{component}` |

## 資料保護 {#protection}

能不能還原到某個時間點，要三樣東西：**完整備份**、**從備份那一刻起連續的 binlog**、**有人驗證過備份能還原**。mysqld_exporter 只看得到第二樣的一部分。

| 要看什麼 | 資料庫的來源 | exporter metric（collector） | 預設 | 權限 | 證據 |
|---|---|---|---|---|---|
| binlog 有沒有開 | `log_bin` | `mysql_global_variables_log_bin` | 開 | — | 實測：MySQL 8.4 預設 1、MariaDB 預設 0[^my-asp-exp] |
| binlog 保留多久 | `binlog_expire_logs_seconds`（MariaDB 另有 `expire_logs_days`、`max_binlog_total_size`） | `mysql_global_variables_binlog_expire_logs_seconds` | 開 | — | 實測：MySQL 2592000、MariaDB 0[^my-asp-exp][^my-asp-my-binlog-expire][^my-asp-mdb-binlog-expire] |
| binlog 用量 | `SHOW BINARY LOGS` | `mysql_binlog_size_bytes`、`mysql_binlog_files`、`mysql_binlog_file_number`（binlog_size） | 關 | REPLICATION CLIENT | 實測[^my-asp-ex-binlog-query] |
| 斷電時的耐久性 | `sync_binlog`、`innodb_flush_log_at_trx_commit` | `mysql_global_variables_sync_binlog` 等 | 開 | — | 實測：MariaDB sync_binlog 預設 0、MySQL 1[^my-asp-exp] |
| GTID（PITR 時定位交易） | `gtid_mode`、`gtid_strict_mode` | 見[複寫與 HA](#replication) | 開 | — | 實測 |
| 最近一次成功的備份 | 不在資料庫裡 | **沒有** | — | — | 讀碼：沒有備份相關的 collector[^my-default-collectors] |

- **`log_bin=0` 時 binlog_size collector 不輸出任何 series**，`collector_success` 仍是 1；「沒開 binlog」要看 `mysql_global_variables_log_bin`。<span class="at-chip at-ev-tested">實測</span>[^my-asp-ex-binlog-skip][^my-asp-exp]
- **MariaDB 的 sync_binlog 預設是 0**（交給 OS 決定何時寫入磁碟），斷電時 binlog 可能少掉已 commit 的交易；`MySQLDurabilityRelaxed` 在預設的 MariaDB 上會響，這是刻意的。<span class="at-chip at-ev-tested">實測</span>[^my-asp-mdb-sync-binlog][^my-asp-my-sync-binlog]
- **備份要自己輸出 metric。**建議的模式：備份 job 成功後寫一個 textfile（node_exporter 的 textfile collector），或推到 Pushgateway；告警看「多久沒有成功」與「metric 整個不見」兩條。名稱與門檻是例子：<span class="at-chip at-ev-inferred">推論</span>

```text
# /var/lib/node_exporter/textfile/mysql_backup.prom，由備份 script 最後一步原子地寫入（先寫暫存檔再 mv）
mysql_backup_last_success_timestamp_seconds{cluster="c1",tool="mariabackup",kind="full"} 1794200000
mysql_backup_size_bytes{cluster="c1",tool="mariabackup",kind="full"} 5.2e+09
mysql_backup_duration_seconds{cluster="c1",tool="mariabackup",kind="full"} 840
```

- **binlog 開著不代表能還原。**binlog 和資料在同一顆磁碟上時一起壞；要確認 binlog 有被複製到別處（replica、`mysqlbinlog --read-from-remote-server --stop-never` 之類的串流）。exporter 看不到串流的進度。

| 規則 | DB 原始碼 | 表／變數 | exporter 原始碼與 SQL | metric |
|---|---|---|---|---|
| `MySQLBinlogDisabled` | MySQL[^my-asp-my-log-bin]、MariaDB[^my-asp-mdb-log-bin] | `log_bin` | `SHOW GLOBAL VARIABLES`（ON → 1）[^my-trace-ex-gv-query][^my-parse-status] | `mysql_global_variables_log_bin` |
| `MySQLBinlogRetentionShort` | MySQL 預設 30 天[^my-asp-my-binlog-expire]、MariaDB 預設 0[^my-asp-mdb-binlog-expire] | `binlog_expire_logs_seconds` | 同上 | `mysql_global_variables_binlog_expire_logs_seconds` |
| `MySQLDurabilityRelaxed` | `sync_binlog`：MySQL[^my-asp-my-sync-binlog]、MariaDB[^my-asp-mdb-sync-binlog]；`innodb_flush_log_at_trx_commit`[^my-asp-my-flush-log-trx] | 兩個系統變數 | 同上 | `mysql_global_variables_sync_binlog`、`..._innodb_flush_log_at_trx_commit` |
| `MySQLBackupStale`、`MySQLBackupMetricMissing` | — | 備份 job 自己寫的 textfile | 不經過 mysqld_exporter | `mysql_backup_last_success_timestamp_seconds`（例子） |

## 容量與成長 {#capacity}

| 要看什麼 | 資料庫的來源 | exporter metric（collector） | 預設 | 權限 | 證據 |
|---|---|---|---|---|---|
| 磁碟 | 不在資料庫裡 | 沒有；用 node_exporter | — | — | 實測[^exp-mysql-perf] |
| binlog 有沒有照設定刪除 | `SHOW BINARY LOGS`、`slave_connections_needed_for_purge`（MariaDB） | `mysql_binlog_files`、`mysql_global_variables_slave_connections_needed_for_purge` | 關／開 | REPLICATION CLIENT | 實測[^my-asp-exp][^my-asp-mdb-purge-needs-slave] |
| AUTO_INCREMENT 用量 | `information_schema.TABLES.AUTO_INCREMENT`、`COLUMNS.COLUMN_TYPE` | `mysql_info_schema_auto_increment_column`、`..._column_max`（auto_increment.columns） | 關 | SELECT | 實測[^my-asp-exp][^my-asp-ex-autoinc-query] |
| 連線高水位 | `Max_used_connections` | `mysql_global_status_max_used_connections` | 開 | — | 實測[^exp-mysql-ha] |
| 資料量 | `information_schema.TABLES` 的 `DATA_LENGTH + INDEX_LENGTH` | `mysql_info_schema_schema_size_bytes{schema}`（info_schema.schema_size） | 關 | SELECT | 實測[^my-asp-exp][^my-asp-ex-schema-size-query] |

- **磁碟滿時資料庫不會停。**InnoDB 資料檔長不出去時立刻回 `ERROR 1114`，其他表照常寫入；手冊說的「卡在寫入、每分鐘檢查一次空間並寫 error log」是 MyISAM、binlog 這類寫入，見[磁碟滿](#perf-disk-full)。兩種情況 `mysql_up` 都仍是 1。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]（[MySQL 手冊：磁碟滿](https://dev.mysql.com/doc/refman/8.4/en/full-disk.html)）
- **MariaDB 沒有 replica 連著時 binlog 不會過期刪除。**`slave_connections_needed_for_purge` 預設 1。實驗：`binlog_expire_logs_seconds=20`，FLUSH 三次後 5 個檔，等 30 秒再 FLUSH 變成 6 個（沒刪）；設成 0 後 FLUSH 變 3 個。單機 MariaDB 開 binlog 做 PITR 時，binlog 會一直長到磁碟滿，設定看起來卻是對的。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp][^my-asp-mdb-purge-needs-slave]
- **MySQL 的 information_schema 統計值最多晚一天。**`AUTO_INCREMENT`、`DATA_LENGTH`、`DATA_FREE` 走統計快取（`information_schema_stats_expiry` 預設 86400 秒）。實驗中新表插入 10 列、再插入 50 列，兩次 scrape 都沒有這張表的 auto_increment；`ANALYZE TABLE` 之後才出現（74，`INSERT ... SELECT` 會先保留一段自動遞增值，所以比 `MAX(id)+1` 的 61 大）。MariaDB 沒有這個快取，插入 100 列後馬上是 128／127。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp][^my-asp-my-is-stats-expiry]
- **`Max_used_connections` 在 exporter 被擋在外面時也會記下來**：打滿後是 max_connections + 1。它從重啟（或 FLUSH STATUS）起算，適合當「最近曾經多接近上限」的訊號。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha]

| 規則 | DB 原始碼 | 表／變數 | exporter 原始碼與 SQL | metric |
|---|---|---|---|---|
| `MySQLDataDiskFilling`、`MySQLDataDiskFillingSoon` | — | 檔案系統 | node_exporter（job、mountpoint 是佔位符） | `node_filesystem_avail_bytes` |
| `MySQLAutoIncrementNearMax` | MySQL 統計快取[^my-asp-my-is-stats-expiry] | `TABLES.AUTO_INCREMENT`、`COLUMNS.DATA_TYPE` | `pow(2, case data_type ...)-1 as max_int`[^my-asp-ex-autoinc-query] | `mysql_info_schema_auto_increment_column(_max)` |
| `MySQLConnectionPeakNearMax` | MySQL[^my-trace-my-max-used][^my-trace-my-max-used-upd]、MariaDB[^my-trace-mdb-max-used][^my-trace-mdb-max-used-upd] | `Max_used_connections`、`max_connections` | `SHOW GLOBAL STATUS`、`SHOW GLOBAL VARIABLES` | `mysql_global_status_max_used_connections` |

## 效能與故障情境 {#performance}

吞吐量掉下來或資料庫停掉時，哪個指標先動、exporter 看不到什麼、要去哪裡查。依據是一組小實驗：MariaDB 11.4 與 MySQL 8.4 各一台（2 核、1 GB），每台一個限速工作負載（8 條連線、每秒最多 320 筆「點查 + 單列 UPDATE + COMMIT」），scrape 每 5 秒一次，依序做 11 個情境。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]

先講結論：

1. **吞吐量掉下來時，先動的幾乎都是 `threads_running`。**row lock、metadata lock、CPU 限制三種情境，`threads_running` 都在情境開始後 4–10 秒從 2–5 升到 10–13，commit 速率在同一個或之後幾個 scrape 掉下來。但 `MySQLHighThreadsRunning` 的門檻是 `max_connections` 的 60%（實驗是 60），連線池小的應用永遠碰不到。
2. **Metadata lock 會讓整張表停住，row lock 規則看不到。**一個讀了一列沒 commit 的交易（25 秒）加一個 `ALTER TABLE`，後面 9 個 session 全部排隊，commit 從 320/s 掉到 0，最長停頓 37 秒；`innodb_row_lock_current_waits` 是 0，長交易規則（> 300 秒）也不會響。只有 processlist collector 的 `state="waiting_for_table_metadata_lock"` 看得到。
3. **Row lock 等待數最多等於應用的連線數。**8 條連線全部卡住時 `innodb_row_lock_current_waits` 是 8，`MySQLRowLockWaits`（> 10）不會響；改看 `rate(innodb_row_lock_time) / 1000`（平均同時在等的 session 數，實驗 8–9.8）。
4. **找慢 SQL 靠 `perf_schema.eventsstatements`，但有三個坑。**只輸出總耗時前 250 個 digest；latency summary 的 `quantile` label 是 `"99.0"` 不是 `"0.99"`；MariaDB 的 quantile、lock_time、cpu_time 永遠是 0。MariaDB 還要先開 `performance_schema`（預設 OFF，要重啟）。
5. **InnoDB 資料檔長不出去時是立刻報錯，不是卡住。**放在 24 MB tmpfs 的表寫滿時兩邊都立刻回 `ERROR 1114 The table is full`，其他表照常寫入，`mysql_up` 仍是 1；exporter 只看得到語句錯誤數。
6. **OOM 與 crash loop 在 metric 上的樣子不一樣。**單次 OOM kill 後兩邊 3–4 秒就重新接受連線，Prometheus 只抓到一次 `mysql_up` = 0，`MySQLNotReachable`（`for: 1m`）不會響，只有 uptime 歸零看得到。crash loop（每次啟動 2 秒就退出）則相反：uptime 一次都沒被抓到，只有 `mysql_up` 規則看得到。

| 情境 | 客戶端看到的 | 先動的指標（秒＝距情境開始） | exporter 看不到 | 怎麼查 | 規則 |
|---|---|---|---|---|---|
| [慢查詢](#perf-slow-sql) | 沒有變慢（限速負載） | `slow_queries`（12–17 秒）、每個 digest 的 `rows_examined` | slow log 原文、執行計畫、排不進前 250 的 digest | `sys.statement_analysis`、`EXPLAIN` | `MySQLSlowQueries`、`MySQLSlowDigest` |
| [Row lock](#perf-row-lock) | TPS 273 → 98／90，1205 錯誤，p99 4.8–9.8 秒 | `row_lock_current_waits`、`threads_running`（9 秒）→ commit（14 秒） | 誰擋住誰 | `sys.innodb_lock_waits`、`INNODB_TRX` | `MySQLRowLockWaitTime`、`MySQLStatementErrors`、`MySQLThroughputDrop` |
| [Metadata lock](#perf-mdl) | 全停 37 秒 | `mdl_waiting` 9（11 秒）、commit 0 | MariaDB 的 MDL 持有者 | MySQL：`sys.schema_table_lock_waits`；兩邊：`SHOW PROCESSLIST` 找最舊的交易 | `MySQLMetadataLockWaits` |
| [Purge 被擋](#perf-purge) | 沒有變慢（60 秒內） | history list 50 秒到 13,663／13,474 | — | `INNODB_TRX` 找最舊的 snapshot | `MySQLInnoDBHistoryLength`（50,000 在這個寫入量下要約 3.6 分鐘） |
| [CPU 限制](#perf-cpu) | TPS 273 → 155／112，p99 0.5／3.6 秒 | `threads_running`、scrape 耗時（4 秒）→ commit（4–9 秒） | CPU 用量（要 cAdvisor 或 node_exporter） | 容器或主機 CPU、`sys.statement_analysis` 的總耗時 | `MySQLThroughputDrop` |
| [Buffer pool 太小](#perf-buffer-pool) | TPS 約 −10%，p99 340–380 ms | `innodb_buffer_pool_reads` 0–18 → 1,800–2,500/s（1–6 秒） | — | `information_schema.INNODB_BUFFER_POOL_STATS` | `MySQLBufferPoolDiskReads` |
| [暫存表落到磁碟](#perf-tmp) | 沒有變慢 | `created_tmp_disk_tables` 0.2–0.5 → 6.5–9.5/s | 哪個查詢（要 eventsstatements 的 `tmp_disk_tables_total`） | `sys.statements_with_temp_tables` | `MySQLTmpDiskTables` |
| [Redo 太小](#perf-redo) | 沒有重現 | `innodb_log_waits` 一直是 0 | — | checkpoint age | `MySQLInnoDBLogWaits` |
| [連線風暴](#perf-storm) | 既有連線 TPS 273 → 116／108；新連線被拒 | `mysql_up` 變 0（10 秒），其他 metric 消失 | 打滿當下的一切 | 事後的 `connection_errors_total` | `MySQLNotReachable`、`MySQLConnectionsRefused` |
| [磁碟滿](#perf-disk-full) | 只有寫那張表的語句失敗（1114） | 語句錯誤數 | 磁碟用量 | error log、node_exporter | `MySQLDataDiskFilling`、`MySQLStatementErrors` |
| [OOM kill](#perf-oom) | 斷線 4–5 秒（2013、2003），5–8 秒後 TPS 回到 320 | `mysql_up` 一次 0，下一次 uptime 7–8 | OOM 原因 | 容器事件、kernel log | `MySQLRestarted`、`MySQLRestartedRepeatedly`、`MySQLNotReachable` |

TPS 是客戶端「成功交易數 ÷ 時間窗長度」，前面是 MariaDB、後面是 MySQL。這台機器的容器時鐘不穩，客戶端每隔一段時間會出現 4–7 秒的空白（基線也有），所以「停頓」只看明顯更長的。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]

### 找慢 SQL {#perf-slow-sql}

| 來源 | exporter | 看得到 | 看不到 |
|---|---|---|---|
| `Slow_queries`（超過 `long_query_time` 的次數） | 預設就有 | 有沒有慢查詢、多快在增加 | 是哪個查詢 |
| `events_statements_summary_by_digest` | `--collect.perf_schema.eventsstatements`（預設關） | 每個 digest 的次數、總耗時、rows_examined／sent、錯誤、暫存表、no_index_used；MySQL 另有 p95／p99／p999、lock_time、cpu_time | 參數值、執行計畫、排不進前 N 的 digest |
| 同一張表加總 | `--collect.perf_schema.eventsstatementssum`（預設關） | 全部 digest 的錯誤、暫存表、全表掃描總數（不受前 N 限制） | 是哪個 digest |
| slow log、`EXPLAIN` | 沒有 | 原始 SQL 與參數、執行計畫 | — |

- **collector 的查詢**：依 `SUM_TIMER_WAIT` 排序取前 `--collect.perf_schema.eventsstatements.limit`（預設 250）個，只取 `LAST_SEEN` 在 `timelimit`（預設 86400 秒）內的，`DIGEST_TEXT` 截在 `digest_text_limit`（預設 120）字。[^my-perf-ex-stmt-query][^my-perf-ex-stmt-topn][^my-perf-ex-stmt-lastseen][^my-perf-ex-stmt-digest-text][^my-perf-ex-stmt-defaults][^my-perf-ex-stmt-flags]
- **Cardinality**：每個 digest 一組 `schema`、`digest`、`digest_text` label，19 條 series（15 個 counter + summary 的 3 個 quantile、sum、count）。[^my-perf-ex-stmt-labels]實驗中送 300 種不同形狀的查詢後，兩邊都固定在 250 個 digest、4,771 條 series；collector 耗時 13–16 ms。規模要靠 `limit` 控制，另外 `digest_text` 會隨截斷長度變，同一個 digest 改長度後是新的 series。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]
- **quantile label 不是 0.99。**exporter 用 95、99、999 當 map key，label 值變成 `"95.0"`、`"99.0"`、`"999.0"`；照慣例寫 `quantile="0.99"` 回傳空。MariaDB 不走 MySQL ≥ 8.0.28 的查詢，quantile、`lock_time_seconds_total`、`cpu_time_seconds_total` 仍然輸出，但永遠是 0。<span class="at-chip at-ev-tested">實測</span>[^my-perf-ex-stmt-quantile][^my-perf-ex-stmt-8028][^my-perf-ex-stmt-locktime][^exp-mysql-perf]
- **用平均值寫規則**：`rate(..._seconds_total[5m]) / rate(..._total[5m])` 是每個 digest 的平均耗時，兩種資料庫都能用。實驗中 filesort 全表掃描的 digest 每秒檢查 27 萬列，是 `rows_examined` 的第一名；總耗時第一名是 `SLEEP` 與 `COMMIT`。
- **MariaDB 要先開 `performance_schema`。**MariaDB 預設 OFF、MySQL 預設 ON，都要重啟才能改。[^my-perf-mdb-pfs-default][^my-perf-my-pfs-default]沒開時 collector 仍回報成功，只是沒有 series。`performance_schema_digests_size` 是 MariaDB 5,000、MySQL 10,000。
- **Slow_queries 和 slow log 無關。**實驗把 `long_query_time` 設成 0.5，兩個 client 跑 filesort 與 `SLEEP(1)`，30 秒內各 +36／+38。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf][^my-trace-my-slow][^my-trace-mdb-slow]

在資料庫上查（MySQL 的 sys schema 預設就有；MariaDB 11.4 也有 sys，但要開 performance_schema 才有資料）：

```sql
-- 總耗時最高的語句；full_scan、rows_examined_avg 看是不是缺索引
SELECT query, exec_count, total_latency, max_latency, rows_examined_avg, full_scan
  FROM sys.statement_analysis ORDER BY total_latency DESC LIMIT 10;
SELECT * FROM sys.statements_with_full_table_scans LIMIT 10;
SELECT * FROM sys.statements_with_temp_tables LIMIT 10;
-- 錯誤碼分布（exporter 沒有這個）
SELECT ERROR_NUMBER, ERROR_NAME, SUM_ERROR_RAISED
  FROM performance_schema.events_errors_summary_global_by_error WHERE SUM_ERROR_RAISED > 0 ORDER BY 3 DESC;
```

實驗中 MySQL 的 `sys.statement_analysis` 把 `COMMIT` 的 `max_latency` 顯示成 213.50 d，是這台機器時鐘不穩造成的，不要拿單次最大值當依據。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]

### Row lock {#perf-row-lock}

一個交易 `UPDATE ... WHERE id <= 50000` 後不 commit（持有 25% 的列）。

- 9 秒內 8 條連線全部在等：`innodb_row_lock_current_waits` 8、`innodb_trx_transactions{state="LOCK WAIT"}` 8、`threads_running` 3 → 11；commit 從約 275/s 掉到 0.7／1.1/s。每 10 秒（`innodb_lock_wait_timeout`）有一批 1205 錯誤，客戶端共 24／16 次。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]
- 等 row lock 的 session 在 processlist 的 state 是 `updating`，`state=~".*lock.*"` 查不到。
- 語句錯誤數（`events_statements_sum_errors`）升到 2.1／0.9 次/秒；global status 沒有「lock wait timeout 次數」。
- 查擋住的人：MySQL 用 `sys.innodb_lock_waits`（讀 `performance_schema.data_lock_waits`）；MariaDB 用 `information_schema.INNODB_LOCK_WAITS` 搭配 `INNODB_TRX`（這兩個查詢未實測）。

### Metadata lock：DDL 卡在長交易後面 {#perf-mdl}

一個交易 `BEGIN; SELECT ... WHERE id=1;` 後不 commit，3 秒後 `ALTER TABLE ... ADD COLUMN`。

- ALTER 要排他的 metadata lock，排在交易後面；之後所有用到這張表的語句（連點查也是）排在 ALTER 後面。11 秒時兩邊都是 9 個 session 在 `waiting_for_table_metadata_lock`，commit 0、QPS 4–5（只剩 exporter 自己），`innodb_row_lock_current_waits` 0，最舊的交易只有 25–26 秒。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]
- ALTER 不會自己放棄：`lock_wait_timeout` 預設 MySQL 31,536,000 秒（一年）、MariaDB 86,400 秒（一天）。[^my-perf-my-lock-wait-timeout][^my-perf-mdb-lock-wait-timeout]exporter 自己的連線設成 2 秒，所以 exporter 不會被卡住。[^my-trace-ex-lockwait]
- processlist 的 state 字串兩邊相同（"Waiting for table metadata lock"），exporter 轉成小寫底線。[^my-perf-my-mdl-state][^my-perf-mdb-mdl-state][^my-trace-ex-pl-sanitize]
- `perf_schema.eventswaits` 的 `wait/lock/metadata/sql/mdl` 不適合即時告警：等待結束才累計，MySQL 在交易結束時一次加 328 秒；MariaDB 預設沒開這個 instrument，一直是 0。[^my-perf-ex-waits-query]
- 誰擋住：MySQL 的 `performance_schema.metadata_locks` 看得到 9 個 PENDING（EXCLUSIVE、SHARED_READ），`sys.schema_table_lock_waits` 直接給出 `blocking_pid` 與 `KILL` 語句；MariaDB 的 `metadata_locks` 查不到（instrument 預設關），只能在 `SHOW PROCESSLIST` 找開最久的連線。

```sql
-- MySQL
SELECT waiting_query, blocking_pid, blocking_account, sql_kill_blocking_connection FROM sys.schema_table_lock_waits;
-- 兩邊都可用：最舊的交易
SELECT trx_mysql_thread_id, trx_started, trx_state FROM information_schema.INNODB_TRX ORDER BY trx_started LIMIT 5;
```

### Purge 被擋住 {#perf-purge}

`START TRANSACTION WITH CONSISTENT SNAPSHOT` 開著約 60 秒，同時每秒約 250–320 筆 UPDATE。History list 在 50 秒時到 13,663（MariaDB）／13,474（MySQL），交易結束後 40 秒內回到 152／156，吞吐量沒有變化。`MySQLInnoDBHistoryLength` 的門檻 50,000 在這個寫入量下約要 3.6 分鐘才會碰到；長交易本身用 `MySQLLongTransaction` 抓得比較早。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]

### CPU 限制 {#perf-cpu}

`docker update --cpus 0.1`（原本 2 核）45 秒。

- `threads_running` 升到 10／11、scrape 耗時從 0.03 秒升到 0.97／0.56 秒（4 秒內）；commit 從 320/s 掉到 73／45/s，客戶端 p99 0.5／3.6 秒，沒有錯誤。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]
- exporter 看不到 CPU 用量或 throttling；scrape 耗時變長是一個間接訊號，再嚴重就會碰到 `MySQLScrapeNearTimeout`。

### Buffer pool 太小 {#perf-buffer-pool}

執行中把 `innodb_buffer_pool_size` 從 128M 縮到 16M（表約 44 MB／102 MB）。

- `innodb_buffer_pool_reads`（要到磁碟讀的頁）從 0–18/s 升到 1,946／1,437/s，佔讀取請求 35%／20%；客戶端 TPS 約降 10%（273 → 245／243），p99 383／340 ms。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]
- `innodb_buffer_pool_wait_free` 只有 MySQL 動（約 4/s），MariaDB 一直是 0，所以 `MySQLBufferPoolWaitFree` 抓不到 MariaDB 的這種情況；用命中率（`MySQLBufferPoolDiskReads`）。
- 這組數據的磁碟是本機 SSD；磁碟慢時 TPS 掉得更多。

### 暫存表落到磁碟 {#perf-tmp}

兩個 client 用 `tmp_table_size=64K` 反覆 GROUP BY。`created_tmp_disk_tables` 升到 6.5／9.5/s，佔所有暫存表 44%／84%；eventsstatements 的 `tmp_disk_tables_total` 能指出是哪個 digest。MySQL 8.4 的 TempTable 引擎超過上限改用 InnoDB 磁碟表時也算在 `Created_tmp_disk_tables` 裡。限速負載下 TPS 沒有變化。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]

### Redo log 太小 {#perf-redo}

對 6 萬列的表連續三次全表 UPDATE，先用預設 redo（MariaDB 96M、MySQL 100M），再把 redo 縮到最小（MariaDB `innodb_log_file_size=16M`、MySQL `innodb_redo_log_capacity=8M`）。

- 兩次 `innodb_log_waits` 都是 0：它量的是 log buffer（MariaDB 16M、MySQL 64M）不夠，不是 redo 檔案太小。[^my-perf-my-log-waits][^my-perf-mdb-log-waits]
- checkpoint age（MariaDB `Innodb_checkpoint_age`；MySQL `Innodb_redo_log_current_lsn − Innodb_redo_log_checkpoint_lsn`）在預設大小時到 77 MB／66 MB，縮小後最高 2 MB／6 MB，表示 InnoDB 一直在刷頁。[^my-perf-mdb-checkpoint-age][^my-perf-my-redo-lsn]
- **沒有重現吞吐量下降**：第二輪的大量 UPDATE 可能排在第一輪後面等鎖，兩輪的寫入量不一定相同。這個情境只當作「哪些指標看得到 checkpoint 壓力」的參考。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]

### 連線風暴 {#perf-storm}

150 條連線反覆「連線 → 全表掃描 → 斷線」30 秒（`max_connections=100`）。

- 10 秒內兩邊 `mysql_up` 都變 0（exporter 也被擋），其他 mysql_* 消失 25–30 秒；exporter 偶爾擠進去時 `threads_connected` 是 65／89、`threads_running` 66／88。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]
- 原本就連著的工作負載沒有收到錯誤，但全表掃描搶 CPU，TPS 從 273 掉到 116／108。
- 風暴結束後：`connection_errors_total{error="max_connections"}` 一分鐘內 +1,840／+4,040，`aborted_connects` +2,169／+298（MariaDB 把被拒的連線也算進 Aborted_connects，MySQL 沒有），`max_used_connections` 101（max_connections + 1）。這和[資源與飽和](#saturation)一節的結論一致。

### 磁碟滿 {#perf-disk-full}

一張 `DATA DIRECTORY` 指到 24 MB tmpfs 的表（MySQL 要在 `innodb_directories` 裡），每次插入 10 列 × 4 KB。

- 2.4／3.2 秒、3,160 列後兩邊都立刻回 `ERROR 1114 The table 'fill' is full`，不等待；之後同一張表塞一列小資料、其他表的 UPDATE 都成功。MySQL error log 記 `posix_fallocate() ... Operating system error number 28`，MariaDB 只記 `The table 'fill' is full`。`mysql_up` 1、TPS 不變。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]
- 所以「磁碟滿時卡住、每分鐘重試」不適用於 InnoDB 資料檔變大的情況；手冊說的等待是 MyISAM、binlog 等寫入。redo、binlog、資料目錄本身滿的情況沒有測。
- **tablespace 檔案不見時 crash recovery 會失敗。**這張表的 tmpfs 在容器重啟後是空的；之後 MariaDB 被 kill 再啟動時，crash recovery 找不到 `/fill/app/fill.ibd`，`Cannot apply log ... corrupted file` → `Plugin initialization aborted` → `Aborting`，每次啟動 2 秒就退出，進入重啟迴圈。這段期間 `mysql_up` 一直是 0，uptime 從來沒被抓到。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]

### OOM kill {#perf-oom}

把容器記憶體上限降到「目前用量 + 150 MB」（MariaDB 486m、MySQL 730m），再把 `innodb_buffer_pool_size` 加到 1G。

- 加大 buffer pool 的那一刻就觸發 OOM kill（docker `oom` 事件、exit 137），還沒開始寫大表；`restart: on-failure` 立刻重啟。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]
- Crash recovery 很快：MariaDB 從啟動到 `ready for connections` 3 秒（`To recover: 7913 pages`），MySQL 3 秒（XA crash recovery）。客戶端斷線 4–5 秒（先 2013 Lost connection，再 2003 Can't connect），MariaDB 5 秒、MySQL 8 秒後回到 320 tx/s（MySQL 頭 3 秒只有 26–89 tx/s）。
- Prometheus 每 5 秒抓一次，只抓到**一次** `mysql_up` = 0，下一次 uptime 已經是 7–8 秒。所以 `MySQLNotReachable`（`for: 1m`）不會響，能看到的是 `MySQLRestarted`（uptime < 300）與 `resets(mysql_global_status_uptime[...])`（257 → 8）。`MySQLUpFlapping` 也響了。
- OOM 的原因不在任何 mysql_* metric 裡；要看容器的 `oom` 事件、cAdvisor 的 `container_oom_events_total` 或 kernel log。
- **Crash loop 看起來不一樣。**第一次執行時（上一節的 tablespace 遺失，與一次上限設得比 MySQL 啟動所需還低的 OOM），容器每 2 秒就退出、反覆重啟，uptime 一次都沒被 scrape 到，`MySQLRestarted` 與 `resets()` 都沒有資料；只剩 `MySQLNotReachable`、`MySQLCollectorsMissing` 響。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]
- 規則：單次重啟用 `MySQLRestarted`；`MySQLRestartedRepeatedly`（一小時內 `resets(uptime) > 1`）抓「每次都起得來但一直被 kill」；起不來的 crash loop 交給 `MySQLNotReachable`。


## Exporter 覆蓋矩陣 {#coverage}

預設開的 collector：global_status、global_variables、slave_status、info_schema.innodb_cmp、info_schema.innodb_cmpmem、info_schema.query_response_time。[^my-default-collectors]

| 資料庫來源 | Collector | 代表 metric | 預設 | 權限 | MySQL 8.4 | MariaDB 11.4 |
|---|---|---|---|---|---|---|
| `SHOW GLOBAL STATUS` | global_status | `mysql_global_status_*` | 開 | — | ✓ | ✓（多了 history_list_length、deadlocks） |
| `SHOW GLOBAL VARIABLES` | global_variables | `mysql_global_variables_*`（字串值跳過） | 開 | — | ✓ | ✓（沒有 super_read_only） |
| `SHOW REPLICA STATUS` | slave_status | `mysql_slave_status_*` | 開 | REPLICATION CLIENT；MariaDB 加 SLAVE MONITOR | `replica_*` | `slave_*` |
| heartbeat 表 | heartbeat | `mysql_heartbeat_*` | 關 | SELECT | ✓ | ✓ |
| `INNODB_TRX` | info_schema.innodb_trx | `mysql_info_schema_innodb_trx_*` | 關 | PROCESS | ✓ | ✓ |
| `INNODB_METRICS`（只取 enabled 的計數器） | info_schema.innodb_metrics | `mysql_info_schema_innodb_metrics_*` | 關 | PROCESS | ✓（計數器有 `_total`） | ✓（部分沒有 `_total`） |
| `PROCESSLIST` | info_schema.processlist | `mysql_info_schema_processlist_threads{state}` | 關 | PROCESS（沒有時只看到自己） | ✓ | ✓ |
| `sys.x$user_summary` | sys.user_summary | `mysql_sys_current_connections{user}` 等 | 關 | SELECT on sys | ✓ | 回報成功但沒有資料（performance_schema 預設關） |
| `mysql.user` | mysql.user | `mysql_mysql_max_user_connections{mysql_user}` | 關 | SELECT on mysql.user | ✓ | ✓ |
| `SHOW BINARY LOGS` | binlog_size | `mysql_binlog_size_bytes`、`mysql_binlog_files` | 關 | REPLICATION CLIENT | ✓ | ✓ |
| `replication_applier_status_by_worker` | perf_schema.replication_applier_status_by_worker | `mysql_perf_schema_*` | 關 | SELECT | ✓ | **collector 失敗** |
| `events_statements_summary_by_digest` | perf_schema.eventsstatements | `mysql_perf_schema_events_statements_*` | 關 | SELECT | ✓（前 250 個 digest；quantile label 是 `"99.0"`）[^exp-mysql-perf] | 要開 performance_schema；quantile、lock_time 永遠 0 |
| 同上加總 | perf_schema.eventsstatementssum | `mysql_perf_schema_events_statements_sum_*` | 關 | SELECT | ✓ | 要開 performance_schema |
| `events_waits_summary_global_by_event_name` | perf_schema.eventswaits | `mysql_perf_schema_events_waits_*{event_name}` | 關 | SELECT | ✓（MDL 等待結束才累計） | MDL instrument 預設關，一直是 0 |
| `replication_group_members` | perf_schema.replication_group_members | `mysql_perf_schema_replication_group_member_info{member_state,...}` | 關 | SELECT | ✓（Group Replication）[^my-asp-exp] | 不適用 |
| `replication_group_member_stats`（本機） | perf_schema.replication_group_member_stats | `mysql_perf_schema_transactions_*` | 關 | SELECT | ✓[^my-asp-exp] | 不適用 |
| `wsrep_*`（server 與 Galera provider） | global_status | `mysql_global_status_wsrep_*`、`mysql_galera_status_info` | 開 | — | 不適用 | ✓（沒開 Galera 也有一部分）[^my-asp-exp] |
| `information_schema.TABLES`（每張表） | info_schema.tables | `mysql_info_schema_table_size{component}`、`..._table_rows` | 關 | SELECT | ✓（統計快取最多晚一天） | ✓ |
| 同上加總 | info_schema.schema_size | `mysql_info_schema_schema_size_bytes{schema}` | 關 | SELECT | ✓ | ✓[^my-asp-exp] |
| `TABLES.AUTO_INCREMENT` ⨝ `COLUMNS` | auto_increment.columns | `mysql_info_schema_auto_increment_column(_max)` | 關 | SELECT | ✓（統計快取）[^my-asp-exp] | ✓[^my-asp-exp] |
| 備份紀錄 | — | 沒有 | — | — | textfile | textfile |
| `gtid_executed`、`gtid_binlog_pos` | — | 沒有 | — | — | 盲點 | 盲點 |
| `Gtid_Slave_Pos`（replica 狀態） | slave_status | `mysql_slave_status_gtid_slave_pos{domain_id,server_id}` | 開 | SLAVE MONITOR | 不適用 | ✓[^my-trace-ex-slave-gtid] |
| 磁碟、記憶體、OOM | — | 沒有 | — | — | node_exporter | node_exporter |

權限的影響（收回 PROCESS、SLAVE MONITOR 時哪些 collector 失敗）見[元件卡](../components/mysqld_exporter.md#exporter-grants)與前一輪實驗。[^exp-mysql]

## 盲點與補法 {#blind-spots}

| 盲點 | 為什麼 | 補法 |
|---|---|---|
| 連線打滿時的連線數 | exporter 也被擋，整組 metric 消失 | 告警靠 `mysql_up` 與事後的 `connection_errors_total`；[自訂查詢走 admin port／extra_port](#custom-queries) |
| errant GTID | `gtid_executed` 是字串 | [自訂查詢](#custom-queries)輸出每個 UUID（MariaDB：domain × server_id）的數字，在 PromQL 比 replica 與 primary |
| 磁碟用量 | 不在資料庫裡 | node_exporter，規則要知道資料目錄的掛載點 |
| OOM 原因 | 只看得到 uptime 歸零 | kernel log、cAdvisor 的 OOM 事件 |
| 真正的寫入 | `mysql_up` 只做 ping | heartbeat 寫入程式自己回報成功時間，或應用層探測 |
| Replica 錯誤訊息 | `Last_SQL_Error` 是字串 | 規則帶 errno；MySQL 可用[自訂查詢](#custom-queries)加上 `ER_` 名稱；訊息本身看 `SHOW REPLICA STATUS` |
| MariaDB 的每帳號、每查詢統計 | performance_schema 預設關 | 開 `performance_schema=ON`（要重啟，佔記憶體） |
| Metadata lock 細節 | 沒有 collector 讀 `performance_schema.metadata_locks` | [自訂查詢](#custom-queries)輸出等待數、阻擋者數與持有時間；是誰看 `sys.schema_table_lock_waits` |
| 備份新鮮度 | 沒有備份 collector | 備份 job 寫 textfile metric（`mysql_backup_last_success_timestamp_seconds`，例子），見[資料保護](#protection) |
| 頁面損毀 | 沒有 status 計數，只在 error log | log pipeline 對 error log 告警；離峰排程 `CHECK TABLE` 寫 textfile（太重，不放進自訂查詢） |
| GR 節流時間 | `Gr_flow_control_*` 只有 Enterprise component 有 | 看 applier／certifier 佇列對門檻的比例 |
| MariaDB 執行中語句的 digest、長尾延遲 | digest 在語句結束才寫入；沒有 quantile | [ASH 取樣器](mariadb-ash.md#sampler) |

## 用自訂查詢補上 {#custom-queries}

「資料庫有、exporter 沒輸出」的四項，用 query-exporter 5.2.2（映像 `adonato/query-exporter:5.2.2`）跑自訂查詢補上；設定在 [`queries/query-exporter/mysql.yaml`](https://github.com/HahaSula/alert-trust/blob/main/queries/query-exporter/mysql.yaml)，規則在 `rules/targets/mysql.yml` 的 `mysql-custom-queries` 群組。Lab 直接掛載同一份設定。<span class="at-chip at-ev-tested">實測</span>[^my-q-exp]

| 盲點 | 結果 | 新 metric（都帶 `database`、`cluster`） | 規則 |
|---|---|---|---|
| 連線打滿當下的連線數 | 補上（要開 admin port／extra_port） | `mysql_q_threads_connected`、`mysql_q_max_connections` | `MySQLConnectionsExhausted` |
| errant GTID | 補上（在 PromQL 裡比 replica 與 primary） | `mysql_q_gtid_executed_transactions{source_uuid}`、`mysql_q_gtid_binlog_state_seq{domain_id,server_id}`、`mysql_q_read_only` | `MySQLErrantGtid`、`MariaDBErrantGtid` |
| Metadata lock 誰擋住誰 | 補上數字；「是誰」不放 label | `mysql_q_mdl_waiting_sessions`、`_oldest_wait_seconds`、`_blocking_sessions`、`_idle_blocking_sessions`、`_blocker_age_seconds` | `MySQLMetadataLockBlocker` |
| Replica 錯誤訊息 | MySQL 補上錯誤碼與名稱；訊息文字與 MariaDB 補不了 | `mysql_q_replica_last_error_timestamp_seconds{channel,thread,errno,error_name}` | `MySQLReplicaErrorCode` |
| MariaDB ASH 彙總（[取樣器](mariadb-ash.md#sampler)） | 輸出取樣器的 60 秒彙總 | `mysql_q_ash_active_sessions{wait_class,digest}`、`mysql_q_ash_sampler_enabled`、`mysql_q_ash_sampler_last_run_age_seconds` | `MariaDBAshSamplerStopped`、`MariaDBRowLockActiveSessions` |
| 頁面損毀 | 不做 | — | — |
| 真正的寫入 | 不做 | — | — |

不做的兩項：`CHECK TABLE` 要讀整張表並加鎖，不能放進每 15–60 秒的查詢；寫入探測要讓監控帳號有寫入權限，改用 pt-heartbeat ＋ `--collect.heartbeat`（見[延遲](#replication)）。GR 節流時間沒有資料來源，自訂查詢也補不了。

### 連線打滿時查詢怎麼進得去

一般帳號在打滿時一定被拒：MySQL 只放行 admin 連線或有 `SUPER`／`CONNECTION_ADMIN`／`SERVICE_CONNECTION_ADMIN` 的帳號[^my-q-my-admin-bypass][^mysql-conn-reserved]，MariaDB 只放行有 `CONNECTION ADMIN` 的帳號。[^my-q-mdb-ignore-maxconn][^my-q-mdb-conn-check]做法依序：

| 做法 | 條件 | 代價 |
|---|---|---|
| **MySQL admin port**（建議） | `--admin-address=<IP 或主機名稱>`（不接受 `0.0.0.0`）、`--admin-port`（預設 33062）；帳號要 `SERVICE_CONNECTION_ADMIN`[^my-q-my-admin-address][^my-q-my-admin-priv] | 要重啟才能開；這個權限只影響連線，不給資料權限 |
| **MariaDB extra_port**（建議） | `--extra-port`、`--extra-max-connections` 至少 2；extra_port 有自己的連線計數，不需要額外權限[^my-q-mdb-extra-port][^my-q-mdb-extra-scheduler][^my-q-mdb-extra-max-default] | 要重啟；這些連線不算進 `Threads_connected`（實驗中 mdb2 只有 extra_port 連線時是 0） |
| 只靠 query-exporter 的常駐連線 | connection-pool 預設 1 條常駐連線[^my-q-qe-pool-size][^my-q-qe-pool-persist]，打滿前已連上就能繼續查（連線數上限只在連線時檢查） | 斷線就要重連：資料庫重啟、查詢 timeout（query-exporter 會丟掉那條連線[^my-q-qe-timeout-drop]）、每 3600 秒 `pool_recycle`[^my-q-qe-pool-recycle]；打滿時重連會被拒（讀碼，未實測） |
| 給帳號 `CONNECTION_ADMIN` | — | 不要：它同時讓帳號在 `read_only` 下可寫、可以 KILL 別人的連線[^my-q-my-connadmin-readonly][^my-q-my-connadmin-kill] |

實驗：my1、mdb1 的 `max_connections=10`，各開 15 條 `SELECT SLEEP(75)`。<span class="at-chip at-ev-tested">實測</span>[^my-q-exp]

- mysqld_exporter（一般 port）兩台 `mysql_up` 都是 0，`threads_connected` 消失；qexporter 從一般 port 新開連線，MariaDB 收到 `ERROR 1040`（MySQL 那一刻 app 只連上 8 條，還有空位）。
- 同一時間 query-exporter 走 admin port／extra_port：`mysql_q_threads_connected` 兩台都是 10、比例 1，`MySQLConnectionsExhausted` 兩台都進入 pending；CLI 從 33062、3307 新開連線也成功。
- 第一輪用 MariaDB 預設 `extra_max_connections=1`：query-exporter 的常駐連線佔掉唯一的位置，CLI 從 3307 也被拒（`ERROR 1040`），所以建議至少 2。<span class="at-chip at-ev-tested">實測</span>[^my-q-exp-1]

### errant GTID

單一查詢只連一台，算不出「replica 的 `gtid_executed` 是不是 primary 的子集」（`GTID_SUBSET` 要同時拿到兩邊的字串）。做法是讓每台輸出可以比較的數字，在 PromQL 裡比：

- **MySQL**：用 `JSON_TABLE` 把 `@@global.gtid_executed`[^my-q-my-gtid-executed] 拆成每個 source UUID 的交易數（各區間長度加總）。replica 上的本機寫入用 replica 自己的 UUID，primary 沒有這個 UUID；replica 曾經是 primary 時，同一個 UUID 的交易數會比 primary 多。
- **MariaDB**：讀 `@@global.gtid_binlog_state`，它保留每個 (domain_id, server_id) 的最後一筆 GTID。[^my-q-mdb-binlog-state-pair][^my-q-mdb-gtid-binlog-state]不能用 `gtid_binlog_pos`：它每個 domain 只留最後一筆，下一筆複寫事件就把本機寫入蓋掉（實驗：`0-2-161` → `0-1-161`，state 仍有 `0-2-161`）。前提是每台都 `log_slave_updates=ON`。
- **規則**：read_only=1 的節點，某個 UUID（或 domain／server_id）的值在 2 分鐘前就大於 read_only=0 那台現在的值，或 primary 根本沒有這個 UUID → errant。用 2 分鐘前的值是為了避開 replica 比 primary 晚 scrape 而暫時看起來比較多；只在剛好一台可寫時評估。

實驗：在 read_only 的 my2、mdb2 上直接寫一筆。my2 多出 `source_uuid=<my2 的 UUID>` 1 筆（`GTID_SUBSET` 同時是 0），mdb2 多出 `server_id=2` 的 seq；`MySQLErrantGtid`、`MariaDBErrantGtid` 都進入 pending（`for: 5m`，lab 沒有等到 firing）。寫入之前（s0–s3，primary 上有寫入並複寫到 replica）兩條規則都沒有出現在 ALERTS。<span class="at-chip at-ev-tested">實測</span>[^my-q-exp]

Cardinality：label 數 = 曾經寫過這組資料的 server 數（MySQL 的 UUID、MariaDB 的 domain × server_id），重建或換機才會增加。

### Metadata lock 阻擋者

| | MySQL 8.4 | MariaDB 11.4 |
|---|---|---|
| 等待者 | `performance_schema.metadata_locks` 的 `LOCK_STATUS='PENDING'`[^my-q-my-mdl-status] | `PROCESSLIST.STATE='Waiting for table metadata lock'`[^my-perf-mdb-mdl-state]（performance_schema 預設關，也看不到 PENDING[^exp-mysql-perf]） |
| 阻擋者 | 同一張表上 `GRANTED`、自己沒有 PENDING 的 thread | `METADATA_LOCK_INFO`（要 `INSTALL SONAME 'metadata_lock_info'`，官方映像內附）只列已取得的鎖[^my-q-mdb-mli-granted]；阻擋者 = 和「正在等的 session 持有鎖的同一張表」上的其他持有者 |
| 持有多久 | 阻擋者 InnoDB 交易開啟秒數 | `LOCK_TIME_MS`[^my-q-mdb-mli-time] |

查詢都是沒有 `GROUP BY` 的彙總，一定回一列（沒有等待時是 0；NULL 會被當成 0[^my-q-qe-null-zero]），所以沒有 label、不會累積 series。誰（pid、帳號、SQL）不放 label：告警後查 `sys.schema_table_lock_waits` 或 `METADATA_LOCK_INFO`。MariaDB 的限制：排在 ALTER 後面、自己沒有持有任何鎖的 SELECT 只算進等待數，找不到阻擋者；只有等待者本身持有那張表的鎖（例如 ALTER 的 `MDL_SHARED_UPGRADABLE`）時才找得到。

實驗：閒置交易讀 `app.t` → `ALTER TABLE` → `SELECT`。兩邊都是等待 2、阻擋者 1、閒置的阻擋者 1、阻擋者持有約 30 秒；交易結束後全部回到 0。同一時間 mysqld_exporter 的 processlist collector 只看得到 state 的計數。`MySQLMetadataLockBlocker`（最久等待 > 30 秒、`for: 1m`）在 lab 的 75 秒裡沒有走到 pending（容器時鐘慢，取樣時 MySQL 的最久等待才 30 秒）。<span class="at-chip at-ev-tested">實測</span>[^my-q-exp]

### Replica 錯誤碼

MySQL 讀 `performance_schema.replication_applier_status_by_worker` 與 `replication_connection_status` 的 `LAST_ERROR_NUMBER`[^my-q-my-worker-errno][^my-q-my-conn-errno]，join `events_errors_summary_global_by_error` 拿到 `ER_` 名稱[^my-q-my-error-name]。`errno`、`error_name` 是有限集合，可以當 label；`LAST_ERROR_MESSAGE` 含表名與值，是自由文字，不放。值是錯誤時間（Unix 秒），只輸出有錯誤的列，設 60 秒 expiration：錯誤清掉後 series 會消失。[^my-q-qe-expiration][^my-q-qe-expire-on-scrape]

MariaDB 補不了：`replication_applier_status_by_worker` 的列數等於 `slave_parallel_threads`，預設 0 就沒有列，還要 `performance_schema=ON`[^my-q-mdb-worker-rows]；也沒有 `events_errors_summary_*`。繼續用 mysqld_exporter 的 `mysql_slave_status_last_sql_errno`（[複寫與 HA](#replication)）。

實驗：replica 先有 id=100，primary 再寫 id=100。my2 輸出 `errno="1062"`、`error_name="ER_DUP_ENTRY"`、`thread="applier"`，`MySQLReplicaErrorCode` 進入 pending（第一輪到 firing[^my-q-exp-1]）；修好並 `START REPLICA SQL_THREAD` 後 15 秒 series 仍在（expiration 60 秒），再 70 秒消失。mdb2 的 by_worker 表 0 列，`SHOW SLAVE STATUS` 的 `Last_SQL_Errno` 是 1062。<span class="at-chip at-ev-tested">實測</span>[^my-q-exp]

### MariaDB ASH

[ASH 頁](mariadb-ash.md#sampler)的取樣器在資料庫裡每秒取樣，query-exporter 只讀 `ash.aas_60s`（7 種等待類別 × 前 10 個 digest＋other，上限 77 條 series）和取樣 EVENT 的狀態（`information_schema.EVENTS` 只列有 `EVENT` 權限的 schema，所以要 `GRANT EVENT ON ash.*`）。digest 仍只有已結束的語句，quantile 與 CPU 時間仍然沒有，這個盲點只補上「依時間的等待分布」。

實驗：一個交易持有 row lock，3 個 UPDATE 等它。`mysql_q_ash_active_sessions{wait_class="lock_row"}` 是 1.75，和同一時間直接查 `ash.aas_60s` 相同；`ALTER EVENT ... DISABLE` 後 `mysql_q_ash_sampler_enabled` 0、`MariaDBAshSamplerStopped` 進入 pending。這組數據用「每次 scrape 執行」的設定（刪掉 interval）量到，原因見下一節。<span class="at-chip at-ev-tested">實測</span>[^my-q-exp-ash]

### query-exporter 本身的失效方式

| 行為 | 影響 | 對策 | 依據 |
|---|---|---|---|
| 查詢依 `interval` 由排程執行，和 scrape 無關[^my-q-qe-interval] | 失敗時 metric 不更新，沒有 expiration 會一直停在最後一個值 | 每個 metric 設 expiration；資料庫停機後約 90 秒 mdb2 的自訂 series 全部消失 | 讀碼＋實測[^my-q-qe-expiration][^my-q-exp] |
| 查詢回 0 列時不更新任何 metric，也不更新 `query_timestamp`[^my-q-qe-empty-rows][^my-q-qe-timestamp-empty] | 用 `query_timestamp` 判斷停滯會誤報（第一輪：正常沒有錯誤的 replica 查詢被判成停滯） | `MySQLCustomQueryStale` 改用 `queries_total{status="success"}` 的增加量 | 讀碼＋實測[^my-q-exp-1] |
| 欄位名稱或數量不符是 fatal，查詢被永久移除[^my-q-qe-fatal][^my-q-qe-doomed] | 之後只剩不再增加的成功計數 | 同上，5 分鐘沒有成功就告警 | 讀碼 |
| 連不上資料庫只加 `database_errors_total`，沒有 query label[^my-q-qe-connect-error] | — | `MySQLCustomQueryFailing` 同時看兩個 counter（輸出時 counter 加 `_total`） | 讀碼＋實測：mdb2 停機後 `database_errors_total` 2、`MySQLCustomQueryFailing` pending[^my-q-exp] |
| APScheduler 3.11 的 `misfire_grace_time` 預設 1 秒，晚到超過 1 秒的那次直接跳過[^my-q-aps-grace][^my-q-aps-missed] | 這台機器（容器時鐘慢）上 30 秒的查詢約 3 分鐘只成功 2 次、跳過 3 次，ASH metric 一直停在開頭的值 | expiration 至少 4 倍 interval；時鐘不穩的環境改成每次 scrape 執行（刪掉 interval；成本跟著 scrape 次數，多台 Prometheus 會加倍） | 實測[^my-q-exp-ash-interval][^my-q-exp-ash] |
| 每個 metric 自動加 `database` label（項目名稱）[^my-q-qe-db-label]；`query_interval` 不帶[^my-q-qe-interval-labels] | 規則要用 `(cluster, database)` 對齊，不能用 `instance`（那是 query-exporter 的位址） | 設定裡用 database `labels` 標 `cluster` | 讀碼 |

### 權限、成本與 cardinality

```sql
-- MySQL 8.4
CREATE USER 'qexporter'@'%' IDENTIFIED BY '...' WITH MAX_USER_CONNECTIONS 2;
GRANT PROCESS ON *.* TO 'qexporter'@'%';
GRANT SELECT ON performance_schema.* TO 'qexporter'@'%';
GRANT SERVICE_CONNECTION_ADMIN ON *.* TO 'qexporter'@'%';   -- 只在走 admin port 時
-- MariaDB 11.4
CREATE USER 'qexporter'@'%' IDENTIFIED BY '...' WITH MAX_USER_CONNECTIONS 2;
GRANT PROCESS ON *.* TO 'qexporter'@'%';
INSTALL SONAME 'metadata_lock_info';
GRANT SELECT, EVENT ON ash.* TO 'qexporter'@'%';               -- 只在裝了 ASH 取樣器時
```

| 查詢 | interval | 成本 | series 數（每台） |
|---|---|---|---|
| 連線與角色 | 15s | 讀一個 status 變數 | 3 |
| errant GTID | 30s | 拆一個字串，和 UUID 或 domain×server 數成正比 | 寫過的 server 數 |
| metadata lock | 15s | 掃 metadata lock 清單（和持有中的 MDL 數成正比） | 5 |
| replica 錯誤碼 | 15s | 讀 by_worker（列數 = applier worker 數） | 0（有錯誤時每個 channel × thread 1 條） |
| ASH | 30s | 讀最近 60 秒的取樣表（取樣本身的成本見 ASH 頁） | 最多 77 ＋ 2 |

每個查詢 timeout 5 秒，連線時另外設 `lock_wait_timeout=2` 與語句上限（MySQL `max_execution_time=4000`、MariaDB `max_statement_time=4`）。實驗中 4 台合計 39 條自訂 series。<span class="at-chip at-ev-tested">實測</span>[^my-q-exp]

## 建議的 exporter 設定 {#recommended}

```text
--collect.info_schema.innodb_trx          # 長交易
--collect.info_schema.innodb_metrics      # MySQL 的 undo history、deadlock
--collect.info_schema.processlist         # metadata lock 等待（--collect.info_schema.processlist.min_time 預設 0）
--collect.heartbeat                       # 搭配 pt-heartbeat；--collect.heartbeat.database/table
--collect.binlog_size                     # binlog 用量
--collect.sys.user_summary                # 每帳號連線數（MySQL；MariaDB 要先開 performance_schema）
--collect.mysql.user                      # 每帳號的 MAX_USER_CONNECTIONS
--collect.perf_schema.replication_group_members        # Group Replication 成員狀態（只在 GR 節點）
--collect.perf_schema.replication_group_member_stats   # Group Replication 佇列（只輸出本機）
--collect.auto_increment.columns          # AUTO_INCREMENT 用量（每個欄位一組 series）
--collect.info_schema.schema_size         # 每個 schema 的資料量
```

帳號：

```sql
-- MySQL 8.4
CREATE USER 'exporter'@'%' IDENTIFIED BY '...' WITH MAX_USER_CONNECTIONS 3;
GRANT PROCESS, REPLICATION CLIENT, SELECT ON *.* TO 'exporter'@'%';
-- MariaDB 10.5.9 起另外要
GRANT SLAVE MONITOR ON *.* TO 'exporter'@'%';
```

- `SELECT ON *.*` 可以縮成 heartbeat 表、`sys`、`mysql.user`、`performance_schema` 幾個 schema。
- MAX_USER_CONNECTIONS 3 時 exporter 一次 scrape 用掉 2 條；兩台 Prometheus 同時抓就會超過，見[元件卡](../components/mysqld_exporter.md#db-up-flapping)。
- 是否給 CONNECTION_ADMIN 要取捨：它讓 exporter 在打滿時還有一條連線，但只有一條，而且和 DBA 搶同一個保留位子。

## 規則 {#rules}

規則在 [`rules/targets/mysql.yml`](https://github.com/HahaSula/alert-trust/blob/main/rules/targets/mysql.yml)，分成 liveness、budget、replication、ha、cluster（Galera 與 Group Replication）、maintenance、protection、capacity、stability、usage、performance 幾組；每條規則的 `runbook_url` 指回[元件卡](../components/mysqld_exporter.md)上對應的訊號，每個面向的證據鏈在上面各節。佔位符：`job="mysql"`、`cluster`、`role`、`pitr`、node_exporter 的 `job="node"` 與 `mountpoint`、備份 metric 的名稱。

## 實驗 {#lab}

[`lab/targets/mysql/ha/`](https://github.com/HahaSula/alert-trust/tree/main/lab/targets/mysql/ha)：`./run.sh` 起 4 台資料庫、4 台 exporter、1 台 Prometheus（只開 127.0.0.1:22090），跑完 9 個情境後 `docker compose down -v`，約 13 分鐘。原始結果與摘要在 `results/run-20261108.md`，各節點的 metric 名稱清單在 `results/names-*.txt`。[^exp-mysql-ha]

[`lab/targets/mysql/perf/`](https://github.com/HahaSula/alert-trust/tree/main/lab/targets/mysql/perf)：`./run.sh` 跑 11 個效能與故障情境（約 21 分鐘），`./run.sh focus` 只重做 metadata lock 與 OOM（約 7 分鐘）；只開 127.0.0.1:22690，結束後 `docker compose down -v`。[^exp-mysql-perf]

[`lab/targets/mysql/custom-queries/`](https://github.com/HahaSula/alert-trust/tree/main/lab/targets/mysql/custom-queries)：`./run.sh` 起 4 台資料庫、query-exporter 5.2.2、2 台 mysqld_exporter 與 Prometheus（只開 127.0.0.1:22960、22990），做連線打滿、metadata lock、replica 錯誤碼、errant GTID、ASH、資料庫停機 6 個情境，約 14 分鐘；`./run.sh ash` 只重做 ASH。[^my-q-exp]

[`lab/targets/mysql/ash/`](https://github.com/HahaSula/alert-trust/tree/main/lab/targets/mysql/ash)：`./run.sh` 起三台 Group Replication、三台 Galera、一台 MariaDB 與四台 exporter（只開 127.0.0.1:22801–22804），做 quorum、binlog、auto_increment 與 ASH 取樣器的情境，約 6 分鐘；`./run.sh ash` 只重做 ASH（約 1 分鐘）。結果在 `results/`。[^my-asp-exp][^my-asp-exp-ash]
