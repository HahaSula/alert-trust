---
name: env-profile
description: Stage 1 of the alert-trust self-check. Auto-detects what it can (Prometheus buildinfo, VictoriaMetrics /flags and vm_app_version, Alertmanager /api/v2/status), then interviews the owner in rounds of at most 3 questions and writes env/<name>.yaml in the format of env/env.example.yaml. Use before any pack, or when the environment changed. 建立環境描述檔 env/<name>.yaml：先自動偵測，再分輪訪談。
argument-hint: "[name] [url...]"
---

# 建立環境描述

產出 `env/<name>.yaml`（目前工作目錄下），描述有哪些元件、誰監控誰、DMS 與關鍵 alert。後面的 collect、check_coverage 與每個 pack 都以它為準。

`$AT` 是 alert-trust 根目錄（plugin 安裝時 `${CLAUDE_PLUGIN_ROOT}`，在 repo 裡是 `${CLAUDE_SKILL_DIR}/../..`）。

## 全程規則

- **唯讀**：偵測只送 HTTP GET，不呼叫 reload、admin 或任何會改狀態的端點。
- **隱私**：env 檔描述的是使用者的環境（主機名、拓撲、DMS）。**提醒使用者它不能 commit 到公開 repo**；確認 `.gitignore` 有 `env/*.yaml`（只放行 `env.example.yaml`），沒有就建議加上。不要把 token 寫進 env 檔，也不要請使用者貼 token；需要驗證時請對方設定 `AT_TOKEN_<ID>` 環境變數。
- **能偵測的不要問；每輪最多 3 題**，每題說明為什麼要問、不知道時怎麼查；答不出來記「未知」，繼續。
- **每個值標來源**：在 YAML 行尾用註解 `# 量測`、`# 使用者說`、`# 推算`。
- 用使用者的語言。

## 先讀

1. `$AT/env/env.example.yaml`：欄位與格式。只用它出現過的鍵，再加一個頂層 `unknowns:`（字串清單）記錄沒答案的題目。
2. `$AT/docs/model.md` 的「依 infra 組合選卡片」與「檢查流程」：決定要用哪些卡。
3. `type` 的合法值就是卡片 id：`ls $AT/catalog/components $AT/catalog/targets`（監控鏈：`prometheus`、`thanos`、`vmagent`、`victoriametrics`、`victorialogs`、`vmalert`、`grafana_alerting`、`alertmanager`；資料庫：`mysqld_exporter`、`postgres_exporter`、`redis_exporter`、`mongodb_exporter`、`etcd`、`tidb`、`yugabytedb`、`clickhouse`……，以 `ls` 的結果為準；`valkey`、`grafana`、`mongodb` 等常見別名也接受，見 `tools/check_coverage.py` 的 `TYPE_ALIASES`）。目錄沒有的類型照實寫，告訴使用者這部分沒有卡，只能靠通用原則。

## 第 0 步：自動偵測

使用者給了 URL（或可以 port-forward）時，先偵測再問。逐一 GET：

| 元件 | 端點 | 拿到什麼 |
|---|---|---|
| Prometheus | `/api/v1/status/buildinfo` | `version` |
| | `/api/v1/status/flags` | `rules.alert.resend-delay`、`query.lookback-delta`、`storage.tsdb.retention.*` |
| | `/api/v1/alertmanagers` | 它送往哪幾台 Alertmanager（`notifiers`） |
| | `/api/v1/status/config` | `rule_query_offset`、scrape job 名稱（`jobs` 與 `targets[].job` 的候選） |
| | `/api/v1/targets?state=active` | 它抓了誰：判斷它是不是別人的觀察者（`scrapes`） |
| vmalert／VM／vmagent／VictoriaLogs | `/metrics` 的 `vm_app_version{short_version=}` | 版本；`version` 標籤的前綴分得出是 vmalert、vmagent、vmselect、vminsert、vmstorage、victoria-metrics、victoria-logs |
| | `/flags` 與 `/metrics` 的 `flag{name,value,is_set}` | vmalert：`datasource.url`、`notifier.url`、`rule.resendDelay`、`rule.evalDelay`、`remoteWrite.url`、`remoteRead.url`（URL 類 flag 會顯示 `secret`）；VM：`replicationFactor`、`storageNode`（cluster） |
| Alertmanager | `/api/v2/status` | `versionInfo.version`、`cluster.peers` 數量（`replicas`）、`config.original`（DMS 路由、`group_interval`） |

最快的做法是直接跑 collect，再讀它的輸出：

```bash
python3 "$AT/tools/collect.py" --out selfcheck-data/<name>-probe --range 1h --no-metric-jobs \
  --prom prom-a=http://... --vmalert vmalert-biz=http://... --am am=http://...
```

讀每個 `<id>/buildinfo.json`、`flags.json`、`status.json`、`targets.json`、`alertmanagers.json`。偵測到的值直接填，問的時候只請使用者確認。

## 訪談：三輪

| 輪 | 問什麼 | 寫到哪裡 | 為什麼 |
|---|---|---|---|
| 1 | 有哪些元件與版本；怎麼連（直連 URL、`kubectl port-forward`、只能貼資料）；VM 是 single 還是 cluster、RF 多少 | `components[]`（`id`、`type`、`version`、`url`、`mode`、`replicas`、`replication_factor`） | 決定用哪幾張卡、collect 怎麼跑 |
| 2 | 誰監控誰：外部觀察者在哪個故障域；它的告警走哪台 Alertmanager；DMS 是什麼、多久沒收到會通知誰 | `observers[]`（`failure_domain`、`notify_via`、`scrapes`、`federates_from`）、`deadman` | 腳本看不到架構；這是 observer 卡 `needs_input` 的來源 |
| 3 | 最重要的 3 條 alert、通知時限、關鍵 job 與 log 流；有哪些 exporter target、由誰抓、job 名稱 | `critical`、`targets[]`（`id`、`type`、`scraped_by`、`job`） | 決定 heartbeat 涵蓋誰、延遲能不能接受、要用哪些 target 卡 |

已經偵測到的題目跳過，題數不到 3 就把下一輪的題目提前。vmalert 要額外確認的（偵測不到時才問）：

- 每個 vmalert 的 `datasource`（指向哪個 VM 或 VL 的 id）、`notifiers`、`resend_delay`，以及哪一台專門放 Watchdog（`role: watchdog`）。
- Watchdog 和業務 rule 是否在同一個 vmalert：同一個且設了 `-rule.resendDelay` 時，Watchdog 的假 resolved 也會被延後（見 `$AT/catalog/components/vmalert.yaml#watchdog`）。

K8s 連線範例：

```bash
kubectl -n <ns> port-forward svc/<prometheus-svc> 9090:9090
kubectl -n <ns> port-forward svc/<vmalert-svc> 8880:8880
kubectl -n <ns> port-forward svc/<alertmanager-svc> 9093:9093
```

## 寫檔

1. 複製 `$AT/env/env.example.yaml` 的結構，寫到 `env/<name>.yaml`。`name` 用使用者給的名稱，或問一個不含公司與主機資訊的代號。
2. 每個 id 只用小寫英數與 `-`。之後 collect 的 `--xxx id=url` 要用同一個 id。
3. `observers[].scrapes` 列出它抓的每個元件 id；只能在被監控元件自己身上評估的，check_coverage 會判成 `self_only`。
4. 不知道的欄位不要猜；省略它，並寫進 `unknowns:`。
5. 寫完把檔案內容給使用者確認，並再說一次：**這個檔案不要 commit 到公開 repo。**

完成後回到 `selfcheck` 的階段 2。
