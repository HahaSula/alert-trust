# postgres_exporter v0.20.1 送出的 SQL（log_statement=all，連續兩次 scrape）

環境：postgres:16.15-alpine、postgres:17.11-alpine（單機、非 replica），quay.io/prometheuscommunity/postgres-exporter:v0.20.1；
帳號 exporter（pg_monitor）；另開 collector：stat_wal_receiver、database_wraparound、postmaster、stat_checkpointer、long_running_transactions。
log_line_prefix 是 '%m [%p] %u@%d app=%a '：[pid] 不同代表不同的連線。多行 SQL 已壓成一行。

## p16：PostgreSQL 16.15 on x86_64-pc-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit

```text
23:53:22.492 [74] exporter@postgres app=[unknown] LOG: connection authenticated: identity="exporter" method=scram-sha-256 (/var/lib/postgresql/data/pg_hba.conf:128)
23:53:22.492 [74] exporter@postgres app=[unknown] LOG: connection authorized: user=exporter database=postgres
23:53:22.493 [75] exporter@postgres app=[unknown] LOG: connection authenticated: identity="exporter" method=scram-sha-256 (/var/lib/postgresql/data/pg_hba.conf:128)
23:53:22.493 [75] exporter@postgres app=[unknown] LOG: connection authorized: user=exporter database=postgres
23:53:22.494 [74] exporter@postgres app=[unknown] LOG: statement: ;
23:53:22.494 [74] exporter@postgres app=[unknown] LOG: statement: SELECT version();
23:53:22.494 [75] exporter@postgres app=[unknown] LOG: statement: SELECT version();
23:53:22.495 [74] exporter@postgres app=[unknown] LOG: statement: SELECT * FROM pg_stat_database_conflicts;
23:53:22.495 [75] exporter@postgres app=[unknown] LOG: statement: SELECT column_name FROM information_schema.columns WHERE table_name = 'pg_stat_wal_receiver' and column_name = 'flushed_lsn' 
23:53:22.517 [75] exporter@postgres app=[unknown] LOG: statement: SELECT COUNT(*) AS segments, SUM(size) AS size FROM pg_ls_waldir() WHERE name ~ '^[0-9A-F]{24}$'
23:53:22.518 [75] exporter@postgres app=[unknown] LOG: statement: SELECT current_database() datname, schemaname, relname, heap_blks_read, heap_blks_hit, idx_blks_read, idx_blks_hit, toast_blks_read, toast_blks_hit, tidx_blks_read, tidx_blks_hit FROM pg_statio_user_tables
23:53:22.522 [75] exporter@postgres app=[unknown] LOG: statement: SELECT pg_database.datname, tmp.state, tmp2.usename, tmp2.application_name, tmp2.backend_type, tmp2.wait_event_type, tmp2.wait_event, COALESCE(count,0) as count, COALESCE(max_tx_duration,0) as max_tx_duration FROM ( VALUES ('active'), ('idle'), ('idle in transaction'), ('idle in transaction (aborted)'), ('fastpath function call'), ('disabled') ) AS tmp(state) CROSS JOIN pg_database LEFT JOIN ( SELECT datname, state, usename, application_name, backend_type, wait_event_type, wait_event, count(*) AS count, MAX(EXTRACT(EPOCH FROM now() - xact_start))::float AS max_tx_duration FROM pg_stat_activity WHERE pid <> pg_backend_pid() GROUP BY datname,state,usename,application_name,backend_type,wait_event_type,wait_event) AS tmp2 ON tmp.state = tmp2.state AND pg_database.datname = tmp2.datname 
23:53:22.527 [75] exporter@postgres app=[unknown] LOG: statement: SELECT pg_database.datname, pg_database.datconnlimit FROM pg_database;
23:53:22.528 [75] exporter@postgres app=[unknown] LOG: statement: SELECT CASE WHEN NOT pg_is_in_recovery() THEN 0 WHEN pg_last_wal_receive_lsn () = pg_last_wal_replay_lsn () THEN 0 ELSE GREATEST (0, EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))) END AS lag, CASE WHEN pg_is_in_recovery() THEN 1 ELSE 0 END as is_replica, GREATEST (0, EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))) as last_replay
23:53:22.529 [75] exporter@postgres app=[unknown] LOG: statement: SELECT COUNT(*) as transactions, MAX(EXTRACT(EPOCH FROM clock_timestamp() - pg_stat_activity.xact_start)) AS oldest_timestamp_seconds FROM pg_catalog.pg_stat_activity WHERE state IS DISTINCT FROM 'idle' AND query NOT LIKE 'autovacuum:%' AND pg_stat_activity.xact_start IS NOT NULL AND pid <> pg_backend_pid(); 
23:53:22.530 [75] exporter@postgres app=[unknown] LOG: statement: SELECT trim(both '''' from substring(conninfo from 'host=([^ ]*)')) as upstream_host, slot_name, status, (receive_start_lsn- '0/0') % (2^52)::bigint as receive_start_lsn, (flushed_lsn - '0/0') % (2^52)::bigint as flushed_lsn, receive_start_tli, received_tli, extract(epoch from last_msg_send_time) as last_msg_send_time, extract(epoch from last_msg_receipt_time) as last_msg_receipt_time, (latest_end_lsn - '0/0') % (2^52)::bigint as latest_end_lsn, extract(epoch from latest_end_time) as latest_end_time, substring(slot_name from 'repmgr_slot_([0-9]*)') as upstream_node FROM pg_catalog.pg_stat_wal_receiver 
23:53:22.532 [75] exporter@postgres app=[unknown] LOG: statement: SELECT datname, age(d.datfrozenxid) as age_datfrozenxid, mxid_age(d.datminmxid) as age_datminmxid FROM pg_catalog.pg_database d WHERE d.datallowconn 
23:53:22.533 [75] exporter@postgres app=[unknown] LOG: statement: SELECT checkpoints_timed ,checkpoints_req ,checkpoint_write_time ,checkpoint_sync_time ,buffers_checkpoint ,buffers_clean ,maxwritten_clean ,buffers_backend ,buffers_backend_fsync ,buffers_alloc ,stats_reset FROM pg_stat_bgwriter;
23:53:22.534 [75] exporter@postgres app=[unknown] LOG: statement: SELECT application_name, client_addr::text, state, s.slot_name, (case pg_is_in_recovery() when 't' then pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_lsn('0/0'))::float else pg_wal_lsn_diff(pg_current_wal_lsn(), pg_lsn('0/0'))::float end) AS pg_current_wal_lsn_bytes, (case pg_is_in_recovery() when 't' then pg_wal_lsn_diff(pg_last_wal_receive_lsn(), replay_lsn)::float else pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::float end) AS pg_wal_lsn_diff FROM pg_stat_replication LEFT JOIN pg_replication_slots s ON s.active_pid = pg_stat_replication.pid 
23:53:22.539 [75] exporter@postgres app=[unknown] LOG: statement: SELECT d.datname, COALESCE( CASE WHEN d.datname = current_database() THEN s.relid::regclass::text END, s.relid::text ) AS relname, s.param1 AS phase, s.param2 AS heap_blks_total, s.param3 AS heap_blks_scanned, s.param4 AS heap_blks_vacuumed, s.param5 AS index_vacuum_count, s.param6 AS max_dead_tuples, s.param7 AS num_dead_tuples FROM pg_stat_get_progress_info('VACUUM'::text) s(pid, datid, relid, param1, param2, param3, param4, param5, param6, param7, param8, param9, param10, param11, param12, param13, param14, param15, param16, param17, param18, param19, param20) LEFT JOIN pg_database d ON s.datid = d.oid
23:53:22.540 [75] exporter@postgres app=[unknown] LOG: statement: SELECT pg_database.datname as datname, tmp.mode as mode, COALESCE(count, 0) as count FROM ( VALUES ('accesssharelock'), ('rowsharelock'), ('rowexclusivelock'), ('shareupdateexclusivelock'), ('sharelock'), ('sharerowexclusivelock'), ('exclusivelock'), ('accessexclusivelock'), ('sireadlock') ) AS tmp(mode) CROSS JOIN pg_database LEFT JOIN ( SELECT database, lower(mode) AS mode, count(*) AS count FROM pg_locks WHERE database IS NOT NULL GROUP BY database, lower(mode) ) AS tmp2 ON tmp.mode = tmp2.mode and pg_database.oid = tmp2.database ORDER BY 1 
23:53:22.542 [75] exporter@postgres app=[unknown] LOG: statement: SELECT pg_roles.rolname, pg_roles.rolconnlimit FROM pg_roles
23:53:22.542 [75] exporter@postgres app=[unknown] LOG: statement: SELECT archived_count, failed_count, extract(epoch from now() - last_archived_time) AS last_archive_age FROM pg_stat_archiver;
23:53:22.543 [75] exporter@postgres app=[unknown] LOG: statement: SELECT extract(epoch from pg_postmaster_start_time) from pg_postmaster_start_time();
23:53:22.544 [75] exporter@postgres app=[unknown] LOG: statement: SELECT current_database() datname, schemaname, relname, seq_scan, seq_tup_read, idx_scan, idx_tup_fetch, n_tup_ins, n_tup_upd, n_tup_del, n_tup_hot_upd, n_live_tup, n_dead_tup, n_mod_since_analyze, COALESCE(last_vacuum, '1970-01-01Z') as last_vacuum, COALESCE(last_autovacuum, '1970-01-01Z') as last_autovacuum, COALESCE(last_analyze, '1970-01-01Z') as last_analyze, COALESCE(last_autoanalyze, '1970-01-01Z') as last_autoanalyze, vacuum_count, autovacuum_count, analyze_count, autoanalyze_count, pg_indexes_size(relid) as indexes_size, pg_table_size(relid) as table_size FROM pg_stat_user_tables
23:53:22.547 [75] exporter@postgres app=[unknown] LOG: statement: SELECT datid,datname,numbackends,xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,conflicts,temp_files,temp_bytes,deadlocks,blk_read_time,blk_write_time,stats_reset,active_time FROM pg_stat_database;
23:53:22.550 [75] exporter@postgres app=[unknown] LOG: statement: SELECT slot_name, database, active, (case pg_is_in_recovery() when 't' then pg_wal_lsn_diff(pg_last_wal_receive_lsn(), restart_lsn) else pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) end) as pg_wal_lsn_diff FROM pg_replication_slots 
23:53:22.551 [75] exporter@postgres app=[unknown] LOG: execute <unnamed>: SELECT pg_database_size($1)
23:53:22.551 [75] exporter@postgres app=[unknown] DETAIL: parameters: $1 = 'postgres'
23:53:22.552 [75] exporter@postgres app=[unknown] LOG: statement: SELECT slot_name, slot_type, CASE WHEN pg_is_in_recovery() THEN pg_last_wal_receive_lsn() - '0/0' ELSE pg_current_wal_lsn() - '0/0' END AS current_wal_lsn, COALESCE(confirmed_flush_lsn, '0/0') - '0/0' AS confirmed_flush_lsn, active, safe_wal_size, wal_status FROM pg_replication_slots;
23:53:22.553 [75] exporter@postgres app=[unknown] LOG: execute <unnamed>: SELECT pg_database_size($1)
23:53:22.553 [75] exporter@postgres app=[unknown] DETAIL: parameters: $1 = 'template1'
23:53:22.554 [75] exporter@postgres app=[unknown] LOG: statement: SELECT name, setting, COALESCE(unit, ''), COALESCE(short_desc, ''), vartype FROM pg_settings WHERE vartype IN ('bool', 'integer', 'real') AND name NOT IN ('sync_commit_cancel_wait', 'google_dataplex.max_messages');
23:53:22.557 [75] exporter@postgres app=[unknown] LOG: execute <unnamed>: SELECT pg_database_size($1)
23:53:22.557 [75] exporter@postgres app=[unknown] DETAIL: parameters: $1 = 'template0'
23:53:22.558 [75] exporter@postgres app=[unknown] LOG: disconnection: session time: 0:00:00.068 user=exporter database=postgres host=172.20.0.5 port=41594
23:53:24.652 [74] exporter@postgres app=[unknown] LOG: statement: ;
23:53:24.653 [74] exporter@postgres app=[unknown] LOG: statement: SELECT version();
23:53:24.653 [74] exporter@postgres app=[unknown] LOG: statement: SELECT * FROM pg_stat_database_conflicts;
23:53:24.657 [76] exporter@postgres app=[unknown] LOG: connection authenticated: identity="exporter" method=scram-sha-256 (/var/lib/postgresql/data/pg_hba.conf:128)
23:53:24.657 [76] exporter@postgres app=[unknown] LOG: connection authorized: user=exporter database=postgres
23:53:24.659 [76] exporter@postgres app=[unknown] LOG: statement: SELECT version();
23:53:24.660 [76] exporter@postgres app=[unknown] LOG: statement: SELECT current_database() datname, schemaname, relname, seq_scan, seq_tup_read, idx_scan, idx_tup_fetch, n_tup_ins, n_tup_upd, n_tup_del, n_tup_hot_upd, n_live_tup, n_dead_tup, n_mod_since_analyze, COALESCE(last_vacuum, '1970-01-01Z') as last_vacuum, COALESCE(last_autovacuum, '1970-01-01Z') as last_autovacuum, COALESCE(last_analyze, '1970-01-01Z') as last_analyze, COALESCE(last_autoanalyze, '1970-01-01Z') as last_autoanalyze, vacuum_count, autovacuum_count, analyze_count, autoanalyze_count, pg_indexes_size(relid) as indexes_size, pg_table_size(relid) as table_size FROM pg_stat_user_tables
23:53:24.668 [76] exporter@postgres app=[unknown] LOG: statement: SELECT pg_database.datname, pg_database.datconnlimit FROM pg_database;
23:53:24.669 [76] exporter@postgres app=[unknown] LOG: statement: SELECT current_database() datname, schemaname, relname, heap_blks_read, heap_blks_hit, idx_blks_read, idx_blks_hit, toast_blks_read, toast_blks_hit, tidx_blks_read, tidx_blks_hit FROM pg_statio_user_tables
23:53:24.672 [76] exporter@postgres app=[unknown] LOG: statement: SELECT column_name FROM information_schema.columns WHERE table_name = 'pg_stat_wal_receiver' and column_name = 'flushed_lsn' 
23:53:24.684 [76] exporter@postgres app=[unknown] LOG: statement: SELECT pg_database.datname, tmp.state, tmp2.usename, tmp2.application_name, tmp2.backend_type, tmp2.wait_event_type, tmp2.wait_event, COALESCE(count,0) as count, COALESCE(max_tx_duration,0) as max_tx_duration FROM ( VALUES ('active'), ('idle'), ('idle in transaction'), ('idle in transaction (aborted)'), ('fastpath function call'), ('disabled') ) AS tmp(state) CROSS JOIN pg_database LEFT JOIN ( SELECT datname, state, usename, application_name, backend_type, wait_event_type, wait_event, count(*) AS count, MAX(EXTRACT(EPOCH FROM now() - xact_start))::float AS max_tx_duration FROM pg_stat_activity WHERE pid <> pg_backend_pid() GROUP BY datname,state,usename,application_name,backend_type,wait_event_type,wait_event) AS tmp2 ON tmp.state = tmp2.state AND pg_database.datname = tmp2.datname 
23:53:24.691 [76] exporter@postgres app=[unknown] LOG: statement: SELECT trim(both '''' from substring(conninfo from 'host=([^ ]*)')) as upstream_host, slot_name, status, (receive_start_lsn- '0/0') % (2^52)::bigint as receive_start_lsn, (flushed_lsn - '0/0') % (2^52)::bigint as flushed_lsn, receive_start_tli, received_tli, extract(epoch from last_msg_send_time) as last_msg_send_time, extract(epoch from last_msg_receipt_time) as last_msg_receipt_time, (latest_end_lsn - '0/0') % (2^52)::bigint as latest_end_lsn, extract(epoch from latest_end_time) as latest_end_time, substring(slot_name from 'repmgr_slot_([0-9]*)') as upstream_node FROM pg_catalog.pg_stat_wal_receiver 
23:53:24.693 [76] exporter@postgres app=[unknown] LOG: statement: SELECT application_name, client_addr::text, state, s.slot_name, (case pg_is_in_recovery() when 't' then pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_lsn('0/0'))::float else pg_wal_lsn_diff(pg_current_wal_lsn(), pg_lsn('0/0'))::float end) AS pg_current_wal_lsn_bytes, (case pg_is_in_recovery() when 't' then pg_wal_lsn_diff(pg_last_wal_receive_lsn(), replay_lsn)::float else pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::float end) AS pg_wal_lsn_diff FROM pg_stat_replication LEFT JOIN pg_replication_slots s ON s.active_pid = pg_stat_replication.pid 
23:53:24.696 [76] exporter@postgres app=[unknown] LOG: execute <unnamed>: SELECT pg_database_size($1)
23:53:24.696 [76] exporter@postgres app=[unknown] DETAIL: parameters: $1 = 'postgres'
23:53:24.697 [76] exporter@postgres app=[unknown] LOG: statement: SELECT datname, age(d.datfrozenxid) as age_datfrozenxid, mxid_age(d.datminmxid) as age_datminmxid FROM pg_catalog.pg_database d WHERE d.datallowconn 
23:53:24.698 [76] exporter@postgres app=[unknown] LOG: statement: SELECT datid,datname,numbackends,xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,conflicts,temp_files,temp_bytes,deadlocks,blk_read_time,blk_write_time,stats_reset,active_time FROM pg_stat_database;
23:53:24.700 [76] exporter@postgres app=[unknown] LOG: statement: SELECT pg_roles.rolname, pg_roles.rolconnlimit FROM pg_roles
23:53:24.701 [76] exporter@postgres app=[unknown] LOG: statement: SELECT slot_name, database, active, (case pg_is_in_recovery() when 't' then pg_wal_lsn_diff(pg_last_wal_receive_lsn(), restart_lsn) else pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) end) as pg_wal_lsn_diff FROM pg_replication_slots 
23:53:24.701 [76] exporter@postgres app=[unknown] LOG: statement: SELECT CASE WHEN NOT pg_is_in_recovery() THEN 0 WHEN pg_last_wal_receive_lsn () = pg_last_wal_replay_lsn () THEN 0 ELSE GREATEST (0, EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))) END AS lag, CASE WHEN pg_is_in_recovery() THEN 1 ELSE 0 END as is_replica, GREATEST (0, EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))) as last_replay
23:53:24.702 [76] exporter@postgres app=[unknown] LOG: statement: SELECT extract(epoch from pg_postmaster_start_time) from pg_postmaster_start_time();
23:53:24.703 [76] exporter@postgres app=[unknown] LOG: statement: SELECT checkpoints_timed ,checkpoints_req ,checkpoint_write_time ,checkpoint_sync_time ,buffers_checkpoint ,buffers_clean ,maxwritten_clean ,buffers_backend ,buffers_backend_fsync ,buffers_alloc ,stats_reset FROM pg_stat_bgwriter;
23:53:24.704 [76] exporter@postgres app=[unknown] LOG: statement: SELECT COUNT(*) AS segments, SUM(size) AS size FROM pg_ls_waldir() WHERE name ~ '^[0-9A-F]{24}$'
23:53:24.705 [76] exporter@postgres app=[unknown] LOG: statement: SELECT pg_database.datname as datname, tmp.mode as mode, COALESCE(count, 0) as count FROM ( VALUES ('accesssharelock'), ('rowsharelock'), ('rowexclusivelock'), ('shareupdateexclusivelock'), ('sharelock'), ('sharerowexclusivelock'), ('exclusivelock'), ('accessexclusivelock'), ('sireadlock') ) AS tmp(mode) CROSS JOIN pg_database LEFT JOIN ( SELECT database, lower(mode) AS mode, count(*) AS count FROM pg_locks WHERE database IS NOT NULL GROUP BY database, lower(mode) ) AS tmp2 ON tmp.mode = tmp2.mode and pg_database.oid = tmp2.database ORDER BY 1 
23:53:24.706 [76] exporter@postgres app=[unknown] LOG: statement: SELECT archived_count, failed_count, extract(epoch from now() - last_archived_time) AS last_archive_age FROM pg_stat_archiver;
23:53:24.707 [76] exporter@postgres app=[unknown] LOG: statement: SELECT slot_name, slot_type, CASE WHEN pg_is_in_recovery() THEN pg_last_wal_receive_lsn() - '0/0' ELSE pg_current_wal_lsn() - '0/0' END AS current_wal_lsn, COALESCE(confirmed_flush_lsn, '0/0') - '0/0' AS confirmed_flush_lsn, active, safe_wal_size, wal_status FROM pg_replication_slots;
23:53:24.708 [76] exporter@postgres app=[unknown] LOG: statement: SELECT COUNT(*) as transactions, MAX(EXTRACT(EPOCH FROM clock_timestamp() - pg_stat_activity.xact_start)) AS oldest_timestamp_seconds FROM pg_catalog.pg_stat_activity WHERE state IS DISTINCT FROM 'idle' AND query NOT LIKE 'autovacuum:%' AND pg_stat_activity.xact_start IS NOT NULL AND pid <> pg_backend_pid(); 
23:53:24.709 [76] exporter@postgres app=[unknown] LOG: execute <unnamed>: SELECT pg_database_size($1)
23:53:24.709 [76] exporter@postgres app=[unknown] DETAIL: parameters: $1 = 'template1'
23:53:24.711 [76] exporter@postgres app=[unknown] LOG: statement: SELECT d.datname, COALESCE( CASE WHEN d.datname = current_database() THEN s.relid::regclass::text END, s.relid::text ) AS relname, s.param1 AS phase, s.param2 AS heap_blks_total, s.param3 AS heap_blks_scanned, s.param4 AS heap_blks_vacuumed, s.param5 AS index_vacuum_count, s.param6 AS max_dead_tuples, s.param7 AS num_dead_tuples FROM pg_stat_get_progress_info('VACUUM'::text) s(pid, datid, relid, param1, param2, param3, param4, param5, param6, param7, param8, param9, param10, param11, param12, param13, param14, param15, param16, param17, param18, param19, param20) LEFT JOIN pg_database d ON s.datid = d.oid
23:53:24.712 [76] exporter@postgres app=[unknown] LOG: statement: SELECT name, setting, COALESCE(unit, ''), COALESCE(short_desc, ''), vartype FROM pg_settings WHERE vartype IN ('bool', 'integer', 'real') AND name NOT IN ('sync_commit_cancel_wait', 'google_dataplex.max_messages');
23:53:24.716 [76] exporter@postgres app=[unknown] LOG: execute <unnamed>: SELECT pg_database_size($1)
23:53:24.716 [76] exporter@postgres app=[unknown] DETAIL: parameters: $1 = 'template0'
23:53:24.718 [76] exporter@postgres app=[unknown] LOG: disconnection: session time: 0:00:00.063 user=exporter database=postgres host=172.20.0.5 port=41600
23:53:27.004 [83] postgres@postgres app=[unknown] LOG: connection authorized: user=postgres database=postgres application_name=psql
```

