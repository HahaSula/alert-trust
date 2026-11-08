#!/bin/sh
# 複本：等主庫就緒後 pg_basebackup，寫入 standby.signal 與 primary_conninfo，再啟動。
# 參數：$1 主庫主機、$2 application_name、$3 slot 名稱（空字串表示不用 slot）
set -e
until pg_isready -h "$1" -U postgres -q; do sleep 1; done
sleep 2
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  PGPASSWORD=repl pg_basebackup -h "$1" -U repl -D "$PGDATA" -X stream
  chmod 700 "$PGDATA"
  touch "$PGDATA/standby.signal"
  echo "primary_conninfo = 'host=$1 user=repl password=repl application_name=$2'" >> "$PGDATA/postgresql.auto.conf"
  [ -n "$3" ] && echo "primary_slot_name = '$3'" >> "$PGDATA/postgresql.auto.conf"
fi
exec postgres -c hot_standby=on -c shared_preload_libraries=pg_stat_statements -c wal_receiver_status_interval=2s
