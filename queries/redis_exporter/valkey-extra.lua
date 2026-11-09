-- redis_exporter -script（搭配 --lua-script-read-only，用 EVAL_RO）：
-- 補 INFO 裡有、redis_exporter v1.97.0 沒有輸出的欄位。輸出 redis_script_values{key="..."}。
-- 每次 scrape 在伺服器的主執行緒跑一次，只讀 2 個 INFO section，成本和一次 INFO 相近。
-- 注意：script 出錯時 exporter 把 redis_up 設成 0（exporter.go 的 extractLuaScriptMetrics 回傳 err），
-- 所以這裡不用 redis.call 以外可能失敗的操作，找不到的欄位輸出預設值。
local out = {}
local function put(k, v) out[#out + 1] = k; out[#out + 1] = tostring(v) end
local function field(info, name) return string.match(info, "\n" .. name .. ":([^\r\n]*)") end

local repl = redis.call("INFO", "replication")
-- 複本斷線多久（秒）；只在 master_link_status 不是 up 時存在，連線正常時輸出 0
put("master_link_down_since_seconds", tonumber(field(repl, "master_link_down_since_seconds") or "0") or 0)
-- min-replicas-to-write 有設時，目前「夠新」的複本數；沒設時輸出 -1
put("min_replicas_good", tonumber(field(repl, "min_slaves_good_slaves") or "-1") or -1)
-- FAILOVER 指令（不是 Sentinel）進行中
local fs = field(repl, "master_failover_state")
put("failover_in_progress", (fs and fs ~= "no-failover") and 1 or 0)

local stats = redis.call("INFO", "stats")
put("total_forks", tonumber(field(stats, "total_forks") or "0") or 0)
return out
