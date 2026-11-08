# 覆蓋檢查器的驗證環境

一台受測的 Prometheus（`prom-a`）、一台外部觀察者（`meta`）、一台 Alertmanager，刻意埋了三種缺口，確認 `tools/check_coverage.py` 抓得到。3 個小容器，跑 1 分鐘即可。

| 埋的缺口 | 做法 | 應判為 |
|---|---|---|
| WAL 寫入失敗沒人看 | 觀察者的規則拿掉 `WALWriteFailed` | 缺少 |
| Rule 失敗只在自己身上 | `RuleEvaluationFailures` 只放在 `prom-a` | 只在自己身上 |
| Job 名稱對不上 | 觀察者用 job `prom-a-self` 抓 `prom-a`，規則比對 `job=~".*prometheus.*"` | 規則在但沒資料（需要 `--live`） |

```bash
docker compose up -d
# 等 30 秒讓 rule 跑過幾輪
mkdir -p snapshot/prom-a snapshot/meta
curl -s http://localhost:19301/api/v1/rules > snapshot/prom-a/rules.json
curl -s http://localhost:19302/api/v1/rules > snapshot/meta/rules.json
python3 ../../tools/check_coverage.py --env env.yaml --snapshot snapshot --out report --live
docker compose down -v
```

`report-20261108.md` 是一次執行的結果：三個缺口都抓到，另外 `PrometheusCrashLooping` 也被判為「規則在但沒資料」，因為它同樣用 `job=~".*prometheus.*"` 比對，這是規則範本在 job 名稱不同的環境會遇到的真實問題。快照另存在 `tests/fixtures/coverage/`，給離線測試用。
