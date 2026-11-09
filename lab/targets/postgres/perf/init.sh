#!/bin/sh
# exporter 帳號（pg_monitor；其中一個設 lock_timeout）、bench 資料庫與 pg_stat_statements。
set -e
psql -v ON_ERROR_STOP=1 -U postgres -qX <<'SQL'
CREATE ROLE exporter LOGIN PASSWORD 'exporter';
CREATE ROLE exporter_lt LOGIN PASSWORD 'exporter';
GRANT pg_monitor TO exporter, exporter_lt;
ALTER ROLE exporter_lt SET lock_timeout = '1s';
CREATE DATABASE bench;
\c bench
CREATE EXTENSION pg_stat_statements;
SQL
