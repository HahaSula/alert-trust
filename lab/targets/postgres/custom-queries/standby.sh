#!/bin/sh
# 複本：等主庫起來，pg_basebackup 用 slot s1slot，application_name=s1，hot_standby_feedback=on。
set -e
until pg_isready -h p -U postgres >/dev/null 2>&1; do sleep 1; done
sleep 3
if [ ! -s /var/lib/postgresql/data/PG_VERSION ]; then
  mkdir -p /var/lib/postgresql/data && chown postgres /var/lib/postgresql/data && chmod 700 /var/lib/postgresql/data
  gosu postgres pg_basebackup -h p -U postgres -D /var/lib/postgresql/data -S s1slot -R -X stream
  cat >> /var/lib/postgresql/data/postgresql.auto.conf <<CONF
primary_conninfo = 'host=p user=postgres password=postgres application_name=s1'
primary_slot_name = 's1slot'
hot_standby_feedback = on
max_prepared_transactions = 5
CONF
fi
exec gosu postgres postgres -D /var/lib/postgresql/data
