#!/usr/bin/env bash
# MongoDB 8.0.34 replica set ＋ mongodb_exporter v0.54.0 的監控實驗，約 18 分鐘。原始輸出寫到 results/run-<日期>.txt，結束會 docker compose down -v。
# 注意：這台機器的 Docker 容器單調時鐘比牆鐘慢約 30%。electionTimeoutMillis、heartbeat、Prometheus 的 for 與 scrape_duration_seconds
# 都用單調時鐘，時間類數字只當參考；「幾秒後」用主機的 date 量，mongod log 與 mongosh 的 Date 是牆鐘。
set -u
cd "$(dirname "$0")"
OUT=results/run-$(date +%Y%m%d).txt
mkdir -p results prom/rules
cp ../../../rules/targets/mongodb.yml prom/rules/mongodb.yml
Q() { python3 -I q.py "$@"; }
H() { echo; echo "## $1（$(date +%T)）"; }
AUTH=(-u root -p rootpw --authenticationDatabase admin)
# M <member> '<js>'：在 member 上用 root 跑 mongosh
M() { local s=$1; shift; docker compose exec -T "$s" mongosh --quiet "${AUTH[@]}" --eval "$*" 2>&1 | sed 's/^/    /'; }
primary() { for s in m1 m2 m3; do p=$(docker compose exec -T "$s" mongosh --quiet "${AUTH[@]}" --eval 'db.hello().primary' 2>/dev/null | tr -d '\r' | cut -d: -f1); [ -n "$p" ] && [ "$p" != "undefined" ] && { echo "$p"; return; }; done; echo none; }
others() { for s in m1 m2 m3; do [ "$s" != "$1" ] && echo "$s"; done; }
# 在 primary 上背景寫入：writer <member> <秒數> <每筆 bytes> <間隔 ms>。明確用 w:1：預設 write concern 是 majority（見 E），
# majority 不可能達成時預設的寫入會一直等。
writer() { docker compose exec -T "$1" mongosh --quiet "${AUTH[@]}" --eval "
  const end = Date.now() + $2*1000, pad = 'x'.repeat($3); let n = 0, err = 0;
  while (Date.now() < end) { try { db.getSiblingDB('app').w.insertOne({t: new Date(), pad}, {writeConcern: {w: 1}}); n++; } catch (e) { err++; } sleep($4); }
  print('writer done: inserted', n, 'errors', err);" 2>&1 | sed 's/^/    [writer] /' & WPID=$!; }

docker compose down -v >/dev/null 2>&1
docker compose up -d m1 m2 m3 >/dev/null 2>&1
sleep 12
docker compose exec -T m1 mongosh --quiet --eval 'rs.initiate({_id:"rs0",members:[{_id:0,host:"m1:27017"},{_id:1,host:"m2:27017"},{_id:2,host:"m3:27017"}]}).ok' >/dev/null
docker compose exec -T m1 mongosh --quiet --eval 'for (let i = 0; i < 60 && !db.hello().isWritablePrimary; i++) sleep(1000); print("m1 isWritablePrimary", db.hello().isWritablePrimary)'
docker compose exec -T m1 mongosh --quiet --eval '
  const a = db.getSiblingDB("admin");
  a.createUser({user:"root",pwd:"rootpw",roles:["root"]}); a.auth("root","rootpw");
  a.createUser({user:"exporter",pwd:"exppw",roles:[{role:"clusterMonitor",db:"admin"},{role:"read",db:"local"}]});
  a.createUser({user:"appreader",pwd:"apppw",roles:[{role:"read",db:"app"}]});
  db.getSiblingDB("app").w.insertOne({init:1}); print("users created");'
docker compose up -d >/dev/null 2>&1
sleep 40
exec > >(tee "$OUT") 2>&1
echo "環境：MongoDB 8.0.34 ×3（replica set rs0，--wiredTigerCacheSizeGB 0.25、--oplogSize 2、enableTestCommands=1、keyFile），"
echo "      percona/mongodb_exporter:0.54.0（e1～e3 每 member 一個，加 --collector.diagnosticdata --collector.replicasetstatus --compatible-mode），"
echo "      Prometheus v3.13.5（scrape_interval 5s、scrape_timeout 4s；規則 rules/targets/mongodb.yml 與 prom/rules/upstream.yml）。主機 nproc=$(nproc)"
M m1 'rs.status().members.map(m => m.name + " " + m.stateStr).join(", ")'

H "A 基線：每種 exporter 設定輸出什麼"
Q targets
for j in mongodb mongodb-default mongodb-norole mongodb-all mongodb-multi mongodb-scrapeall; do
  Q names "$j" > "results/names-$j.txt"
  echo "  $j：$(wc -l < "results/names-$j.txt") 個 metric 名稱（存到 results/names-$j.txt）"
done
Q q 'scrape_samples_scraped'
Q q 'scrape_duration_seconds'
Q q 'collector_scrape_time_ms{job=~"mongodb|mongodb-norole|mongodb-default", instance="m1:27017"}'
echo "  沒有 clusterMonitor 的 exporter（e-norole）的 log（預設 log.level=error）："
docker compose logs --no-log-prefix e-norole 2>&1 | grep -o 'level=[A-Z]* source=[^ ]* msg="[^"]*"' | sort | uniq -c | sed 's/^/    /'
echo "  同一個帳號在 m1 造成的 user assert（Unauthorized 也算）："
Q q 'increase(mongodb_ss_asserts{job="mongodb", instance="m1:27017", assert_type="user"}[1m])'
echo "  規則 MongoDBDiagnosticDataMissing 的運算式換成 default／norole 兩個 job："
Q q 'mongodb_up{job=~"mongodb-default|mongodb-norole"} == 1 unless on (job, instance) mongodb_ss_uptime'
echo "  上游規則用到的 metric 與 label 在不在："
for e in 'mongodb_mongod_wiredtiger_concurrent_transactions_available_tickets{job="mongodb",instance="m1:27017"}' \
         'mongodb_ss_wt_concurrentTransactions_available{job="mongodb",instance="m1:27017"}' \
         'count(mongodb_ss_wt_concurrentTransactions_available{job="mongodb",txn_rw_type=~".+"})' \
         'mongodb_ss_wt_concurrentTransactions_totalTickets{job="mongodb",instance="m1:27017"}' \
         'count(mongodb_rs_members_optimeDate{job="mongodb",set=~".+"})' \
         'count(mongodb_mongod_replset_member_replication_lag{job="mongodb"})' \
         'count(mongodb_ss_repl_isWritablePrimary{job="mongodb",rs_nm=~".+"})' \
         'count(mongodb_mongod_replset_my_state{job="mongodb"})' \
         'count(mongodb_instance_uptime_seconds{job="mongodb"})' \
         'count(mongodb_dbstats_fsUsedSize{job="mongodb"})' \
         'count(mongodb_dbstats_fsUsedSize{job="mongodb-all"})' \
         'count(mongodb_ss_security_SSLServerCertificateExpirationDate)' \
         'count(mongodb_mongos_sharding_changelog_10min_total)'; do Q q "$e"; done
echo "  閒置時 secondary 自己回報的延遲（每 3 秒一次，共 30 秒）："
for i in $(seq 10); do echo "    $(date +%T) $(Q q 'mongodb_mongod_replset_member_replication_lag{job="mongodb",self="1"}' | grep -o 'name="m.:27017".*} [0-9]*' | sed 's/, self.*}//' | paste -sd' ')"; sleep 3; done
echo "  同一個 secondary 被幾個 exporter 回報延遲（每個 member 都從自己的 replSetGetStatus 算一份）："
Q q 'count by (name) (mongodb_mongod_replset_member_replication_lag{job="mongodb"})'
echo "  rs_state label：每個 metric 都帶 member 自己的狀態"
Q q 'count by (instance, rs_state) (mongodb_ss_uptime{job="mongodb"})'
Q alerts
Q q 'ALERTS{component="upstream"}'

H "C oplog window：--oplogSize 2（2MB），寫入 2000 筆 5KB"
Q q 'mongodb_oplog_stats_storageStats_maxSize{job="mongodb"}'
M m1 'const p="x".repeat(5000); for (let i=0;i<2000;i++) db.getSiblingDB("app").c.insertOne({i,p}); "inserted"'
sleep 15
M m1 'const r = db.getReplicationInfo(); JSON.stringify({logSizeMB: r.logSizeMB, usedMB: r.usedMB, timeDiff: r.timeDiff, tFirst: r.tFirst, tLast: r.tLast})'
Q q 'mongodb_mongod_replset_oplog_head_timestamp{job="mongodb"} - mongodb_mongod_replset_oplog_tail_timestamp{job="mongodb"}'
Q q 'mongodb_oplog_stats_storageStats_size{job="mongodb"}'
echo "  oplog 什麼時候截斷（每 15 秒一次，最多 150 秒；截斷要等 stable checkpoint 推進，checkpoint 預設 60 秒一次）："
for i in $(seq 10); do sz=$(Q q 'mongodb_oplog_stats_storageStats_size{job="mongodb",instance="m1:27017"}' | grep -o '} [0-9]*' | tr -d '} '); echo "    $(date +%T) m1 oplog size=${sz}"; [ -n "$sz" ] && [ "$sz" -lt 3000000 ] && break; sleep 15; done
docker compose logs --no-log-prefix m1 2>/dev/null | grep '"WiredTiger record store oplog truncation finished"' | grep -o '"t":{"$date":"[^"]*"}' | sed 's/^/    [log] truncation finished /'
echo "  規則 MongoDBOplogWindowShort 的前半（不含 uptime > 3600 的條件）："
Q q '(mongodb_mongod_replset_oplog_head_timestamp{job="mongodb"} - mongodb_mongod_replset_oplog_tail_timestamp{job="mongodb"}) < 3600'

H "D secondary 延遲：m3 fsyncLock 40 秒，primary 每 100ms 寫一筆"
P=$(primary); echo "  primary 是 $P"
writer "$P" 45 200 100
sleep 2
M m3 'db.fsyncLock().lockCount'
for t in 15 35; do
  sleep $((t == 15 ? 13 : 20))
  echo "  fsyncLock 後約 ${t} 秒："
  Q snap
  Q q 'mongodb_mongod_replset_member_replication_lag{job="mongodb",name="m3:27017"}'
  Q q '(mongodb_rs_members_optimeDate{job="mongodb",member_idx="'"$P"':27017",instance="'"$P"':27017"} - ignoring(member_idx, member_state) group_right mongodb_rs_members_optimeDate{job="mongodb",member_idx="m3:27017",instance="'"$P"':27017"}) / 1000'
  Q q 'mongodb_ss_flowControl_isLagged{job="mongodb"}'
  Q q 'scrape_duration_seconds{job="mongodb"}'
done
M "$P" 'db.getSiblingDB("app").w.insertOne({wc:1},{writeConcern:{w:"majority",wtimeout:5000}}).acknowledged'
M m3 'db.fsyncUnlock().lockCount'
wait $WPID
sleep 10
echo "  fsyncUnlock 10 秒後："
Q snap

H "E majority commit point 停住：m2、m3 都 fsyncLock 70 秒，primary 每 50ms 寫一筆 5KB（明確指定 w:1）"
P=$(primary); echo "  primary 是 $P"
S=$(others "$P" | paste -sd' ')
for s in $S; do M "$s" 'db.fsyncLock().lockCount'; done
writer "$P" 70 5000 50
sleep 20
echo "  w:majority＋wtimeout 5000："
M "$P" 'try { db.getSiblingDB("app").w.insertOne({wc:2},{writeConcern:{w:"majority",wtimeout:5000}}); "ok" } catch (e) { e.codeName + ": " + e.message }'
echo "  預設 write concern："
M "$P" 'const d = db.adminCommand({getDefaultRWConcern: 1}); JSON.stringify(d.defaultWriteConcern) + " " + d.defaultWriteConcernSource'
echo "  不指定 write concern 的 insert（主機 timeout 8 秒中斷 mongosh）："
t0=$(date +%s.%N); timeout 8 docker compose exec -T "$P" mongosh --quiet "${AUTH[@]}" --eval 'db.getSiblingDB("app").w.insertOne({wc:3}); print("returned")' 2>&1 | sed 's/^/    /'
echo "    rc=${PIPESTATUS[0]}，經過 $(python3 -c "import sys; print(round($(date +%s.%N) - $t0, 1))") 秒（主機時鐘）"
sleep 20
echo "  fsyncLock 約 50 秒後："
Q q 'mongodb_ss_metrics_getLastError_wtimeouts{job="mongodb",instance="'"$P"':27017"}'
Q q '(mongodb_rs_optimes_lastAppliedWallTime{job="mongodb"} - mongodb_rs_optimes_lastCommittedWallTime{job="mongodb"}) / 1000'
Q q 'mongodb_ss_flowControl_isLagged{job="mongodb",instance="'"$P"':27017"}'
Q q 'mongodb_ss_flowControl_targetRateLimit{job="mongodb",instance="'"$P"':27017"}'
Q q 'mongodb_ss_flowControl_isLaggedCount{job="mongodb",instance="'"$P"':27017"}'
Q q 'mongodb_oplog_stats_storageStats_size{job="mongodb",instance="'"$P"':27017"}'
Q q 'mongodb_mongod_replset_oplog_head_timestamp{job="mongodb",instance="'"$P"':27017"} - mongodb_mongod_replset_oplog_tail_timestamp{job="mongodb",instance="'"$P"':27017"}'
Q snap
wait $WPID
for s in $S; do M "$s" 'db.fsyncUnlock().lockCount'; done
sleep 20
echo "  解鎖 20 秒後："
Q q '(mongodb_rs_optimes_lastAppliedWallTime{job="mongodb"} - mongodb_rs_optimes_lastCommittedWallTime{job="mongodb"}) / 1000'
Q q 'mongodb_oplog_stats_storageStats_size{job="mongodb"}'
Q q 'mongodb_ss_flowControl_isLaggedCount{job="mongodb"}'
Q alerts

H "F 伺服器忙：primary 上 sleep 12 秒並持有 global W 鎖"
P=$(primary); echo "  primary 是 $P"
(M "$P" 'db.adminCommand({sleep:1, secs:12, lock:"w"}).ok' | sed 's/^/  [sleep] /') & SPID=$!
sleep 2
echo "  直接抓（X-Prometheus-Scrape-Timeout-Seconds: 4，exporter 給查詢 3 秒）："
Q fetch "http://e${P#m}:9216/metrics"
Q fetch "http://e-multi:9216/scrape?target=${P}:27017"
Q fetch "http://e-multi:9216/scrapeall" 12
Q fetch "http://e-default:9216/metrics"
sleep 1
Q q 'up{job=~"mongodb|mongodb-multi|mongodb-scrapeall"}'
Q q 'scrape_samples_scraped{job=~"mongodb|mongodb-multi|mongodb-scrapeall"}'
Q q 'scrape_duration_seconds{job=~"mongodb|mongodb-multi|mongodb-scrapeall"}'
wait $SPID
sleep 6
echo "  sleep 結束後："
Q q 'scrape_samples_scraped{job=~"mongodb|mongodb-multi|mongodb-scrapeall"}'

H "G 停掉 primary（docker kill）"
P=$(primary); echo "  primary 是 $P；$(date +%T.%N | cut -c1-12) kill"
S1=$(others "$P" | head -1)
docker compose kill "$P" >/dev/null 2>&1
docker compose exec -T "$S1" mongosh --quiet "${AUTH[@]}" --eval '
  const end = Date.now() + 30000; let last = "";
  while (Date.now() < end) { let h; try { h = db.hello(); } catch (e) { h = {primary: "err"}; }
    const s = String(h.primary); if (s !== last) { print(new Date().toISOString(), "primary =", s); last = s; } sleep(500); }' 2>&1 | sed "s/^/    [$S1 看 hello] /"
grep -h '"Starting an election\|"Election succeeded\|"Transition to primary complete' <(docker compose logs --no-log-prefix m1 m2 m3 2>/dev/null) | grep -o '"t":{"$date":"[^"]*"}.*"msg":"[^"]*"' | tail -6 | sed 's/^/    [log] /'
echo "  kill 約 32 秒後："
Q snap
Q q 'mongodb_up{job=~"mongodb|mongodb-multi"}'
Q q 'count by (cluster) (mongodb_ss_repl_isWritablePrimary{job="mongodb"} == 1)'
Q q 'mongodb_rs_members_health{job="mongodb", member_idx="'"$P"':27017"}'
Q q 'max by (cluster) (mongodb_rs_term{job="mongodb"})'
Q q 'up{job="mongodb-scrapeall"}'
Q q 'scrape_samples_scraped{job=~"mongodb|mongodb-scrapeall"}'
sleep 20
Q alerts
Q q 'ALERTS{component="upstream"}'
docker compose start "$P" >/dev/null 2>&1
sleep 30
echo "  $P 重新啟動 30 秒後："
Q snap
Q q 'count by (__name__) ({job="mongodb", instance="'"$P"':27017", __name__=~"mongodb_ss_uptime|mongodb_rs_term"})'
Q q 'changes(mongodb_mongod_replset_my_state{job="mongodb"}[5m])'
Q q 'max by (cluster) (mongodb_rs_term{job="mongodb"}) - min by (cluster) (min_over_time(mongodb_rs_term{job="mongodb"}[15m]))'
Q alerts
Q q 'ALERTS{component="upstream"}'

H "G2 一個 secondary 卡住（docker pause 25 秒）：exporter 與 multi-target"
P=$(primary); X=$(others "$P" | tail -1); echo "  primary $P，pause $X"
docker compose pause "$X" >/dev/null 2>&1
sleep 8
Q fetch "http://e${X#m}:9216/metrics"
Q fetch "http://e-multi:9216/scrape?target=${X}:27017"
Q fetch "http://e-multi:9216/scrapeall" 12
sleep 12
Q q 'up{job=~"mongodb|mongodb-multi|mongodb-scrapeall"}'
Q q 'mongodb_up{job=~"mongodb|mongodb-multi"}'
Q q 'scrape_duration_seconds{job=~"mongodb|mongodb-multi|mongodb-scrapeall"}'
Q q 'scrape_samples_scraped{job=~"mongodb|mongodb-multi|mongodb-scrapeall"}'
Q q 'mongodb_rs_members_health{job="mongodb", member_idx="'"$X"':27017"}'
docker compose unpause "$X" >/dev/null 2>&1
sleep 15

H "H 停掉兩個 secondary（primary 還活著）"
P=$(primary); S=$(others "$P" | paste -sd' '); echo "  primary $P，$(date +%T.%N | cut -c1-12) kill $S"
docker compose kill $S >/dev/null 2>&1
docker compose exec -T "$P" mongosh --quiet "${AUTH[@]}" --eval '
  const end = Date.now() + 25000; let last = "";
  while (Date.now() < end) { const h = db.hello(); const s = h.isWritablePrimary + " " + h.secondary;
    if (s !== last) { print(new Date().toISOString(), "isWritablePrimary/secondary =", s); last = s; } sleep(500); }' 2>&1 | sed "s/^/    [$P 看 hello] /"
grep -h "relinquishing primary\|Stepping down" <(docker compose logs --no-log-prefix "$P" 2>/dev/null) | grep -o '"t":{"$date":"[^"]*"}.*"msg":"[^"]*"' | tail -3 | sed 's/^/    [log] /'
echo "  寫入："
M "$P" 'try { db.getSiblingDB("app").w.insertOne({x:1}); "ok" } catch (e) { e.codeName }'
sleep 15
echo "  kill 約 45 秒後："
Q snap
Q q 'up{job="mongodb"}'
Q q 'mongodb_up{job="mongodb"}'
Q q 'count by (cluster) (mongodb_up{job="mongodb"}) unless count by (cluster) (mongodb_ss_repl_isWritablePrimary{job="mongodb"} == 1)'
Q q 'count(mongodb_mongod_replset_member_replication_lag{job="mongodb"})'
Q q 'mongodb_rs_members_health{job="mongodb"}'
sleep 20
Q alerts
Q q 'ALERTS{component="upstream"}'
docker compose start $S >/dev/null 2>&1
sleep 30
echo "  兩台重新啟動 30 秒後："
Q snap

H "結束"
docker compose down -v >/dev/null 2>&1
echo "  殘留容器：$(docker ps -a --format '{{.Names}}' | grep -c '^at-mongo-')；殘留 volume：$(docker volume ls -q | grep -c '^at-mongo-')"
