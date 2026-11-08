#!/bin/sh
# 主庫初始化：exporter 帳號（有／沒有 pg_monitor、有 pg_use_reserved_connections）、複寫帳號、測試表與 slot。
set -e
psql -v ON_ERROR_STOP=1 -U postgres -qX <<'SQL'
CREATE ROLE exporter LOGIN PASSWORD 'exporter';
GRANT pg_monitor TO exporter;
CREATE ROLE exporter_rsv LOGIN PASSWORD 'exporter';
GRANT pg_monitor, pg_use_reserved_connections TO exporter_rsv;
CREATE ROLE nomon LOGIN PASSWORD 'nomon';
CREATE ROLE repl LOGIN REPLICATION PASSWORD 'repl';
CREATE ROLE app LOGIN PASSWORD 'app';
CREATE EXTENSION pg_stat_statements;
CREATE TABLE t(id int PRIMARY KEY, v int, pad text);
INSERT INTO t SELECT g, 0, '' FROM generate_series(1, 10000) g;
GRANT ALL ON t TO app;
SELECT pg_create_physical_replication_slot('standby_a');
SQL
echo "host replication repl all scram-sha-256" >> "$PGDATA/pg_hba.conf"
