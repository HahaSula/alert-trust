# MariaDB 各版本的監控差異

這頁列出 MariaDB 10.6、10.11、11.4、11.8、12.3 五條 LTS 線之間會影響監控的差異。lab 與原始碼參考是 MariaDB 11.4.13；這頁補上其他版本線。[MariaDB 卡](../../components/mariadb.md)的 `versions_checked` 與版本相關的訊號、規則依這頁寫；本體怎麼監控見 [MariaDB 頁](../mariadb.md)。

依據是各版本線最新 tag 的原始碼（`mariadb-10.6.28`、`mariadb-10.11.19`、`mariadb-11.4.13`、`mariadb-11.8.9`、`mariadb-12.3.3`，2026-11-10 從 [tag 列表](https://github.com/MariaDB/server/tags)選）、mysqld_exporter v0.21.0 的原始碼，以及 [MariaDB downloads REST API](https://downloads.mariadb.org/rest-api/mariadb/) 的版本狀態。沒有做實驗，全部是<span class="at-chip at-ev-code">讀碼</span>；標「推論」的是從原始碼推出、沒有單獨驗證的結果。

## 結論 {#summary}

1. **replication metric 的名稱在五條線都一樣。**MariaDB 的 `REPLICA` 只是 `SLAVE` 的同義字，`SHOW REPLICA STATUS` 回的仍是 `Slave_IO_Running`、`Seconds_Behind_Master`；exporter 先跑 `SHOW ALL SLAVES STATUS`，輸出 `mysql_slave_status_slave_*`。這點和 MySQL 不同，MySQL 8.4 起名稱會換。
2. **11.0 起 `SUPER` 不再包含 `SLAVE MONITOR`、`BINLOG MONITOR`。**在 11.x 新建、只給 `SUPER` 的監控帳號跑不了 `SHOW SLAVE STATUS` 與 `SHOW BINARY LOGS`；從舊版升上來的帳號會自動補上。`REPLICATION CLIENT` 在 MariaDB 只等於 `BINLOG MONITOR`，照 exporter README 給權限的話，replica 上的 `slave_status` collector 會失敗。
3. **11.8 起 replica 狀態多三個欄位**：`Master_last_event_time`、`Slave_last_event_time`、`Master_Slave_time_diff`，exporter 會自動輸出成 metric，可以補 `replication-lag`。
4. **`performance_schema` 在五條線都預設 OFF。**依賴它的訊號（`slow-sql-visibility`、`statement-errors`、`user-connection-limits`）要先開，而且要重啟。
5. **`--collect.perf_schema.replication_applier_status_by_worker` 在 MariaDB 一定失敗**：MariaDB 的表沒有 exporter 要的欄位，而 exporter 的版本判斷把 MariaDB 10.x／11.x 當成「大於 8.0」，不會跳過（推論，未實測）。

## 選的版本 {#versions}

| 版本線 | 這頁用的 tag | 狀態（REST API，2026-11-10） |
|---|---|---|
| 10.6 | `mariadb-10.6.28` | LTS，EOL 2026-07-06（已過） |
| 10.11 | `mariadb-10.11.19` | LTS，EOL 2028-02-16 |
| 11.4 | `mariadb-11.4.13` | LTS，EOL 2029-05-29；目前卡片核對的版本 |
| 11.8 | `mariadb-11.8.9` | LTS，EOL 2030-02-13 |
| 12.3 | `mariadb-12.3.3` | LTS，EOL 2029-06-12 |
| mysqld_exporter | `v0.21.0` | 最新 release |

13.0 是 rolling release，這頁不比對。

## 版本差異表 {#diff}

| 版本 | 變更 | 對監控的影響（哪個 metric／collector／規則） | exporter 有沒有處理 | 依據 |
|---|---|---|---|---|
| 10.6–12.3 | `REPLICA` 關鍵字對應到 `SLAVE`；`SHOW REPLICA STATUS` 與 `SHOW SLAVE STATUS` 是同一個語句，欄位名不變 | `mysql_slave_status_slave_io_running`、`slave_sql_running`、`seconds_behind_master`、`master_server_id` 在五條線名稱相同；MariaDB 卡只需要寫舊名 | 有：`SHOW ALL SLAVES STATUS` 在 MariaDB 第一個就成功 | [^ver-mdb-replica-keyword][^ver-mdb-show-all-slaves][^ver-mdb106-sbm-col][^ver-mdb114-sbm-col][^ver-ex-slave-loop] |
| 10.6–12.3 | `SHOW ALL SLAVES STATUS` 多 `Connection_name`、`Gtid_Slave_Pos`、`Retried_transactions` 等欄位 | 每個 replication 來源一組 series，用 `connection_name` label 區分；`Gtid_IO_Pos`、`Gtid_Slave_Pos` 拆成 `{domain_id,server_id}` | 有 | [^ver-mdb-show-all-slaves][^my-trace-ex-slave-gtid] |
| 11.8 | replica 狀態改由 `information_schema.SLAVE_STATUS` 產生；新增 `Master_last_event_time`、`Slave_last_event_time`（datetime）、`Master_Slave_time_diff`（秒），只在 `SHOW ALL SLAVES STATUS` 出現 | exporter 會多輸出 `mysql_slave_status_master_last_event_time`、`slave_last_event_time`（parseStatus 把 datetime 轉成 Unix 秒）與 `master_slave_time_diff`，可以當 `replication-lag` 的第二個來源。datetime 沒有時區，Go 會當 UTC 解析，伺服器時區不是 UTC 時兩個時間戳會差一個時區偏移（推論，未實測） | 部分：自動輸出，但不知道時區、型別是 untyped | [^ver-mdb118-is-slave-status][^ver-mdb118-last-event-time][^ver-mdb118-all-only-cols][^ver-ex-parse-datetime] |
| 10.6–12.3 | 權限：`REPLICATION CLIENT` 是 `BINLOG MONITOR` 的別名；`SHOW SLAVE STATUS` 要 `SLAVE MONITOR`（`REPLICA MONITOR` 同義） | exporter README 只給 `PROCESS, REPLICATION CLIENT, SELECT`，在 replica 上 `collector_success{collector="slave_status"}` 0，所有 replication 規則回傳空 | 沒有：README 沒提 `SLAVE MONITOR` | [^ver-mdb-repl-client-alias][^ver-mdb-slave-monitor-kw][^ver-ex-readme-grants] |
| 10.6／10.11 | `SHOW SLAVE STATUS` 允許 `SLAVE MONITOR` 或 `SUPER`；`SHOW BINARY LOGS` 允許 `BINLOG MONITOR` 或 `SUPER` | 只給 `SUPER` 的監控帳號還能用 | — | [^ver-mdb1011-slave-status-priv][^ver-mdb1011-binlogs-priv] |
| 11.0+（11.4、11.8、12.3） | 兩者都只認 `SLAVE MONITOR`／`BINLOG MONITOR`；舊版（< 11.0）的 grant 表載入時，有 `SUPER` 的帳號自動補上這些權限 | **在 11.x 新建**、只給 `SUPER` 的帳號：`slave_status` 與 `binlog_size` collector 變 0。從 10.x 升上來的帳號不受影響，所以同一份部署腳本在新舊環境結果不同 | 沒有 | [^ver-mdb114-slave-status-priv][^ver-mdb114-binlogs-priv][^ver-mdb114-super-upgrade] |
| 10.6–12.3 | `SHOW SLAVE HOSTS` 要 `REPLICATION MASTER ADMIN` | `--collect.slave_hosts`（預設關）在一般監控帳號上失敗 | 沒有 | [^ver-mdb114-slave-hosts-priv] |
| 10.6–12.3 | `performance_schema` 預設 OFF（12.3 也是） | `slow-sql-visibility`、`statement-errors`、`user-connection-limits`（`sys` 的資料來自 performance_schema）沒開就沒有資料 | 沒有：collector 查到空表也回報成功 | [^ver-mdb-pfs-default-off][^ver-mdb123-pfs-default-off] |
| 10.6–12.3 | `performance_schema.replication_applier_status_by_worker` 只有 `LAST_SEEN_TRANSACTION`、`LAST_ERROR_*`、`WORKER_IDLE_TIME` 等欄位，沒有 `LAST_APPLIED_TRANSACTION_*` | 開 `--collect.perf_schema.replication_applier_status_by_worker` 時查詢欄位不存在，collector 0 | 沒有：這個 collector 的門檻是 8.0，MariaDB 10.x／11.x／12.x 轉成浮點數都大於 8.0 | [^ver-mdb-pfs-worker-cols][^my-applier-worker][^ver-ex-applier-version][^ver-ex-version-float] |
| 10.6–12.3 | `Slave_running`（ON／OFF）與 `Slaves_running`（執行中的 replica 連線數）在 global status | 沒在複製時是 0，不會整組消失；可以當 `replica-status-present` 的補充（`mysql_global_status_slave_running`、`mysql_global_status_slaves_running`） | 有：`slave_running` 標成 gauge | [^ver-mdb-slave-running][^ver-mdb-slaves-running][^my-global-status-skip] |
| 10.6–12.3 | semi-sync 內建，名稱一直是 `Rpl_semi_sync_master_*`、`rpl_semi_sync_master_enabled` | `semi-sync` 在 MariaDB 卡只要寫 `master` | 不需要 | [^ver-mdb123-semisync-master] |
| 10.6／10.11 → 11.x | InnoDB change buffer 移除，`Innodb_ibuf_size` 等 ibuf 狀態消失 | `mysql_global_status_innodb_ibuf_*` 在 11.x 沒有 | 不需要 | [^ver-mdb1011-ibuf-size] |
| 10.6–12.3 | `Innodb_history_list_length`、`Innodb_deadlocks` 在五條線都有 | `undo-history`、`deadlocks` 不用開 `innodb_metrics` | 不需要 | [^ver-mdb114-history-len][^mariadb-innodb-deadlocks] |
| 11.4 | 新增 `max_binlog_total_size`、`slave_connections_needed_for_purge` | `binlog-growth` 用的 `mysql_global_variables_slave_connections_needed_for_purge` 在 10.6／10.11 不存在 | 不需要 | [^ver-mdb114-max-binlog-total][^ver-mdb114-purge-needed] |
| 11.4 | 新增 status 變數 `Binlog_disk_use`（binlog 總大小） | 不開 `--collect.binlog_size` 也能看 binlog 用量：`mysql_global_status_binlog_disk_use` | 不需要 | [^ver-mdb114-binlog-disk-use] |
| 11.4 | 新增 `Max_used_connections_time`（datetime） | `connection-headroom` 可以知道高水位發生的時間；exporter 把它轉成 Unix 秒（時區同上） | 部分 | [^ver-mdb114-max-used-conn-time][^ver-ex-parse-datetime] |
| 11.8 | 新增 `Tmp_space_used`、`Max_tmp_space_used` 與上限 `max_tmp_session_space_usage`、`max_tmp_total_space_usage` | `tmp-tables`、`disk-space` 可以直接看暫存空間用量；超過上限時查詢會被擋 | 不需要 | [^ver-mdb118-tmp-space][^ver-mdb118-max-tmp-total] |
| 12.3 | 新增 `binlog_storage_engine`（預設空，啟用時 binlog 存在 storage engine 裡） | 這頁沒有核對啟用後 `SHOW BINARY LOGS` 與 `Binlog_disk_use` 的輸出；啟用前 `binlog-growth`、`binlog-pitr` 的做法要重新核對 | 未核對 | [^ver-mdb123-binlog-engine] |
| 10.6–12.3 | query cache 仍在（`Qcache_*` status） | MariaDB 卡可以有 query cache 的 metric；MySQL 8.0 起沒有 | 不需要 | [^ver-mdb-qcache] |
| 10.6–12.3 | `extra_port`（thread pool 用的額外連線埠，預設 0） | 連線打滿時的管理入口；exporter 預設不用 | 沒有 | [^ver-mdb123-extra-port] |

## mysqld_exporter 在 MariaDB 上的行為 {#exporter}

| 項目 | 做法 | 會怎麼壞 | 依據 |
|---|---|---|---|
| 版本判斷 | `@@version` 取「主版號.次版號」轉浮點數，和 collector 的門檻比 | MariaDB 10.6 → 10.6、10.11 → 10.11、11.4 → 11.4，都大於所有門檻（最高 8.0），所以沒有 collector 會被跳過，也包括 MySQL 專用的 | [^ver-ex-version-float][^ver-ex-version-gate] |
| 判斷是 MariaDB | `@@version` 含 `mariadb` | 只用在 `innodb_tablespaces`（10.5 起不查 `SPACE_TYPE`）與 `eventsstatements`（MariaDB 不查 `SUM_CPU_TIME`） | [^ver-ex-flavor][^ver-ex-tablespaces-mariadb][^ver-ex-stmt-8028] |
| replica 狀態 | `SHOW ALL SLAVES STATUS` 成功就停 | 權限不足時三種語法都失敗，collector 0；不是 replica 時 0 列，collector 1、沒有 series | [^ver-ex-slave-loop][^my-slave-skip] |
| query_response_time | 預設開；`@@query_response_time_stats` 查不到或是 0 就直接回成功 | 沒裝 plugin 時 collector 1、沒有 series，看不出來 | [^ver-ex-qrt-nil][^my-default-collectors] |

## 各版本要注意的地方 {#notes}

- **10.6**：社群支援已在 2026-07-06 結束。沒有 `slave_connections_needed_for_purge`、`max_binlog_total_size`、`Binlog_disk_use`；`SUPER` 仍涵蓋 `SLAVE MONITOR`。
- **10.11**：和 10.6 在本卡用到的 metric 上沒有找到差異（只比對了 replica 狀態欄位與 global status 名稱）。
- **11.4**：新建監控帳號只給 `SUPER` 不夠；多了 `Binlog_disk_use`、`Max_used_connections_time`、binlog 總量上限與「等 replica 再 purge」；change buffer 狀態消失。
- **11.8**：replica 狀態多了事件時間與 `Master_Slave_time_diff`；多了暫存空間用量與上限。
- **12.3**：`binlog_storage_engine` 是新的 binlog 實作，啟用前要重新核對 binlog 相關訊號；`performance_schema` 仍預設 OFF。

## 卡片上會隨版本變的訊號 {#signals}

| 訊號 | 隨版本變什麼 | 建議 |
|---|---|---|
| `exporter-grants` | 11.0 起 `SUPER` 不含 `SLAVE MONITOR`／`BINLOG MONITOR`；`REPLICATION CLIENT` 只等於 `BINLOG MONITOR` | MariaDB 卡寫明 `GRANT PROCESS, BINLOG MONITOR, SLAVE MONITOR, SELECT`；要 `slave_hosts` 再加 `REPLICATION MASTER ADMIN` |
| `replication-running`、`replication-error`、`replica-status-present`、`role-detection` | 名稱五條線不變（`slave_*`） | 只寫舊名；`replica-status-present` 可加 `mysql_global_status_slave_running` 當不會消失的補充 |
| `replication-lag` | 11.8 起多 `master_slave_time_diff`、`master_last_event_time` | 11.8+ 可加；heartbeat 仍是主要來源 |
| `binlog-growth`、`binlog-pitr` | `slave_connections_needed_for_purge`、`max_binlog_total_size`、`Binlog_disk_use` 只有 11.4+；12.3 有新 binlog 實作 | `applies_when` 標 11.4+；12.3 啟用 `binlog_storage_engine` 時標未核對 |
| `connection-headroom` | 11.4 起有 `Max_used_connections_time` | 可加，供事後分析 |
| `tmp-tables`、`disk-space` | 11.8 起有 `Tmp_space_used` 與上限 | 11.8+ 可加「用量 / max_tmp_total_space_usage」 |
| `innodb-pressure` | 11.x 沒有 change buffer 狀態 | 不要用 `innodb_ibuf_*` |
| `slow-sql-visibility`、`statement-errors`、`user-connection-limits` | 五條線都要先開 `performance_schema` | 維持現有 `applies_when` |
| `semi-sync` | 名稱只有 `master` | MariaDB 卡只寫 `master` |

其他訊號（`galera-*` 的 `wsrep_*` 變數、`connections`、`long-transactions`、`lock-waits` 等）這頁沒有逐版比對 Galera 與 wsrep 變數的差異；只確認 `wsrep_on` 在五條線都有。

MySQL 的差異在 [MySQL 各版本的監控差異](mysql.md)。

拆卡時新增的兩個飽和訊號：`Threads_created` 五條線都有；`Innodb_data_pending_fsyncs` 的內容在 10.11 換了（10.6 含 redo log 的 pending flush，10.11 起只算 tablespace），見卡片的 [thread-cache](../../components/mariadb.md#thread-cache)、[pending-fsyncs](../../components/mariadb.md#pending-fsyncs)。<span class="at-chip at-ev-code">讀碼</span>[^spl-mdb106-threads-created][^spl-mdb123-threads-created][^spl-mdb106-pending-fsyncs-calc][^spl-mdb1011-pending-fsyncs]
