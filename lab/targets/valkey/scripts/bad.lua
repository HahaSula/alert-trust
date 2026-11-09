-- 故意失敗的 -script：測試 script 錯誤對 redis_up 的影響（lab 專用）
return redis.call("NO_SUCH_COMMAND")
