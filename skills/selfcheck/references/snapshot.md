# 快照目錄結構

`tools/collect.py` 產生，`tools/check_coverage.py` 與各 pack 讀取。只送 HTTP GET，只用 Python 標準函式庫。

## 指令

```bash
python3 "$AT/tools/collect.py" --out <dir> [--range 7d] [--timeout 60] [--no-metric-jobs] \
  [--prom [ID=]URL]... [--vmalert [ID=]URL]... [--vm [ID=]URL]... \
  [--vmagent [ID=]URL]... [--vlogs [ID=]URL]... [--am [ID=]URL]...
```

- 每個參數都可以重複。`ID` 用 env 檔裡的 component 或 observer id；沒給時由 host 與 port 推出（例如 `localhost-9090`），並印出提醒。同一個 id 不能出現兩次。
- `--vm` 給 VictoriaMetrics single 的 URL，或 cluster 的 vmselect 查詢前綴（`http://vmselect:8481/select/0/prometheus`）；`/flags`、`/metrics` 會從 host 根目錄讀。
- `--no-metric-jobs`：跳過「每個 alert 用到的 metric 在哪些 job」的查詢（metric 多時查詢數很多）。
- 任何一個端點失敗都只記進 `errors.json`，其他照常蒐集，結束碼仍是 0。參數錯誤時結束碼是 2。

驗證：依序找 `AT_TOKEN_<ID>`（id 轉大寫，非英數字元換成 `_`），再找 `PROM_BEARER_TOKEN`、`VMALERT_BEARER_TOKEN`、`VM_BEARER_TOKEN`、`VMAGENT_BEARER_TOKEN`、`VLOGS_BEARER_TOKEN`、`AM_BEARER_TOKEN`。Token 不會寫進檔案；寫進檔案的 URL 也去掉了 `user:password@` 與 query string。

## 目錄

```text
<dir>/
  manifest.json          蒐集時間、range、每個 id 的 kind、url、檔案清單、錯誤數
  <id>/
    component.json       {id, kind, url, collected_at}
    errors.json          {名稱: 錯誤訊息}；{} 代表全部讀到
    buildinfo.json       {"status":"success","data":{"version":..., ...}}，讀得到版本時才有
    ...                  依 kind 而定，見下表
```

| 檔案 | prom | vmalert | vm | vmagent | vlogs | am | 內容 |
|---|---|---|---|---|---|---|---|
| `rules.json` | ✓ | ✓ | | | | | `/api/v1/rules` 的**原始回應**（含 `status`、`data.groups`）。check_coverage 讀這個 |
| `buildinfo.json` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | prom 是 `/api/v1/status/buildinfo` 原始回應；VM 系列由 `/metrics` 的 `vm_app_version` 推出（`data.source: metrics:vm_app_version`）；am 取自 `/api/v2/status` 的 `versionInfo` |
| `flags.json` | ✓ | ✓ | ✓ | ✓ | ✓ | | prom 是 `/api/v1/status/flags` 原始回應。VM 系列是 `{"status":"success","data":{flag: value},"set":[明確設定的 flag],"source":...}`，合併 `/flags` 與 `flag{name,value,is_set}` metric；secret 類 flag 由 VM 自己遮成 `secret` |
| `flags.txt` | | ✓ | ✓ | ✓ | ✓ | | `/flags` 原始文字 |
| `metrics.txt` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | `/metrics` 原始文字，可以餵給 `tools/check_metrics.py` |
| `alerts.json` | | ✓ | | | | ✓ | vmalert `/api/v1/alerts`；am `/api/v2/alerts`（含被 silence、inhibit 的） |
| `silences.json` | | | | | | ✓ | `/api/v2/silences` |
| `status.json` | | | | | | ✓ | `/api/v2/status`：`config.original`、`cluster`、`versionInfo` |
| `runtimeinfo.json`、`tsdb.json`、`walreplay.json`、`alertmanagers.json`、`targets.json`、`config.json`、`metadata.json` | ✓ | | (`tsdb.json`) | | | | 對應的 Prometheus 狀態 API 原始回應 |
| `peaks.json` | ✓ | | ✓ | | | | `{"range","exprs","results"}`：尖峰查詢，`results[名稱]` 是 instant query 的 `result`；查詢失敗時是 `null` |
| `metric_jobs.json` | ✓ | | ✓ | | | | `{metric: {job: series 數}}`：alert 用到的 metric 在哪些 job。vm 的版本用同一次蒐集到的所有 vmalert 的 rule（近似值：不分 vmalert 的 datasource） |

`peaks.json` 的查詢名稱（`scrape_duration_peak`、`group_duration_peak`、`vmalert_iteration_duration_peak`、`partial_results`……）寫在 `exprs` 裡，引用數字時同時引用名稱與查詢式。VM 的尖峰只有在元件的 `/metrics` 有被抓進這個 VM 時才有值；`has_*` 開頭的查詢告訴你哪些資料來源存在。

## 不能連 API 時

請使用者貼輸出，照上表的檔名存進 `<dir>/<id>/`，並手動寫 `component.json`（`{"id","kind","url":"pasted"}`）。最重要的是每個 ruler 的 `rules.json`（`/api/v1/rules` 原始 JSON），check_coverage 只靠它就能判斷 `covered`、`self_only`、`missing`。

## 隱私

快照可能含主機名、label 值、`config.original`（Alertmanager 會把 secret 欄位遮成 `<secret>`，但 URL 路徑、收件人仍在）。放在 `selfcheck-data/`，確認 `.gitignore` 有它，不要 commit 或公開。
