# MySQL 各版本的監控差異

這頁列出 MySQL 8.0、8.4 LTS、9.7 LTS 之間會影響監控的差異：哪些 metric 改名或消失、哪些預設值改了、mysqld_exporter 有沒有處理。lab 與多數原始碼參考是 MySQL 8.4.7；這頁補上其他版本線。[MySQL 卡](../../components/mysql.md)的 `versions_checked` 與版本相關的訊號、規則依這頁寫；本體怎麼監控見 [MySQL 頁](../mysql.md)。

依據是各版本線最新 tag 的原始碼（`mysql-8.0.46`、`mysql-8.4.13`、`mysql-9.7.4`，2026-11-10 從 [tag 列表](https://github.com/mysql/mysql-server/tags)選）、mysqld_exporter v0.21.0（目前最新 release）的原始碼，以及官方手冊。沒有做實驗，全部是<span class="at-chip at-ev-code">讀碼</span>；標「推論」的是從原始碼推出、沒有單獨驗證的結果。

9.x 最新的一條線是 9.7，手冊把它列為 LTS（[MySQL Releases](https://dev.mysql.com/doc/refman/9.7/en/mysql-releases.html)），所以這頁比較的是三條長期線。

## 結論 {#summary}

1. **8.0 → 8.4 升版時，replication metric 全部改名。**exporter 在 8.0 用 `SHOW SLAVE STATUS`（得到舊欄位名），8.4 起這個語法被移除，exporter 改用 `SHOW REPLICA STATUS`（新欄位名）。`mysql_slave_status_slave_io_running` 變成 `mysql_slave_status_replica_io_running`，`seconds_behind_master` 變成 `seconds_behind_source`。只寫舊名稱的規則在升版後回傳空，不會報錯。
2. **exporter 帳號如果用 `mysql_native_password`，8.4 起預設登不進去，9.x 完全不行。**`mysql_up` 變 0，所有 `mysql_*` 只剩 `mysql_up`。
3. **8.4 起 Group Replication 成員被踢出時預設進入 `offline_mode`。**exporter 帳號通常沒有 `CONNECTION_ADMIN`，連不上那台，`gr-member-state` 在那台看到的是 `mysql_up` 0，而不是 member_state 變 ERROR（推論）。
4. **9.7 把 replication applier metrics 與 Group Replication flow control 統計放進社群版**，但都是要自己 `INSTALL COMPONENT` 的 component；exporter 對 applier metrics 表沒有 collector。
5. **舊的 semi-sync plugin（`Rpl_semi_sync_master_*`）在 9.x 沒有了**，只剩 `Rpl_semi_sync_source_*`。

## 選的版本 {#versions}

| 版本線 | 這頁用的 tag | 狀態 |
|---|---|---|
| 8.0 | `mysql-8.0.46` | 8.0 最後一條 tag（這頁沒有核對 8.0 的支援狀態） |
| 8.4 LTS | `mysql-8.4.13` | 目前卡片核對過 8.4.7 |
| 9.7 LTS | `mysql-9.7.4` | 9.x 最新；手冊列為 LTS |
| mysqld_exporter | `v0.21.0` | 最新 release（[releases](https://github.com/prometheus/mysqld_exporter/releases/tag/v0.21.0)） |

## 版本差異表 {#diff}

| 版本 | 變更 | 對監控的影響（哪個 metric／collector／規則） | exporter 有沒有處理 | 依據 |
|---|---|---|---|---|
| 8.0 | `SHOW SLAVE STATUS` 與 `SHOW REPLICA STATUS` 都能用；用舊語法時欄位改回舊名（`Slave_IO_Running`、`Seconds_Behind_Master`、`Master_Host`） | exporter 先試成功的是 `SHOW SLAVE STATUS`，所以 8.0 上輸出的是 `mysql_slave_status_slave_*`、`seconds_behind_master`、`master_server_id` | 有：依序試三種語法，取第一個成功的 | [^ver-my80-show-slave-status][^ver-my80-old-names][^ver-ex-slave-loop] |
| 8.4 | `SHOW SLAVE STATUS` 移除，只剩 `SHOW REPLICA STATUS`，欄位是新名（`Replica_IO_Running`、`Seconds_Behind_Source`、`Source_Host`） | 同一台升版後 metric 名稱換掉：`replication-running`、`replication-lag`、`replica-status-present`、`role-detection` 只寫舊名的規則回傳空。`mysql_slave_status_*` 的 metric 前綴不變（collector 名稱就是 slave_status） | 有：舊語法 parse error 後改試 `SHOW REPLICA STATUS`；`master_host`／`master_uuid` label 會改讀 `Source_Host`／`Source_UUID`，label 名稱不變 | [^ver-my84-show-replica-only][^ver-my84-sbs-col][^ver-ex-slave-loop][^ver-ex-slave-source-uuid] |
| 8.4 | 0.21.0 起 exporter 把 `replica_io_running`、`replica_sql_running`、`slave_io_running`、`slave_sql_running`、`seconds_behind_master`、`seconds_behind_source` 標成 gauge，其他欄位仍是 untyped | 名稱沒變，只有 type；看 metric type 的工具要注意 | 有（0.21.0 breaking change） | [^ver-ex-slave-types] |
| 9.7 | `SHOW REPLICA STATUS` 欄位清單與 8.4 相同（60 個欄位） | 沒有新的改名 | 不需要 | [^ver-my97-show-replica-only] |
| 8.4 | `SHOW SLAVE HOSTS` 移除，改 `SHOW REPLICAS` | `--collect.slave_hosts`（預設關）在 8.4 改走 `SHOW REPLICAS` | 有：只在 1064 parse error 時改用新語法，權限錯誤照實回報 | [^ver-my80-show-slave-hosts][^ver-my84-show-replicas][^ver-ex-slave-hosts-fallback] |
| 8.4 | `SHOW MASTER STATUS` 移除，改 `SHOW BINARY LOG STATUS` | exporter 不用這個語法；自訂查詢（query-exporter）或腳本要改 | 不需要（`binlog_size` 用 `SHOW BINARY LOGS`，8.4 仍在） | [^ver-my80-show-master-status][^ver-my84-binary-log-status][^ver-ex-binlog-logbin] |
| 8.4 | `Com_show_slave_status`、`Com_show_master_status`、`Com_change_master`、`Com_slave_start` 等舊名稱的 command 計數移除 | `mysql_global_status_commands_total{command="show_slave_status"}` 消失；新的是 `show_replica_status` | 不需要（照 status 變數名稱輸出） | [^ver-my80-com-show-slave-status][^ver-my84-com-show-replica-status]；[What Is New in 8.4](https://dev.mysql.com/doc/refman/8.4/en/mysql-nutshell.html) |
| 8.0–9.7 | `Slave_open_temp_tables`、`Slave_rows_last_search_algorithm_used` 這兩個 status 變數在 9.7 仍和 `Replica_*` 並存 | `mysql_global_status_slave_open_temp_tables` 與 `replica_open_temp_tables` 兩個都有 | 不需要 | [^ver-my97-slave-open-temp] |
| 8.0–9.7 | `log_slave_updates`、`slave_net_timeout`、`slave_parallel_workers` 等舊名稱系統變數在 9.7 仍是 deprecated alias | 舊名稱還在；這頁沒有核對 `SHOW GLOBAL VARIABLES` 是否兩個名稱都列出 | 不需要 | [^ver-my97-log-slave-updates-alias] |
| 8.4 | `expire_logs_days` 移除，只剩 `binlog_expire_logs_seconds` | `mysql_global_variables_expire_logs_days` 在 8.4 消失；`binlog-pitr` 規則要用 `binlog_expire_logs_seconds` | 不需要 | [^ver-my80-expire-logs-days][^ver-my84-binlog-expire-seconds] |
| 9.7 | `replica_parallel_type`（與 `slave_parallel_type`）移除 | 字串變數，exporter 本來就不輸出；看設定的檢查腳本要改 | 不需要 | [^ver-my84-replica-parallel-type]；[What Is New in 9.7](https://dev.mysql.com/doc/refman/9.7/en/mysql-nutshell.html) |
| 8.0／8.4 | semi-sync 有兩組 plugin：舊的 `semisync_master`／`semisync_slave`（status 前綴 `Rpl_semi_sync_master_`）與新的 `semisync_source`／`semisync_replica`（`Rpl_semi_sync_source_`） | `semi-sync` 規則要看裝的是哪一組；card 已經兩組一起列 | 不需要（照名稱輸出） | [^ver-my84-semisync-master-plugin][^ver-my84-semisync-master-prefix] |
| 9.7 | 舊 semi-sync plugin 移除，只能裝 `semisync_source`／`semisync_replica` | 只剩 `mysql_global_status_rpl_semi_sync_source_*`；寫 `master` 的規則在 9.7 回傳空 | 不需要 | [^ver-my97-semisync-plugins][^ver-my97-semisync-source-status] |
| 8.4 | `mysql_native_password` 仍可載入，但預設不啟用（要 `--mysql-native-password=ON`）；`default_authentication_plugin` 移除 | 8.0 上用 native password 建的 exporter 帳號，升到 8.4 後登入失敗：`mysql_up` 0，`exporter-grants` 要包含帳號的認證方式 | exporter 用 go-sql-driver v1.10.1，支援 `caching_sha2_password`；沒有 TLS 的 TCP 連線會向 server 要 RSA 公鑰完成完整認證 | [^ver-my84-native-pw-cached][^ver-ex-gsd-sha2]；[What Is New in 8.4](https://dev.mysql.com/doc/refman/8.4/en/mysql-nutshell.html) |
| 9.7 | `mysql_native_password` 不在內建認證 plugin 清單裡 | native password 帳號在 9.x 登不進去，症狀同上 | 同上 | [^ver-my97-no-native-pw] |
| 8.0–9.7 | `performance_schema` 預設 ON | `slow-sql-visibility`、`statement-errors`、`user-connection-limits`、`metadata-locks` 在三條線都能用 | — | [^ver-my-pfs-default-on] |
| 8.0.28+ | `events_statements_summary_by_digest` 有 `SUM_CPU_TIME` 等欄位 | `--collect.perf_schema.eventsstatements` 在 8.0.28 以上多輸出 CPU／lock time | 有：只在 MySQL 且版本 ≥ 8.0.28 時用新查詢 | [^ver-ex-stmt-8028] |
| 8.4 | Group Replication 的 `group_replication_exit_state_action` 預設從 `READ_ONLY` 改成 `OFFLINE_MODE` | 被踢出的成員設 `offline_mode=ON`，沒有 `CONNECTION_ADMIN` 的帳號被斷線、新連線被拒。exporter 在那台變成 `mysql_up` 0，那台的 `mysql_perf_schema_replication_group_member_info` 消失（推論）。`gr-member-state` 要把「某成員的 `mysql_up` 變 0」也算進去，或給 exporter 帳號 `CONNECTION_ADMIN` | 沒有 | [^ver-my80-gr-exit-readonly][^ver-my84-gr-exit-offline]；[手冊](https://dev.mysql.com/doc/refman/8.4/en/group-replication-responses-failure-exit.html) |
| 8.4 | InnoDB 預設值改變：`innodb_adaptive_hash_index` OFF、`innodb_io_capacity` 10000、`innodb_change_buffering` none、`innodb_flush_method` O_DIRECT 等 | `innodb-pressure`、`buffer-pool-misses` 這類以 8.0 為基準的門檻與 dashboard 在升版後要重新取基準 | 不需要 | [What Is New in 8.4](https://dev.mysql.com/doc/refman/8.4/en/mysql-nutshell.html) |
| 9.7 | 新的 status 變數 `Count_hit_tmp_table_size`、`Table_open_cache_triggers_*` | `tmp-tables`、`table-open-cache` 在 9.7 多一個可用的計數（`mysql_global_status_count_hit_tmp_table_size`） | 不需要（global_status 輸出所有數值型 status 變數） | [^ver-my97-count-hit-tmp][^my-global-status-skip] |
| 9.7 | `component_replication_applier_metrics` 進社群版：`performance_schema.replication_applier_metrics`、`replication_applier_progress_by_worker` | 有更細的 applier 統計（每個 channel、每個 worker），可補 `replication-lag` | **沒有 collector**；要用自訂查詢 | [^ver-my97-applier-metrics][^ver-my97-applier-progress]；[9.7.0 release notes](https://dev.mysql.com/doc/relnotes/mysql/9.7/en/news-9-7-0.html) |
| 9.7 | `component_group_replication_flow_control_stats` 進社群版：`Gr_flow_control_throttle_count`、`_throttle_time_sum`、`_throttle_active_count`、`_throttle_last_throttle_timestamp` | `gr-flow-control` 在 9.7 社群版可以直接看「被節流多久」，不只佇列長度；8.4 社群版沒有這個 component | 部分：裝了 component 後 global_status 會輸出數值型的那幾個（推論，未實測） | [^ver-my97-gr-fc-stats][^my-global-status-skip] |
| 8.0–9.7 | `admin_address`／`admin_port`（8.0.14 起）在三條線都有 | `connections` 打滿時，有 `SERVICE_CONNECTION_ADMIN` 的帳號可以走 admin port；exporter 預設不用 | 沒有（要自己在 DSN 指 admin port） | [^ver-my80-admin-port] |
| 9.7.4 | 新增 `VERSION_WITH_UPDATE()`（四段版本號）；`VERSION()`／`@@version` 仍是三段 | exporter 用 `@@version` 判斷版本，不受影響 | 不需要 | [What Is New in 9.7](https://dev.mysql.com/doc/refman/9.7/en/mysql-nutshell.html) |

## mysqld_exporter 怎麼處理版本 {#exporter}

| 項目 | 做法 | 會怎麼壞 | 依據 |
|---|---|---|---|
| 版本判斷 | 每次 scrape 跑 `SELECT @@version`，取「主版號.次版號」轉成浮點數，小於 collector 的 `Version()` 就跳過 | 被跳過的 collector 不輸出 `collector_success`（不是 0）。MySQL 8.0／8.4／9.7 都高於所有 collector 的門檻（最高 8.0），所以不會被跳過 | [^ver-ex-version-float][^ver-ex-version-gate] |
| MySQL／MariaDB 判斷 | `@@version` 字串含 `mariadb` 就當 MariaDB，否則當 MySQL | 只有 `eventsstatements`（8.0.28 判斷）與 `innodb_tablespaces` 用到 | [^ver-ex-flavor][^ver-ex-stmt-8028][^ver-ex-tablespaces-mariadb] |
| replica 狀態 | 依序試 `SHOW ALL SLAVES STATUS`、`SHOW SLAVE STATUS`、`SHOW REPLICA STATUS`，每個再試 ` NONBLOCKING`／` NOLOCK` 後綴；metric 名稱直接用欄位名小寫 | 8.0 上第一個成功的是 `SHOW SLAVE STATUS` → 舊名；8.4／9.7 前兩種都 parse error → 新名。名稱跟著伺服器版本換，exporter 不做對應 | [^my-slave-queries][^ver-ex-slave-loop] |
| 權限不足 | 三種語法都失敗時回傳最後一個錯誤 | `collector_success{collector="slave_status"}` 0，replication metric 全部消失 | [^ver-ex-slave-loop] |
| 沒在複製 | 查詢成功但 0 列 | `collector_success` 1，沒有 `mysql_slave_status_*`；和「升版改名」「權限被收回」在規則上看起來一樣 | [^my-slave-skip] |

## 各版本要注意的地方 {#notes}

- **8.0**：replication metric 是 `slave_*`／`*_master` 舊名；semi-sync 可能是 `master` 或 `source` 兩組其中一組；`expire_logs_days` 還在。升 8.4 前先把規則改成新舊名稱一起比對（card 的 `replication-running` 已經這樣寫），並確認 exporter 帳號不是 `mysql_native_password`。
- **8.4**：replication metric 換成 `replica_*`／`*_source`；Group Replication 被踢出的成員預設進 `offline_mode`，exporter 會連不上；InnoDB 預設值改了，門檻要重新取基準。
- **9.7**：native password 與舊 semi-sync plugin 都沒有了；applier metrics、GR flow control 統計可以在社群版裝 component 取得，但 applier metrics 要自訂查詢。

## 卡片上會隨版本變的訊號 {#signals}

| 訊號 | 隨版本變什麼 | 建議 |
|---|---|---|
| `db-up`、`exporter-grants` | 8.4 native password 預設停用、9.x 移除 | grants 檢查加一項：`SELECT plugin FROM mysql.user WHERE user='exporter'` 不是 `mysql_native_password` |
| `replication-running`、`replica-status-present`、`role-detection` | 8.0 `slave_*`、8.4+ `replica_*` | 保持新舊名稱一起比對；`replica-status-present` 在升版那一刻會誤報一次（舊名消失），要在註解寫明 |
| `replication-lag` | 8.0 `seconds_behind_master`、8.4+ `seconds_behind_source`；9.7 可另裝 applier metrics component | 規則兩個名稱一起列；applier metrics 列為可選的自訂查詢 |
| `replication-error` | `last_sql_errno`／`last_io_errno` 欄位名在新舊語法相同 | 不受影響 |
| `semi-sync` | 8.0／8.4 兩組名稱擇一，9.7 只剩 `source` | 保持兩組一起比對 |
| `binlog-pitr` | 8.4 移除 `expire_logs_days` | 只用 `binlog_expire_logs_seconds` |
| `gr-member-state`、`gr-quorum` | 8.4 起被踢出的成員進 `offline_mode`，exporter 連不上 | 把「GR 成員的 `mysql_up` == 0」納入；或給 exporter `CONNECTION_ADMIN`（代價是 max_connections 打滿時也會佔保留連線） |
| `gr-flow-control` | 9.7 社群版可裝 flow control 統計 component | 9.7 加 `Gr_flow_control_throttle_*`；8.4 維持只看佇列 |
| `innodb-pressure`、`buffer-pool-misses` | 8.4 InnoDB 預設值改變 | 升版後重新取基準 |
| `tmp-tables` | 9.7 多 `Count_hit_tmp_table_size` | 9.7 可加 |
| `slow-sql-visibility`、`statement-errors` | exporter 在 8.0.28 以上多抓 CPU／lock time | 不影響現有規則 |

其他訊號（`connections`、`long-transactions`、`deadlocks`、`lock-waits`、`slow-queries`、`table-open-cache`、`autoinc-exhaustion` 等）在這三條線用的 status 變數與 information_schema 表沒有找到改名或移除。這頁沒有逐一比對 `information_schema.INNODB_METRICS` 預設啟用的計數器，`undo-history` 與 `deadlocks` 的 MySQL 寫法在 8.0 與 9.7 上未核對。

MariaDB 的差異在 [MariaDB 各版本的監控差異](mariadb.md)。

拆卡時新增的兩個飽和訊號（`Threads_created`、`Innodb_data_pending_fsyncs`）在 8.0.46、8.4.13、9.7.4 都存在而且算法相同，見卡片的 [thread-cache](../../components/mysql.md#thread-cache)、[pending-fsyncs](../../components/mysql.md#pending-fsyncs)。<span class="at-chip at-ev-code">讀碼</span>[^spl-my80-threads-created][^spl-my97-threads-created][^spl-my80-pending-fsyncs][^spl-my97-pending-fsyncs]
