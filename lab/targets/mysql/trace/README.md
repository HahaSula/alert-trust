# mysqld_exporter 實際送出的 SQL

輕量檢查：一台 MariaDB 11.4、一台 MySQL 8.4.7（都不是 replica），各接一台 mysqld_exporter v0.21.0，開 `general_log`（`log_output=TABLE`），做一次 scrape，讀出 exporter 帳號送出的每一條 SQL。

MySQL 的 `general_log` 不記語法錯誤的語句，所以 slave_status 在 MySQL 8.4 上先失敗的那幾次，改用 `performance_schema.events_errors_summary_by_user_by_error` 的 ER_PARSE_ERROR（1064）次數確認。

```bash
./run.sh     # 約 2 分鐘；compose project at-mytrace，只開 127.0.0.1:22501–22502，結束時 docker compose down -v
```

結果：`results/general-log-YYYYMMDD.md`。

- 用 `docs/targets/mysql-appendix.md` 的「Exporter 實際送出的 SQL」一節。
- heartbeat 表用 pt-heartbeat 的欄位格式（`ts VARCHAR(26)` 存 ISO 時間、`server_id`），只寫入一次，用來確認 exporter 的 `UNIX_TIMESTAMP(ts)` 能解析這個格式。
