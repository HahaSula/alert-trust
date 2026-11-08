-- exporter 帳號（pg_monitor）與一個保留 WAL 但沒有人用的 slot（讓 replication_slots 有一列可看）。
CREATE ROLE exporter LOGIN PASSWORD 'exporter';
GRANT pg_monitor TO exporter;
SELECT pg_create_physical_replication_slot('trace_slot', true);