## p17：PostgreSQL 17.11 on x86_64-pc-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit

```text
23:53:22.576 [69] exporter@postgres app=[unknown] LOG: connection authenticated: identity="exporter" method=scram-sha-256 (/var/lib/postgresql/data/pg_hba.conf:128)
23:53:22.576 [69] exporter@postgres app=[unknown] LOG: connection authorized: user=exporter database=postgres
23:53:22.577 [70] exporter@postgres app=[unknown] LOG: connection authenticated: identity="exporter" method=scram-sha-256 (/var/lib/postgresql/data/pg_hba.conf:128)
23:53:22.577 [70] exporter@postgres app=[unknown] LOG: connection authorized: user=exporter database=postgres
23:53:22.579 [69] exporter@postgres app=[unknown] LOG: statement: SELECT version();
23:53:22.579 [70] exporter@postgres app=[unknown] LOG: statement: ;
23:53:22.579 [70] exporter@postgres app=[unknown] LOG: statement: SELECT version();
23:53:22.580 [69] exporter@postgres app=[unknown] LOG: statement: SELECT pg_database.datname, tmp.state, tmp2.usename, tmp2.application_name, tmp2.backend_type, tmp2.wait_event_type, tmp2.wait_event, COALESCE(count,0) as count, COALESCE(max_tx_duration,0) as max_tx_duration FROM ( VALUES ('active'), ('idle'), ('idle in transaction'), ('idle in transaction (aborted)'), ('fastpath function call'), ('disabled') ) AS tmp(state) CROSS JOIN pg_database LEFT JOIN ( SELECT datname, state, usename, application_name, backend_type, wait_event_type, wait_event, count(*) AS count, MAX(EXTRACT(EPOCH FROM now() - xact_start))::float AS max_tx_duration FROM pg_stat_activity WHERE pid <> pg_backend_pid() GROUP BY datname,state,usename,application_name,backend_type,wait_event_type,wait_event) AS tmp2 ON tmp.state = tmp2.state AND pg_database.datname = tmp2.datname 
23:53:22.580 [70] exporter@postgres app=[unknown] LOG: statement: SELECT * FROM pg_stat_database_conflicts;
23:53:22.589 [69] exporter@postgres app=[unknown] LOG: statement: SELECT num_timed ,num_requested ,restartpoints_timed ,restartpoints_req ,restartpoints_done ,write_time ,sync_time ,buffers_written ,stats_reset FROM pg_stat_checkpointer;
23:53:22.590 [69] exporter@postgres app=[unknown] LOG: statement: SELECT pg_roles.rolname, pg_roles.rolconnlimit FROM pg_roles
23:53:22.591 [69] exporter@postgres app=[unknown] LOG: statement: SELECT current_database() datname, schemaname, relname, heap_blks_read, heap_blks_hit, idx_blks_read, idx_blks_hit, toast_blks_read, toast_blks_hit, tidx_blks_read, tidx_blks_hit FROM pg_statio_user_tables
23:53:22.597 [69] exporter@postgres app=[unknown] LOG: statement: SELECT pg_database.datname, pg_database.datconnlimit FROM pg_database;
23:53:22.598 [69] exporter@postgres app=[unknown] LOG: statement: SELECT buffers_clean ,maxwritten_clean ,buffers_alloc ,stats_reset FROM pg_stat_bgwriter;
23:53:22.598 [69] exporter@postgres app=[unknown] LOG: statement: SELECT archived_count, failed_count, extract(epoch from now() - last_archived_time) AS last_archive_age FROM pg_stat_archiver;
23:53:22.600 [69] exporter@postgres app=[unknown] LOG: statement: SELECT slot_name, database, active, (case pg_is_in_recovery() when 't' then pg_wal_lsn_diff(pg_last_wal_receive_lsn(), restart_lsn) else pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) end) as pg_wal_lsn_diff FROM pg_replication_slots 
23:53:22.601 [69] exporter@postgres app=[unknown] LOG: statement: SELECT column_name FROM information_schema.columns WHERE table_name = 'pg_stat_wal_receiver' and column_name = 'flushed_lsn' 
23:53:22.614 [69] exporter@postgres app=[unknown] LOG: statement: SELECT extract(epoch from pg_postmaster_start_time) from pg_postmaster_start_time();
23:53:22.614 [69] exporter@postgres app=[unknown] LOG: statement: SELECT current_database() datname, schemaname, relname, seq_scan, seq_tup_read, idx_scan, idx_tup_fetch, n_tup_ins, n_tup_upd, n_tup_del, n_tup_hot_upd, n_live_tup, n_dead_tup, n_mod_since_analyze, COALESCE(last_vacuum, '1970-01-01Z') as last_vacuum, COALESCE(last_autovacuum, '1970-01-01Z') as last_autovacuum, COALESCE(last_analyze, '1970-01-01Z') as last_analyze, COALESCE(last_autoanalyze, '1970-01-01Z') as last_autoanalyze, vacuum_count, autovacuum_count, analyze_count, autoanalyze_count, pg_indexes_size(relid) as indexes_size, pg_table_size(relid) as table_size FROM pg_stat_user_tables
23:53:22.618 [69] exporter@postgres app=[unknown] LOG: statement: SELECT slot_name, slot_type, CASE WHEN pg_is_in_recovery() THEN pg_last_wal_receive_lsn() - '0/0' ELSE pg_current_wal_lsn() - '0/0' END AS current_wal_lsn, COALESCE(confirmed_flush_lsn, '0/0') - '0/0' AS confirmed_flush_lsn, active, safe_wal_size, wal_status FROM pg_replication_slots;
23:53:22.618 [69] exporter@postgres app=[unknown] LOG: statement: SELECT trim(both '''' from substring(conninfo from 'host=([^ ]*)')) as upstream_host, slot_name, status, (receive_start_lsn- '0/0') % (2^52)::bigint as receive_start_lsn, (flushed_lsn - '0/0') % (2^52)::bigint as flushed_lsn, receive_start_tli, received_tli, extract(epoch from last_msg_send_time) as last_msg_send_time, extract(epoch from last_msg_receipt_time) as last_msg_receipt_time, (latest_end_lsn - '0/0') % (2^52)::bigint as latest_end_lsn, extract(epoch from latest_end_time) as latest_end_time, substring(slot_name from 'repmgr_slot_([0-9]*)') as upstream_node FROM pg_catalog.pg_stat_wal_receiver 
23:53:22.620 [69] exporter@postgres app=[unknown] LOG: statement: SELECT d.datname, COALESCE( CASE WHEN d.datname = current_database() THEN s.relid::regclass::text END, s.relid::text ) AS relname, s.param1 AS phase, s.param2 AS heap_blks_total, s.param3 AS heap_blks_scanned, s.param4 AS heap_blks_vacuumed, s.param5 AS index_vacuum_count, s.param6 AS max_dead_tuples, s.param7 AS num_dead_tuples FROM pg_stat_get_progress_info('VACUUM'::text) s(pid, datid, relid, param1, param2, param3, param4, param5, param6, param7, param8, param9, param10, param11, param12, param13, param14, param15, param16, param17, param18, param19, param20) LEFT JOIN pg_database d ON s.datid = d.oid
23:53:22.621 [69] exporter@postgres app=[unknown] LOG: statement: SELECT pg_database.datname as datname, tmp.mode as mode, COALESCE(count, 0) as count FROM ( VALUES ('accesssharelock'), ('rowsharelock'), ('rowexclusivelock'), ('shareupdateexclusivelock'), ('sharelock'), ('sharerowexclusivelock'), ('exclusivelock'), ('accessexclusivelock'), ('sireadlock') ) AS tmp(mode) CROSS JOIN pg_database LEFT JOIN ( SELECT database, lower(mode) AS mode, count(*) AS count FROM pg_locks WHERE database IS NOT NULL GROUP BY database, lower(mode) ) AS tmp2 ON tmp.mode = tmp2.mode and pg_database.oid = tmp2.database ORDER BY 1 
23:53:22.622 [69] exporter@postgres app=[unknown] LOG: statement: SELECT COUNT(*) as transactions, MAX(EXTRACT(EPOCH FROM clock_timestamp() - pg_stat_activity.xact_start)) AS oldest_timestamp_seconds FROM pg_catalog.pg_stat_activity WHERE state IS DISTINCT FROM 'idle' AND query NOT LIKE 'autovacuum:%' AND pg_stat_activity.xact_start IS NOT NULL AND pid <> pg_backend_pid(); 
23:53:22.624 [69] exporter@postgres app=[unknown] LOG: statement: SELECT CASE WHEN NOT pg_is_in_recovery() THEN 0 WHEN pg_last_wal_receive_lsn () = pg_last_wal_replay_lsn () THEN 0 ELSE GREATEST (0, EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))) END AS lag, CASE WHEN pg_is_in_recovery() THEN 1 ELSE 0 END as is_replica, GREATEST (0, EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))) as last_replay
23:53:22.625 [69] exporter@postgres app=[unknown] LOG: execute <unnamed>: SELECT pg_database_size($1)
23:53:22.625 [69] exporter@postgres app=[unknown] DETAIL: Parameters: $1 = 'postgres'
23:53:22.628 [69] exporter@postgres app=[unknown] LOG: statement: SELECT COUNT(*) AS segments, SUM(size) AS size FROM pg_ls_waldir() WHERE name ~ '^[0-9A-F]{24}$'
23:53:22.629 [69] exporter@postgres app=[unknown] LOG: execute <unnamed>: SELECT pg_database_size($1)
23:53:22.629 [69] exporter@postgres app=[unknown] DETAIL: Parameters: $1 = 'template1'
23:53:22.631 [69] exporter@postgres app=[unknown] LOG: statement: SELECT datid,datname,numbackends,xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,conflicts,temp_files,temp_bytes,deadlocks,blk_read_time,blk_write_time,stats_reset,active_time FROM pg_stat_database;
23:53:22.632 [69] exporter@postgres app=[unknown] LOG: statement: SELECT name, setting, COALESCE(unit, ''), COALESCE(short_desc, ''), vartype FROM pg_settings WHERE vartype IN ('bool', 'integer', 'real') AND name NOT IN ('sync_commit_cancel_wait', 'google_dataplex.max_messages');
23:53:22.635 [69] exporter@postgres app=[unknown] LOG: execute <unnamed>: SELECT pg_database_size($1)
23:53:22.635 [69] exporter@postgres app=[unknown] DETAIL: Parameters: $1 = 'template0'
23:53:22.636 [69] exporter@postgres app=[unknown] LOG: statement: SELECT datname, age(d.datfrozenxid) as age_datfrozenxid, mxid_age(d.datminmxid) as age_datminmxid FROM pg_catalog.pg_database d WHERE d.datallowconn 
23:53:22.637 [69] exporter@postgres app=[unknown] LOG: statement: SELECT application_name, client_addr::text, state, s.slot_name, (case pg_is_in_recovery() when 't' then pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_lsn('0/0'))::float else pg_wal_lsn_diff(pg_current_wal_lsn(), pg_lsn('0/0'))::float end) AS pg_current_wal_lsn_bytes, (case pg_is_in_recovery() when 't' then pg_wal_lsn_diff(pg_last_wal_receive_lsn(), replay_lsn)::float else pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::float end) AS pg_wal_lsn_diff FROM pg_stat_replication LEFT JOIN pg_replication_slots s ON s.active_pid = pg_stat_replication.pid 
23:53:22.639 [69] exporter@postgres app=[unknown] LOG: disconnection: session time: 0:00:00.065 user=exporter database=postgres host=172.20.0.4 port=54352
23:53:24.728 [70] exporter@postgres app=[unknown] LOG: statement: ;
23:53:24.728 [70] exporter@postgres app=[unknown] LOG: statement: SELECT version();
23:53:24.729 [70] exporter@postgres app=[unknown] LOG: statement: SELECT * FROM pg_stat_database_conflicts;
23:53:24.732 [71] exporter@postgres app=[unknown] LOG: connection authenticated: identity="exporter" method=scram-sha-256 (/var/lib/postgresql/data/pg_hba.conf:128)
23:53:24.732 [71] exporter@postgres app=[unknown] LOG: connection authorized: user=exporter database=postgres
23:53:24.734 [71] exporter@postgres app=[unknown] LOG: statement: SELECT version();
23:53:24.735 [71] exporter@postgres app=[unknown] LOG: statement: SELECT name, setting, COALESCE(unit, ''), COALESCE(short_desc, ''), vartype FROM pg_settings WHERE vartype IN ('bool', 'integer', 'real') AND name NOT IN ('sync_commit_cancel_wait', 'google_dataplex.max_messages');
23:53:24.740 [71] exporter@postgres app=[unknown] LOG: statement: SELECT buffers_clean ,maxwritten_clean ,buffers_alloc ,stats_reset FROM pg_stat_bgwriter;
23:53:24.742 [71] exporter@postgres app=[unknown] LOG: statement: SELECT pg_roles.rolname, pg_roles.rolconnlimit FROM pg_roles
23:53:24.745 [71] exporter@postgres app=[unknown] LOG: statement: SELECT archived_count, failed_count, extract(epoch from now() - last_archived_time) AS last_archive_age FROM pg_stat_archiver;
23:53:24.747 [71] exporter@postgres app=[unknown] LOG: statement: SELECT COUNT(*) as transactions, MAX(EXTRACT(EPOCH FROM clock_timestamp() - pg_stat_activity.xact_start)) AS oldest_timestamp_seconds FROM pg_catalog.pg_stat_activity WHERE state IS DISTINCT FROM 'idle' AND query NOT LIKE 'autovacuum:%' AND pg_stat_activity.xact_start IS NOT NULL AND pid <> pg_backend_pid(); 
23:53:24.749 [71] exporter@postgres app=[unknown] LOG: statement: SELECT COUNT(*) AS segments, SUM(size) AS size FROM pg_ls_waldir() WHERE name ~ '^[0-9A-F]{24}$'
23:53:24.750 [71] exporter@postgres app=[unknown] LOG: statement: SELECT num_timed ,num_requested ,restartpoints_timed ,restartpoints_req ,restartpoints_done ,write_time ,sync_time ,buffers_written ,stats_reset FROM pg_stat_checkpointer;
23:53:24.751 [71] exporter@postgres app=[unknown] LOG: statement: SELECT current_database() datname, schemaname, relname, seq_scan, seq_tup_read, idx_scan, idx_tup_fetch, n_tup_ins, n_tup_upd, n_tup_del, n_tup_hot_upd, n_live_tup, n_dead_tup, n_mod_since_analyze, COALESCE(last_vacuum, '1970-01-01Z') as last_vacuum, COALESCE(last_autovacuum, '1970-01-01Z') as last_autovacuum, COALESCE(last_analyze, '1970-01-01Z') as last_analyze, COALESCE(last_autoanalyze, '1970-01-01Z') as last_autoanalyze, vacuum_count, autovacuum_count, analyze_count, autoanalyze_count, pg_indexes_size(relid) as indexes_size, pg_table_size(relid) as table_size FROM pg_stat_user_tables
23:53:24.760 [71] exporter@postgres app=[unknown] LOG: statement: SELECT pg_database.datname, pg_database.datconnlimit FROM pg_database;
23:53:24.761 [71] exporter@postgres app=[unknown] LOG: statement: SELECT d.datname, COALESCE( CASE WHEN d.datname = current_database() THEN s.relid::regclass::text END, s.relid::text ) AS relname, s.param1 AS phase, s.param2 AS heap_blks_total, s.param3 AS heap_blks_scanned, s.param4 AS heap_blks_vacuumed, s.param5 AS index_vacuum_count, s.param6 AS max_dead_tuples, s.param7 AS num_dead_tuples FROM pg_stat_get_progress_info('VACUUM'::text) s(pid, datid, relid, param1, param2, param3, param4, param5, param6, param7, param8, param9, param10, param11, param12, param13, param14, param15, param16, param17, param18, param19, param20) LEFT JOIN pg_database d ON s.datid = d.oid
23:53:24.762 [71] exporter@postgres app=[unknown] LOG: statement: SELECT current_database() datname, schemaname, relname, heap_blks_read, heap_blks_hit, idx_blks_read, idx_blks_hit, toast_blks_read, toast_blks_hit, tidx_blks_read, tidx_blks_hit FROM pg_statio_user_tables
23:53:24.765 [71] exporter@postgres app=[unknown] LOG: statement: SELECT pg_database.datname as datname, tmp.mode as mode, COALESCE(count, 0) as count FROM ( VALUES ('accesssharelock'), ('rowsharelock'), ('rowexclusivelock'), ('shareupdateexclusivelock'), ('sharelock'), ('sharerowexclusivelock'), ('exclusivelock'), ('accessexclusivelock'), ('sireadlock') ) AS tmp(mode) CROSS JOIN pg_database LEFT JOIN ( SELECT database, lower(mode) AS mode, count(*) AS count FROM pg_locks WHERE database IS NOT NULL GROUP BY database, lower(mode) ) AS tmp2 ON tmp.mode = tmp2.mode and pg_database.oid = tmp2.database ORDER BY 1 
23:53:24.768 [71] exporter@postgres app=[unknown] LOG: statement: SELECT pg_database.datname, tmp.state, tmp2.usename, tmp2.application_name, tmp2.backend_type, tmp2.wait_event_type, tmp2.wait_event, COALESCE(count,0) as count, COALESCE(max_tx_duration,0) as max_tx_duration FROM ( VALUES ('active'), ('idle'), ('idle in transaction'), ('idle in transaction (aborted)'), ('fastpath function call'), ('disabled') ) AS tmp(state) CROSS JOIN pg_database LEFT JOIN ( SELECT datname, state, usename, application_name, backend_type, wait_event_type, wait_event, count(*) AS count, MAX(EXTRACT(EPOCH FROM now() - xact_start))::float AS max_tx_duration FROM pg_stat_activity WHERE pid <> pg_backend_pid() GROUP BY datname,state,usename,application_name,backend_type,wait_event_type,wait_event) AS tmp2 ON tmp.state = tmp2.state AND pg_database.datname = tmp2.datname 
23:53:24.770 [71] exporter@postgres app=[unknown] LOG: statement: SELECT CASE WHEN NOT pg_is_in_recovery() THEN 0 WHEN pg_last_wal_receive_lsn () = pg_last_wal_replay_lsn () THEN 0 ELSE GREATEST (0, EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))) END AS lag, CASE WHEN pg_is_in_recovery() THEN 1 ELSE 0 END as is_replica, GREATEST (0, EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))) as last_replay
23:53:24.771 [71] exporter@postgres app=[unknown] LOG: statement: SELECT column_name FROM information_schema.columns WHERE table_name = 'pg_stat_wal_receiver' and column_name = 'flushed_lsn' 
23:53:24.785 [71] exporter@postgres app=[unknown] LOG: statement: SELECT application_name, client_addr::text, state, s.slot_name, (case pg_is_in_recovery() when 't' then pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_lsn('0/0'))::float else pg_wal_lsn_diff(pg_current_wal_lsn(), pg_lsn('0/0'))::float end) AS pg_current_wal_lsn_bytes, (case pg_is_in_recovery() when 't' then pg_wal_lsn_diff(pg_last_wal_receive_lsn(), replay_lsn)::float else pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::float end) AS pg_wal_lsn_diff FROM pg_stat_replication LEFT JOIN pg_replication_slots s ON s.active_pid = pg_stat_replication.pid 
23:53:24.789 [71] exporter@postgres app=[unknown] LOG: statement: SELECT trim(both '''' from substring(conninfo from 'host=([^ ]*)')) as upstream_host, slot_name, status, (receive_start_lsn- '0/0') % (2^52)::bigint as receive_start_lsn, (flushed_lsn - '0/0') % (2^52)::bigint as flushed_lsn, receive_start_tli, received_tli, extract(epoch from last_msg_send_time) as last_msg_send_time, extract(epoch from last_msg_receipt_time) as last_msg_receipt_time, (latest_end_lsn - '0/0') % (2^52)::bigint as latest_end_lsn, extract(epoch from latest_end_time) as latest_end_time, substring(slot_name from 'repmgr_slot_([0-9]*)') as upstream_node FROM pg_catalog.pg_stat_wal_receiver 
23:53:24.791 [71] exporter@postgres app=[unknown] LOG: execute <unnamed>: SELECT pg_database_size($1)
23:53:24.791 [71] exporter@postgres app=[unknown] DETAIL: Parameters: $1 = 'postgres'
23:53:24.792 [71] exporter@postgres app=[unknown] LOG: statement: SELECT extract(epoch from pg_postmaster_start_time) from pg_postmaster_start_time();
23:53:24.793 [71] exporter@postgres app=[unknown] LOG: statement: SELECT datname, age(d.datfrozenxid) as age_datfrozenxid, mxid_age(d.datminmxid) as age_datminmxid FROM pg_catalog.pg_database d WHERE d.datallowconn 
23:53:24.793 [71] exporter@postgres app=[unknown] LOG: execute <unnamed>: SELECT pg_database_size($1)
23:53:24.793 [71] exporter@postgres app=[unknown] DETAIL: Parameters: $1 = 'template1'
23:53:24.795 [71] exporter@postgres app=[unknown] LOG: statement: SELECT slot_name, database, active, (case pg_is_in_recovery() when 't' then pg_wal_lsn_diff(pg_last_wal_receive_lsn(), restart_lsn) else pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) end) as pg_wal_lsn_diff FROM pg_replication_slots 
23:53:24.796 [71] exporter@postgres app=[unknown] LOG: execute <unnamed>: SELECT pg_database_size($1)
23:53:24.796 [71] exporter@postgres app=[unknown] DETAIL: Parameters: $1 = 'template0'
23:53:24.797 [71] exporter@postgres app=[unknown] LOG: statement: SELECT datid,datname,numbackends,xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,conflicts,temp_files,temp_bytes,deadlocks,blk_read_time,blk_write_time,stats_reset,active_time FROM pg_stat_database;
23:53:24.799 [71] exporter@postgres app=[unknown] LOG: statement: SELECT slot_name, slot_type, CASE WHEN pg_is_in_recovery() THEN pg_last_wal_receive_lsn() - '0/0' ELSE pg_current_wal_lsn() - '0/0' END AS current_wal_lsn, COALESCE(confirmed_flush_lsn, '0/0') - '0/0' AS confirmed_flush_lsn, active, safe_wal_size, wal_status FROM pg_replication_slots;
23:53:24.800 [71] exporter@postgres app=[unknown] LOG: disconnection: session time: 0:00:00.070 user=exporter database=postgres host=172.20.0.4 port=54366
23:53:27.385 [78] postgres@postgres app=[unknown] LOG: connection authorized: user=postgres database=postgres application_name=psql
```

