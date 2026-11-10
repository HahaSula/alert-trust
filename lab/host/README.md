# 主機與容器層：記憶體接近 limit、OOM kill、唯讀檔案系統

驗證[主機與容器資源卡](../../catalog/components/host.yaml)與 [`rules/host/meta.yml`](../../rules/host/meta.yml) 裡三個訊號：
[container-memory-limit](../../docs/components/host.md#container-memory-limit)、[oom-kills](../../docs/components/host.md#oom-kills)、
[filesystem-readonly](../../docs/components/host.md#filesystem-readonly)。不用 privileged 容器。

結果與數字：[results/summary.md](results/summary.md)。

## 需要

- Docker 與 docker compose v2、Python 3（只用標準函式庫）
- 映像檔：`prom/prometheus:v3.13.5`、`ghcr.io/google/cadvisor:v0.60.6`、`prom/node-exporter:v1.12.1`、`python:3.12.7-alpine`
- 主機 port：34090（Prometheus）、34091（node_exporter）、34092（cAdvisor）
- 記憶體：每個測試容器 64 MiB，加上三個元件約 300 MB

## 怎麼跑

```bash
docker compose up -d prometheus node-exporter cadvisor
python3 record.py results/baseline.jsonl 1 1                    # 情境 3 在啟動 1 分鐘後就 firing
docker compose --profile mem up -d memhog                        # 情境 1a／1b
python3 record.py results/timeline.jsonl 660 10 &                # 每 10 秒記一次
docker compose --profile oom up -d oomhog                        # 情境 2，約 30 秒後啟動
docker compose --profile mem stop memhog
docker compose --profile mem2 up -d memhog2                      # 情境 1c
python3 record.py results/timeline-2.jsonl 480 10
docker compose --profile mem --profile mem2 --profile oom down -v
```

全程約 15 分鐘。`rules/host/meta.yml` 以唯讀掛進 Prometheus；改了規則後 `docker kill -s HUP at-host-prometheus` 重載。

## 組成

| 檔案 | 內容 |
|---|---|
| `compose.yml` | compose project `at-host`：Prometheus、node_exporter、cAdvisor，與三個測試容器（profile `mem`、`mem2`、`oom`） |
| `prometheus.yml` | job `node`、`cadvisor`，`scrape_interval`／`evaluation_interval` 5s，載入 `rules/host/meta.yml` |
| `memhog.py` | 慢慢配記憶體到目標（`memory.current` 或 memory.stat 的 `anon`）後停住 |
| `record.py` | 每 10 秒查一次 Prometheus：working set、limit、比值、OOM counter、`node_vmstat_oom_kill`、`node_filesystem_readonly`、rule 狀態 |

## 情境

| | 做法 | 看什麼 |
|---|---|---|
| 1a／1b | memhog：memory.current 到 55 MiB 停 150s，再到 60 MiB | working set／limit；ContainerMemoryNearLimit（原式） |
| 1c | memhog2：anon 到 61 MiB 停 420s | ContainerMemoryNearLimit（修正後）pending → firing |
| 2 | oomhog：配到 100 MiB（limit 64 MiB），`restart: on-failure:2` | `container_oom_events_total`、`node_vmstat_oom_kill`；ContainerOOMEvent、HostOOMKill |
| 3 | named volume 以 `:ro` 掛進 node_exporter 的 `/data-ro`，`/data-rw` 當對照 | `node_filesystem_readonly`；HostFilesystemReadOnly |

## 結果

| 訊號 | 結果 |
|---|---|
| `container_memory_working_set_bytes`、`container_spec_memory_limit_bytes` | 讀得到；limit 67108864；ratio 0.83（55 MiB）、0.90（60 MiB，跨在門檻上）、0.97（anon 61 MiB） |
| ContainerMemoryNearLimit | 原式在獨立 cAdvisor 上永遠是空的（沒有 `container` label）；修正後 pending 約 5 分鐘後 firing |
| `node_vmstat_oom_kill`／HostOOMKill | 3 次 OOM kill，25 → 28；firing |
| `container_oom_events_total`／ContainerOOMEvent | 一直是 0；cAdvisor 收到事件但路徑對不上（WSL2 的 cgroup 巢狀），沒有觸發 |
| `node_filesystem_readonly`／HostFilesystemReadOnly | `/data-ro` 1、`/data-rw` 0；firing |

## 和元件卡不同的地方

- 原本的 container-resources 規則都用 `container!=""`。這個 label 只有 kubelet 的 `/metrics/cadvisor` 有；獨立 cAdvisor 只有 `id`、`name`、`image`。
  已在 `rules/host/meta.yml` 的三條容器規則加上用 `id` 選 Docker cgroup 的第二段（ContainerCPUThrottled 沒有在 lab 觸發，只依同一個 label 事實修改）。
- 用 working set 判斷時，page cache 會把 memory.current 推到 limit，working set 會在門檻附近上下；門檻離 limit 太近時規則會反覆 pending。

## 限制

- 單機、單次。Docker Desktop on WSL2：cAdvisor 的 docker factory 需要 containerd socket，從容器裡連不到，
  所以用 raw factory（`--raw_cgroup_prefix_whitelist=/docker/`）收 `/docker/<id>`，series 只有 `id` label，沒有 `name`、`image`。
- cAdvisor 用 `cgroup: host`（host cgroup namespace，不是 privileged）與 `/dev/kmsg` device；把 `/sys` bind mount 進去在 Docker Desktop 上看到的是 WSL distro 的 cgroup 樹，沒有容器。
- 容器時鐘不準：只看數值與狀態，不比秒數。
- `container_oom_events_total` 在這台機器上量不到，結論維持讀碼。kube-state-metrics 的 OOMKilled 沒有 K8s，沒測。
- 唯讀只測「掛載時就是唯讀」；錯誤後 remount 唯讀需要 privileged，沒有測。
