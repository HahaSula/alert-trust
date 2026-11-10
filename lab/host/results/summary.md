# lab/host 結果（2026-11-11，WSL2 + Docker Desktop）

版本：Prometheus v3.13.5、cAdvisor v0.60.6、node_exporter v1.12.1、測試容器 python:3.12.7-alpine（`mem_limit: 64m`、`memswap_limit: 64m`）。
`scrape_interval` 與 `evaluation_interval` 都是 5s。時間是主機牆鐘（`wall`）；容器時鐘在這台機器上不準，只看數值與狀態。

原始輸出：`timeline.jsonl`（memhog、oomhog，每 10 秒一筆）、`timeline-2.jsonl`（memhog2）、`baseline.jsonl`、`rules-final.json`、
`cadvisor-oom-events.json`、`kernel-oom-lines.txt`、`oomhog-inspect.txt`、`label-check.txt`、`run-log.txt`（操作時間）。

<a id="overview"></a>

## 一覽

| 情境 | metric 實測值 | rule | 結果 |
|---|---|---|---|
| 1a 記憶體 ~55 MiB（memory.current） | working set 55418880 B，ratio 0.826 | ContainerMemoryNearLimit（原式） | inactive（低於 0.9，原式的 selector 也是空的） |
| 1b 記憶體 60 MiB（memory.current） | working set 60694528 → 60043264 B，ratio 0.904 → 0.895 | ContainerMemoryNearLimit（原式） | inactive：`container!=""` 選不到任何 series |
| 1c anon 61 MiB（memhog2） | working set 65511424 → 65052672 B，ratio 0.976 → 0.969 | ContainerMemoryNearLimit（修正後） | 01:12:40 pending → 01:17:45 firing |
| 2 OOM（oomhog，配到 100 MiB） | 核心 OOM kill 3 次；`node_vmstat_oom_kill` 25 → 28；`container_oom_events_total` 一直是 0 | HostOOMKill | 01:05:16 firing（increase 2.07，下一筆 3.10） |
| | | ContainerOOMEvent | inactive（cAdvisor 有收到事件，但 counter 對不到 series，見下） |
| 3 唯讀掛載 | `node_filesystem_readonly{mountpoint="/data-ro",fstype="ext4"} 1`；對照 `/data-rw` 為 0 | HostFilesystemReadOnly | firing（for 1m 後；啟動後第一筆 baseline 01:04:25 已 firing） |

<a id="memory"></a>

## 情境 1：記憶體接近 limit

- `container_spec_memory_limit_bytes` = 67108864（64 MiB），與 `mem_limit` 一致。
- 1a／1b 用 `memory.current` 當目標：page cache 會一路長到 limit（memory.current 最後 67067904），核心回收 file page，
  working set（usage − inactive_file）在 0.895–0.904 之間，剛好跨在 0.9 門檻上，不適合當測試點。
- 原式 `container_memory_working_set_bytes{job=~".*(cadvisor|kubelet).*", container!=""}` 在獨立 cAdvisor 上回傳空
  （`timeline.jsonl` 的 `ratio_rule_selector` 每一筆都是 `[]`；`label-check.txt`：`{job="cadvisor", container!=""}` 沒有任何 metric）。
  獨立 cAdvisor 的 label 只有 `id`、`name`（第一個 alias）、`image` 與 `container_label_*`，`container` 是 kubelet 加的。
  所以原式在 ratio 0.904 時仍是 inactive，不是因為門檻。
- 01:09:02 修正 `rules/host/meta.yml`（加上用 `id=~"/docker/.+|/system.slice/docker-.+"` 選的第二段）並 SIGHUP 重載。
- 1c 改用 memory.stat 的 `anon` 當目標（61 MiB，anon 實際 64565248 B）：working set 0.969–0.976，修正後的規則 pending 約 5 分鐘後 firing。
- 這組數據裡 cAdvisor 的值有時十幾秒到幾分鐘不變（例如 55418880 持續到 01:08:17）；看值，不看秒數。

<a id="oom"></a>

## 情境 2：OOM kill

- oomhog 每 0.2 秒配 2 MiB，目標 100 MiB；`restart: on-failure:2`，共被殺 3 次，最後 `status=exited OOMKilled=true exit=137 restarts=2`。
- 核心訊息（`kernel-oom-lines.txt`）：`oom-kill:constraint=CONSTRAINT_MEMCG,...,oom_memcg=/wsl-user/distro-11901/docker/<id>,task_memcg=/wsl-user/distro-11901/docker/<id>`。
- `node_vmstat_oom_kill`：25 → 27 → 28；`increase(...[10m])` 2.07 → 3.10，HostOOMKill 立即 firing（沒有 `for`）。
  `/proc/vmstat` 不分 namespace，node_exporter 不掛主機 /proc 也讀得到整台（Docker VM 核心）的計數。
- `container_oom_events_total{id="/docker/<oomhog-id>"}` 一直是 0，ContainerOOMEvent 沒有觸發。
  cAdvisor 的事件 API 有 3 筆 `oomKill`（`cadvisor-oom-events.json`），`container_name` 是核心給的
  `/wsl-user/distro-11901/docker/<id>`；cAdvisor 在 host cgroup namespace 看到的容器名稱是 `/docker/<id>`。
  counter 以 OOM 訊息裡的路徑為 key、以容器名稱查（`cmd/internal/api/oom.go` 第 72、95 行），兩者不同就永遠是 0。
  這是這台機器的 cgroup 巢狀（WSL2 distro 底下的 Docker Desktop）造成的，一般 Linux 主機上 cAdvisor 看到的路徑通常與核心一致，但這裡沒有驗證。
- oomhog 結束後，它的 series 在 cAdvisor 消失；Prometheus 裡約 5 分鐘後才查不到（01:10:26，cAdvisor 輸出帶時間戳，沒有 stale marker）。
  沒有重啟的容器被 OOM kill 時，`increase()` 能不能看到最後一次增加取決於 kill 與消失之間有沒有被 scrape 到。

<a id="readonly"></a>

## 情境 3：掛載時就是唯讀

- compose 的 named volume 以 `:ro` 掛進 node_exporter 的 `/data-ro`，另一個以讀寫掛在 `/data-rw`；兩者 fstype 都是 ext4（Docker VM 的磁碟）。
- node_exporter 沒有掛主機 rootfs，filesystem collector 讀的是它自己容器的 mount table。
- `node_filesystem_readonly`：`/data-ro` 1、`/data-rw` 0；HostFilesystemReadOnly 對 `/data-ro` firing，`/data-rw` 沒有。
- 只測了「掛載時就是唯讀」。ext4 因錯誤 remount 成唯讀需要 privileged，沒有測；node_exporter 讀的是同一個 mount option（`ro`），但路徑不同。