## exporter log（collector 失敗或版本跳過）

```text
      2 level=WARN source=main.go:81 msg="Error loading config" err="error opening config file \"postgres_exporter.yml\": open postgres_exporter.yml: no such file or directory"
      2 level=WARN source=pg_stat_checkpointer.go:115 msg="pg_stat_checkpointer collector is not available on PostgreSQL < 17.0.0, skipping" collector=stat_checkpointer
```

## 對應的 metric（第二次 scrape，節錄）

### 127.0.0.1:23501

```text
pg_database_wraparound_age_datfrozenxid_seconds{datname="postgres"} 10
pg_database_wraparound_age_datfrozenxid_seconds{datname="template1"} 10
pg_locks_count{datname="postgres",mode="accessexclusivelock"} 0
pg_locks_count{datname="postgres",mode="sharelock"} 0
pg_long_running_transactions 0
pg_long_running_transactions_oldest_timestamp_seconds 0
pg_postmaster_start_time_seconds 1.794181998653956e+09
pg_replication_is_replica 0
pg_replication_lag_seconds 0
pg_replication_last_replay_seconds 0
pg_replication_slots_active{database="",slot_name="trace_slot"} 0
pg_replication_slots_pg_wal_lsn_diff{database="",slot_name="trace_slot"} 4832
pg_replication_slots_slot_current_wal_lsn{slot_name="trace_slot",slot_type="physical"} 2.1988328e+07
pg_replication_slots_slot_is_active{slot_name="trace_slot",slot_type="physical"} 0
pg_replication_slots_wal_status{slot_name="trace_slot",slot_type="physical",wal_status="reserved"} 1
pg_scrape_collector_success{collector="database"} 1
pg_scrape_collector_success{collector="database_wraparound"} 1
pg_scrape_collector_success{collector="locks"} 1
pg_scrape_collector_success{collector="long_running_transactions"} 1
pg_scrape_collector_success{collector="postmaster"} 1
pg_scrape_collector_success{collector="replication"} 1
pg_scrape_collector_success{collector="replication_slots"} 1
pg_scrape_collector_success{collector="roles"} 1
pg_scrape_collector_success{collector="settings"} 1
pg_scrape_collector_success{collector="stat_activity"} 1
pg_scrape_collector_success{collector="stat_archiver"} 1
pg_scrape_collector_success{collector="stat_bgwriter"} 1
pg_scrape_collector_success{collector="stat_checkpointer"} 1
pg_scrape_collector_success{collector="stat_database"} 1
pg_scrape_collector_success{collector="stat_progress_vacuum"} 1
pg_scrape_collector_success{collector="stat_replication"} 1
pg_scrape_collector_success{collector="stat_user_tables"} 1
pg_scrape_collector_success{collector="stat_wal_receiver"} 1
pg_scrape_collector_success{collector="statio_user_tables"} 1
pg_scrape_collector_success{collector="wal"} 1
pg_settings_max_connections 100
pg_settings_max_slot_wal_keep_size_bytes -1
pg_settings_reserved_connections 0
pg_settings_superuser_reserved_connections 3
pg_stat_activity_count{application_name="",backend_type="",datname="postgres",state="active",usename="",wait_event="",wait_event_type=""} 0
pg_stat_activity_max_tx_duration{application_name="",backend_type="",datname="postgres",state="active",usename="",wait_event="",wait_event_type=""} 0
pg_stat_archiver_archived_count 0
pg_stat_archiver_failed_count 0
pg_stat_bgwriter_checkpoints_req_total 1
pg_stat_database_deadlocks{datid="5",datname="postgres"} 0
pg_stat_database_stats_reset{datid="5",datname="postgres"} 0
pg_stat_database_temp_bytes{datid="5",datname="postgres"} 0
pg_stat_database_xact_commit{datid="5",datname="postgres"} 43
pg_up 1
pg_wal_size_bytes 1.6777216e+07
```

