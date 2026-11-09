---
name: rule-review
description: Review someone's alerting rule files (rule.yml, VMRule, PrometheusRule). Always establishes the monitoring stack and checks the monitor-of-monitor coverage of the infrastructure FIRST, and only then checks per-rule timing contracts. Use whenever the user hands over rule files or asks "are these rules OK / can I trust them". 檢查別人的 rule 檔、審 alert 規則時使用：先確認監控體系與基礎設施的 monitor of monitor，再查時間契約。
when_to_use: 檢查 rule.yml、審 alert rule、這些規則能不能信、review PrometheusRule／VMRule、rule 時間契約
argument-hint: "<rule files...>"
---

# 檢查 rule 檔

**順序是固定的，不能跳步。**只看 rule 本身的時間契約，會漏掉更根本的問題：負責評估這些 rule 的 Prometheus 或 vmalert 自己變慢、掛掉、寫不進去時，沒有人知道。所以先確認監控基礎設施，再看 rule。

`$AT` 是這個 plugin 的根目錄（`${CLAUDE_PLUGIN_ROOT}`，沒有時用 `${CLAUDE_SKILL_DIR}/../..`）。直接讀 repo 的 AI 也照同樣的步驟走，見 `AGENTS.md` 的「檢查使用者的 rule 檔」。

## 全程規則

- 唯讀，不改使用者的檔案，除非對方要求。
- 數字標來源：量到的、使用者說的、推算的。
- 一次最多問 3 題；不知道就記「未知」繼續，最後列在「沒有確認的部分」。
- 結論要引用目錄：`$AT/catalog/` 的訊號 id、契約 id，以及網站連結 `https://hahasula.github.io/alert-trust/components/<卡片>/#<訊號>`。

## 第 1 步：確認監控體系（必做）

先回答這三個問題，答案寫進 `env/<name>.yaml`（格式見 `$AT/env/env.example.yaml`）。能從檔案判斷的先判斷，判斷不了才問。

| 問題 | 怎麼判斷 | 為什麼重要 |
|---|---|---|
| **評估這些 rule 的是 Prometheus 還是 vmalert？版本？** | `type: vlogs`、`eval_delay`、`concurrency` 是 vmalert；`query_offset`、`keep_firing_for` 是 Prometheus；VMRule／PrometheusRule CRD 名稱；問使用者 | 時間契約的公式不同：Prometheus `R ≥ (2+k)·I + d`，vmalert `R ≥ I + L − D`；Prometheus 2.53 以前沒有 `query_offset` |
| **資料從哪裡來？** | Prometheus 自己 scrape，或 vmagent → VictoriaMetrics（single／cluster），log 來自 VictoriaLogs | 決定要用哪些元件卡 |
| **有沒有外部觀察者？告警走哪台 Alertmanager？** | 問使用者 | 沒有外部觀察者時，monitor of monitor 全部只在自己身上 |

## 第 2 步：監控基礎設施的覆蓋（必做，先於時間契約）

1. 依第 1 步選元件卡：`$AT/catalog/components/<元件>.yaml`。Prometheus 體系看 `prometheus`、`alertmanager`、`observer`；VM 體系看 `vmagent`、`victoriametrics`、`vmalert`、`alertmanager`、`observer`，有 log 再加 `victorialogs`。
2. 把使用者的 rule 檔（以及外部觀察者的 rule 檔，如果有）轉成快照，跑覆蓋檢查：

```bash
python3 "$AT/tools/rules_to_snapshot.py" --out snapshot --ruler <受測 ruler id> <使用者的 rule 檔...> [--ruler <觀察者 id> <觀察者的 rule 檔...>]
python3 "$AT/tools/check_coverage.py" --env env/<name>.yaml --snapshot snapshot --out report
```

   能連到 ruler 時改用 `collect.py` 蒐集，並加 `--live` 確認規則的 selector 真的查得到資料。

3. **先報告第 1 級訊號的缺口**，例如 Prometheus 體系常見的：
   - Scrape 耗時接近 `scrape_interval`（`scrape_duration_seconds`；需要 federate 或在受測 Prometheus 上評估）
   - Rule group 跑不完（`prometheus_rule_group_last_duration_seconds` ÷ `prometheus_rule_group_interval_seconds`）、group 停止評估（`prometheus_rule_group_last_evaluation_timestamp_seconds`）
   - WAL 寫入失敗、送不到 Alertmanager、Watchdog 與外部 dead man's switch
   - VM 體系：vmalert 查詢錯誤、partial response、唯讀、`-rule.resendDelay`

   使用者無法提供觀察者的規則時，逐項問「這個訊號有沒有人在看、在哪台評估」，答案標成「使用者說的」。
4. 基礎設施的缺口沒有處理前，後面時間契約的結論要加上一句：「評估這些 rule 的 ruler 本身沒有被監控，以下結論在它變慢或掛掉時不成立。」

## 第 3 步：每條 rule 的時間契約

需要的數字：`scrape_interval`（S）、尖峰 scrape 耗時（d）、`evaluation_interval`（E）、尖峰 group 耗時（T）；vmalert 另外要 `eval_delay`（D）與資料延遲（L）。拿不到就問，或標成未知並給出「在什麼條件下會失效」。

- Prometheus：照 `$AT/skills/prometheus/SKILL.md` 第 2 節。
- vmalert／VictoriaLogs：照 `$AT/skills/victoriametrics/SKILL.md` 第 1、2 節。
- Alertmanager 的告警視窗：照 `$AT/skills/alertmanager/SKILL.md` 第 2 節。
- 資料庫 exporter 的規則：照 `$AT/skills/database-targets/SKILL.md`。

公式與名詞：`$AT/catalog/contracts.yaml`、`$AT/catalog/terms.yaml`，網站的信任契約頁與名詞頁。

## 第 4 步：規則本身的寫法

- `x == 0`、`x != 1` 這類資料消失時回傳空的寫法（`tools/validate.py` 的 presence lint 規則同樣適用）。
- `job=~` 的 selector 要和實際 job 名稱對得上（`check_coverage.py --live` 會判成「規則在但沒資料」）。
- 關鍵 alert 與昂貴的 recording rule 放在不同 group。

## 回報順序

1. 監控體系（第 1 步的答案與來源）
2. 基礎設施的 monitor of monitor 缺口（第 2 步，第 1 級優先）
3. 時間契約不成立的 rule（第 3 步）
4. 寫法問題（第 4 步）
5. 沒有確認的部分
