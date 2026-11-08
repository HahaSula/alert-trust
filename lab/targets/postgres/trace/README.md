# postgres_exporter 實際送出的 SQL

輕量檢查：PostgreSQL 16.15 與 17.11 各一台（都不是 replica），各接一台 postgres_exporter v0.20.1（帳號有 pg_monitor），開 `log_statement=all` 與 `log_connections`，連續做兩次 scrape，讀出 exporter 帳號送出的每一條 SQL 與它用的連線。

```bash
./run.sh     # 約 1 分鐘；compose project at-pgtrace，只開 127.0.0.1:23501–23502，結束時 docker compose down -v
```

結果：`results/statements-YYYYMMDD.md`。

- 用在 `docs/targets/postgres-appendix.md` 的「Exporter 實際送出的 SQL」一節。
- 除了預設 collector，另開 stat_wal_receiver、database_wraparound、postmaster、stat_checkpointer、long_running_transactions。
- `init.sql` 建一個保留 WAL、沒有人用的 slot `trace_slot`，讓 replication_slots 有一列；`max_slot_wal_keep_size` 是預設的 -1，所以沒有 `safe_wal_size_bytes`。
