CREATE ROLE exporter LOGIN PASSWORD 'exporter';
GRANT pg_monitor TO exporter;
-- 沒有 pg_monitor 的使用者：模擬權限被收回或新環境漏了 GRANT
CREATE ROLE nomon LOGIN PASSWORD 'nomon';
CREATE TABLE t(id int);
INSERT INTO t SELECT generate_series(1,1000);
