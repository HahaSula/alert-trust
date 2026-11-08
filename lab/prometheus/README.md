# 故障注入實驗環境（Prometheus）

> 從前身 repo prometheus-selfcheck-skills 搬來（commit b8601c5），結果與 `EVIDENCE.md` 保持原樣，alert-trust 的參考連到這裡。
> **這批包含重度實驗**：CPU／記憶體邊際、寫滿 tmpfs、製造 OOM、IOPS 限制需要特權容器與 8 核 16GB 以上。依 `AGENTS.md`，本機只做輕量實驗；重跑這些情境請用獨立的機器。
> 部分腳本的路徑（例如 `k8s_round2.py` 的 skills 目錄參數）指向舊 repo 的結構，重跑前要調整。

一個隔離的 Docker 環境，用來量測 Prometheus 在各種故障下的訊號：受測 Prometheus、外部 meta Prometheus、Alertmanager、通知接收端、cAdvisor。

```text
target ──scrape──► 受測 Prometheus ──alert──► Alertmanager ──webhook──► 接收端
                         ▲                                                ▲
                         └──────────── 外部 meta Prometheus ──────────────┘
```

需要 Docker 和 Python 3，建議至少 8 核、16GB。所有容器都帶 `mom-lab=1` label。

| 指令 | 情境 |
|---|---|
| `python3 scenarios.py blind` | Target、Alertmanager、Prometheus 停機 |
| `python3 scenarios.py cpuknee` | CPU 逐步降低，量偵測延遲 |
| `python3 scenarios.py memknee` | 記憶體逐步壓低，加非正常重啟 |
| `python3 scenarios.py iops2` | 寫入 IOPS 限制 |
| `python3 scenarios.py query2` | 查詢飽和 |
| `python3 scenarios.py disk251` | 磁碟完全寫滿（也有 disk255、disk313、disk315） |
| `python3 scenarios.py diskgap251` | 空間被 compaction 用完（也有 diskgap313） |
| `python3 scenarios.py pend251` | Pending 抖動（也有 pend255、pend313、pend315） |
| `python3 window_matrix.py <名稱> <image>` | Range、offset、延遲矩陣 |
| `python3 upgrade_tests.py`、`rollback_check.py` | 跨版本回退與 alert 恢復 |
| `python3 compare_versions.py` | 相同負載的版本資源比較 |
| `python3 blackbox_test.py` | Blackbox 探測在各種故障下看到什麼 |
| `python3 k8s/k8s_round2.py <skills 目錄> <chart.tgz>` | K8s 驗證，需要 minikube、helm，以及 kube-prometheus-stack 57.2 的 chart |

清除：

```bash
docker rm -f $(docker ps -aq --filter label=mom-lab=1)
docker network prune -f --filter label=mom-lab=1
```

**注意：**
- IOPS 情境直接寫 cgroup `io.max`，需要特權容器，裝置寫死 `8:0`，cgroup 路徑假設是 cgroupfs driver（`/cg/docker/<id>`）。其他環境要調整。
- 實驗會寫滿 tmpfs 並製造 OOM，請勿在有其他工作的機器上執行。
- 部分腳本的路徑與時間是為了當次實驗設定，不是通用工具。
- 結果在 [`results/`](results/)，時間是單次量測。第 1 次 `iops`、第 1 次 `query` 的設定有缺陷，已由 `iops2`、`query2` 取代。
