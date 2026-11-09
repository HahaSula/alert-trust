#!/bin/sh
# 主庫初始化：允許複寫連線、建立 qe_monitor（只有 pg_monitor）、應用資料庫 app 與測試表。
set -e
echo "host replication all all scram-sha-256" >> "$PGDATA/pg_hba.conf"
psql -v ON_ERROR_STOP=1 -U postgres <<'SQL'
CREATE ROLE qe_monitor LOGIN PASSWORD 'qe';
GRANT pg_monitor TO qe_monitor;
SELECT pg_create_physical_replication_slot('s1slot');
CREATE DATABASE app;
ALTER SYSTEM SET synchronous_standby_names = 'FIRST 1 (s1)';
ALTER SYSTEM SET archive_mode = 'on';
ALTER SYSTEM SET archive_command = '/archive.sh';
SQL
psql -v ON_ERROR_STOP=1 -U postgres -d app <<'SQL'
CREATE EXTENSION pg_buffercache;
CREATE TABLE orders (id int, v text) WITH (autovacuum_enabled = off);
INSERT INTO orders SELECT g % 1000, 'x' FROM generate_series(1, 5000) g;
CREATE TABLE victim (id int, v text);
INSERT INTO victim SELECT g, repeat('z', 100) FROM generate_series(1, 200) g;
SQL