### 127.0.0.1:23502

```text
pg_database_wraparound_age_datfrozenxid_seconds{datname="postgres"} 10
pg_database_wraparound_age_datfrozenxid_seconds{datname="template1"} 10
pg_locks_count{datname="postgres",mode="accessexclusivelock"} 0
pg_locks_count{datname="postgres",mode="sharelock"} 0
pg_long_running_transactions 0
pg_long_running_transactions_oldest_timestamp_seconds 0
pg_postmaster_start_time_seconds 1.794181998609864e+09
pg_replication_is_replica 0
pg_replication_lag_seconds 0
pg_replication_last_replay_seconds 0
pg_replication_slots_active{database="",slot_name="trace_slot"} 0
pg_replication_slots_pg_wal_lsn_diff{database="",slot_name="trace_slot"} 21464
pg_replication_slots_slot_current_wal_lsn{slot_name="trace_slot",slot_type="physical"} 2.1992184e+07
pg_replication_slots_slot_is_active{slot_name="trace_slot",slot_type="physical"} 0
pg_replication_slots_wal_status{slot_name="trace_slot",slot_type="physical",wal_status="reserved"} 1
pg_scrape_collector_success{collector="database"} 1
pg_scrape_collector_success{collector="database_wraparound"} 1
pg_scrape_collector_success{collector="locks"} 1
pg_scrape_collector_success{collector="long_running_transactions"} 1
pg_scrape_collector_success{collector="postmaster"} 1
pg_scrape_collector_success{collector="replication"} 1
pg_scrape_collector_success{collector="replication_slots"} 1
pg_scrape_collector_success{collector="roles"} 1
pg_scrape_collector_success{collector="settings"} 1
pg_scrape_collector_success{collector="stat_activity"} 1
pg_scrape_collector_success{collector="stat_archiver"} 1
pg_scrape_collector_success{collector="stat_bgwriter"} 1
pg_scrape_collector_success{collector="stat_checkpointer"} 1
pg_scrape_collector_success{collector="stat_database"} 1
pg_scrape_collector_success{collector="stat_progress_vacuum"} 1
pg_scrape_collector_success{collector="stat_replication"} 1
pg_scrape_collector_success{collector="stat_user_tables"} 1
pg_scrape_collector_success{collector="stat_wal_receiver"} 1
pg_scrape_collector_success{collector="statio_user_tables"} 1
pg_scrape_collector_success{collector="wal"} 1
pg_settings_max_connections 100
pg_settings_max_slot_wal_keep_size_bytes -1
pg_settings_reserved_connections 0
pg_settings_superuser_reserved_connections 3
pg_stat_activity_count{application_name="",backend_type="",datname="postgres",state="active",usename="",wait_event="",wait_event_type=""} 0
pg_stat_activity_max_tx_duration{application_name="",backend_type="",datname="postgres",state="active",usename="",wait_event="",wait_event_type=""} 0
pg_stat_archiver_archived_count 0
pg_stat_archiver_failed_count 0
pg_stat_checkpointer_num_requested_total 1
pg_stat_database_deadlocks{datid="5",datname="postgres"} 0
pg_stat_database_stats_reset{datid="5",datname="postgres"} 0
pg_stat_database_temp_bytes{datid="5",datname="postgres"} 0
pg_stat_database_xact_commit{datid="5",datname="postgres"} 44
pg_up 1
pg_wal_size_bytes 1.6777216e+07
```

## 沒有 datname 的那一列（pg_stat_database 的 datid=0）

- 127.0.0.1:23501：pg_stat_database_xact_commit 的 datid="0" series 數 = 0；直接查 pg_stat_database 的列數 = 4、datname 是 NULL 的列數 = 1
- 127.0.0.1:23502：pg_stat_database_xact_commit 的 datid="0" series 數 = 0；直接查 pg_stat_database 的列數 = 4、datname 是 NULL 的列數 = 1
