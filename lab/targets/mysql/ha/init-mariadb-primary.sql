-- 帳號不寫 binlog（replica 自己也建一份）；其餘物件寫 binlog，讓 replica 從頭複製。
SET SESSION sql_log_bin = 0;
CREATE USER 'exporter'@'%' IDENTIFIED BY 'exporterpw' WITH MAX_USER_CONNECTIONS 3;
GRANT PROCESS, REPLICATION CLIENT, SLAVE MONITOR, SELECT ON *.* TO 'exporter'@'%';
CREATE USER 'repl'@'%' IDENTIFIED BY 'replpw';
GRANT REPLICATION SLAVE ON *.* TO 'repl'@'%';
CREATE USER 'app'@'%' IDENTIFIED BY 'apppw';
GRANT ALL ON app.* TO 'app'@'%';
CREATE USER 'lim'@'%' IDENTIFIED BY 'limpw' WITH MAX_USER_CONNECTIONS 2;
GRANT SELECT ON app.* TO 'lim'@'%';
SET SESSION sql_log_bin = 1;
CREATE DATABASE app;
CREATE TABLE app.t (id INT PRIMARY KEY, v INT NOT NULL DEFAULT 0);
INSERT INTO app.t (id) SELECT seq FROM app.seq_1_to_200;
CREATE DATABASE heartbeat;
CREATE TABLE heartbeat.heartbeat (ts DATETIME(6) NOT NULL, server_id INT UNSIGNED NOT NULL PRIMARY KEY);
CREATE EVENT heartbeat.beat ON SCHEDULE EVERY 1 SECOND
  DO REPLACE INTO heartbeat.heartbeat (ts, server_id) VALUES (NOW(6), @@server_id);
