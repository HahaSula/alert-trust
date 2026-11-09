-- MariaDB ASH 取樣器原型：每秒一次，把「非 Sleep 的 session」與它當下的語句 digest、等待事件存進 ash.sample。
-- 需要 performance_schema=ON 與 event_scheduler=ON；沒開 performance_schema 時 digest、wait_event 是 NULL，只剩 PROCESSLIST 欄位。
CREATE DATABASE IF NOT EXISTS ash;
CREATE TABLE IF NOT EXISTS ash.sample (
  ts          DATETIME(3)  NOT NULL,
  conn_id     BIGINT       NOT NULL,
  user        VARCHAR(80),
  db          VARCHAR(64),
  command     VARCHAR(16),
  state       VARCHAR(64),
  time_ms     DECIMAL(16,3),
  digest      VARCHAR(64),
  digest_text VARCHAR(160),
  sql_head    VARCHAR(80),
  wait_event  VARCHAR(128),
  trx_state   VARCHAR(16),
  KEY (ts)
) ENGINE=InnoDB;

DELIMITER //
CREATE OR REPLACE PROCEDURE ash.take_sample()
  SQL SECURITY DEFINER
BEGIN
  INSERT INTO ash.sample (ts, conn_id, user, db, command, state, time_ms, digest, digest_text, sql_head, wait_event, trx_state)
  SELECT NOW(3), p.ID, p.USER, p.DB, p.COMMAND, LEFT(p.STATE, 64), p.TIME_MS,
         s.DIGEST, LEFT(s.DIGEST_TEXT, 160), LEFT(p.INFO, 80), w.EVENT_NAME, it.trx_state
    FROM information_schema.PROCESSLIST p
    LEFT JOIN performance_schema.threads t ON t.PROCESSLIST_ID = p.ID
    LEFT JOIN performance_schema.events_statements_current s ON s.THREAD_ID = t.THREAD_ID AND s.NESTING_EVENT_ID IS NULL
    LEFT JOIN performance_schema.events_waits_current w ON w.THREAD_ID = t.THREAD_ID
    LEFT JOIN information_schema.INNODB_TRX it ON it.trx_mysql_thread_id = p.ID
   WHERE p.COMMAND NOT IN ('Sleep', 'Daemon', 'Binlog Dump')
     AND p.ID <> CONNECTION_ID();
END//
DELIMITER ;

-- 每秒取樣；保留 1 小時
CREATE OR REPLACE EVENT ash.ev_sample ON SCHEDULE EVERY 1 SECOND DO CALL ash.take_sample();
CREATE OR REPLACE EVENT ash.ev_purge ON SCHEDULE EVERY 1 MINUTE DO DELETE FROM ash.sample WHERE ts < NOW() - INTERVAL 1 HOUR;

-- 給 exporter（或 query-exporter）讀的彙總：最近 60 秒每種「等待類別 × digest」平均有幾個 active session（AAS）。
-- 類別固定 7 種、digest 只留前 10 名其他併成 other，cardinality 上限 7 × 11。
CREATE OR REPLACE VIEW ash.aas_60s AS
SELECT wait_class, digest_label, COUNT(*) / 60 AS aas FROM (
  SELECT CASE
           WHEN x.state LIKE '%metadata lock%' THEN 'lock_mdl'
           WHEN x.trx_state = 'LOCK WAIT' THEN 'lock_row'
           WHEN x.wait_event LIKE 'wait/io/table/%' OR x.wait_event LIKE 'wait/lock/table/%' THEN 'table_io_lock'
           WHEN x.wait_event LIKE 'wait/io/file/%' THEN 'file_io'
           WHEN x.wait_event LIKE 'wait/synch/%' THEN 'synch'
           WHEN x.state IN ('Updating', 'updating', 'Sending data', 'Sending to client') AND x.wait_event IS NULL THEN 'cpu'
           ELSE 'other'
         END AS wait_class,
         IF(r.digest IS NULL, 'other', LEFT(x.digest, 12)) AS digest_label
    FROM ash.sample x
    LEFT JOIN (SELECT digest FROM ash.sample WHERE ts > NOW(3) - INTERVAL 60 SECOND AND digest IS NOT NULL
                GROUP BY digest ORDER BY COUNT(*) DESC LIMIT 10) r ON r.digest = x.digest
   WHERE x.ts > NOW(3) - INTERVAL 60 SECOND
) z GROUP BY wait_class, digest_label;
