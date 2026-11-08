-- 兩種資料庫共用：exporter 帳號、heartbeat 表（pt-heartbeat 欄位名 ts、server_id）
CREATE USER 'exporter'@'%' IDENTIFIED BY 'exporterpass';
GRANT PROCESS, REPLICATION CLIENT, SELECT ON *.* TO 'exporter'@'%';
CREATE DATABASE heartbeat;
CREATE TABLE heartbeat.heartbeat (ts VARCHAR(26) NOT NULL, server_id INT UNSIGNED NOT NULL PRIMARY KEY);
INSERT INTO heartbeat.heartbeat VALUES (DATE_FORMAT(NOW(6), '%Y-%m-%dT%H:%i:%s.%f'), @@server_id);
