# `-dedup.minScrapeInterval` 對 vmalert 通知時間的影響

**結論**（VictoriaMetrics single v1.155.0、vmalert v1.155.0，S = 15s，dedup 30s 對照沒有 dedup）：

| 情況 | dedup 30s | 沒有 dedup | 對 `fit_contracts.py --notify-within` 的意思 |
|---|---|---|---|
| 持續的條件（target 一直回 500、值改變後不變） | 8 次 alert 時間都和沒有 dedup **相同**（差 0 秒） | — | 總時間**不加** dedup − S；公式維持 `S + d + E + max(L, D) + ⌈for/E⌉·E + group_wait` |
| 只持續一次 scrape 的失敗（1 個 `up = 0` sample） | 6 次有 4 次響、**2 次沒響** | 6 次都響 | 只持續一次 scrape 的條件在 dedup > S 時可能被去掉；靠單次失敗的 rule 不可靠，量 `up == 0` 次數也會少算 |

**機制**（原始碼）：VictoriaMetrics 在寫入與查詢時都做 dedup[^vm-dedup-query-time]，每個 dedup 區間（以 Unix 時間對齊）只留最後一個 sample[^vm-dedup-samples]，查詢範圍內最後一個 sample 一定保留[^vm-dedup-last-kept]。所以進行中的區間裡最新的 sample 一定看得到：持續的條件不會因為 dedup 變慢。單一的 0 sample 如果同一個區間後面還有一個 sample，而且那個 sample 在 vmalert 查詢的時間點（`ts = 現在 − eval_delay`）之前就已寫入，0 就被去掉，vmalert 看不到。

**這組數據**：每一種情況只跑 4–6 次，只能說明機制與量級，不是統計。

## 怎麼做

| 項目 | 設定 |
|---|---|
| 版本 | `victoriametrics/victoria-metrics:v1.155.0`、`victoriametrics/vmalert:v1.155.0`（`vm_app_version` 記在 `results/trials.jsonl` 最後幾行）、假 target `python:3.12.7-alpine` |
| 架構 | 一個假 target；兩台 VM single 各自 scrape 它（`scrape_interval` 15s、`scrape_timeout` 10s），一台 `-dedup.minScrapeInterval=30s`、一台沒有；各配一台 vmalert（`-notifier.blackhole`，其他用預設：`-rule.evalDelay` 30s） |
| rule | group `interval` 15s；`TDown: up{job="t"} == 0`、`TDownFor30s`（同一個條件加 `for: 30s`）、`TValue: lab_value{job="t"} > 0` |
| 試驗 `delay` | 在隨機時間點讓 target 回 500（失敗路徑）或把 `lab_value` 改成 1（值路徑），每秒查兩台 vmalert 的 `/api/v1/alerts`，記 alert 第一次出現的牆上時間；還原後等 alert 消失再做下一次。4 次 × 2 種 |
| 試驗 `blip` | 讓 target 回 500，看到沒有 dedup 的 VM 寫入第一個 `up = 0` 就馬上恢復（只失敗一次 scrape），之後 120 秒看兩台有沒有 `TDown`。6 次 |
| 時間 | `delay` 約 40 分鐘、`blip` 約 15 分鐘；port 37010–37014；compose project `at-vm-dedup-notify` |

```bash
cd lab/vm/dedup-notify
python3 run.py 4            # delay：down／value 各 4 次 → results/trials.jsonl
python3 run.py 6 blip       # blip：6 次單次失敗 → results/blips.jsonl
python3 summarize.py > results/summary.md
```

腳本結束時 `docker compose down -v`；中斷時手動跑 `docker compose -p at-vm-dedup-notify down -v`。跑完已確認沒有殘留容器與 volume。

## 結果

時間都是從改變 target 狀態算起的牆上秒數。「在 30s 區間的位置」是那個 sample 的時間戳 mod 30（dedup 區間以 Unix 時間對齊）。原始資料：`results/trials.jsonl`、`results/blips.jsonl`（每次試驗的兩台 VM export 出來的 sample 也在裡面），表格是 `results/summary.md`。

**持續的條件（delay）**

| # | 情況 | 第一個變化的 sample | 區間位置 | firing：dedup 30s | 沒有 dedup | `for: 30s` firing：dedup | 沒有 dedup |
|---|---|---|---|---|---|---|---|
| 1 | target 回 500 | 8.8 | 28.1 | 56.8 | 56.8 | 74.6 | 74.6 |
| 2 | 值 0 → 1 | 11.1 | 26.6 | 62.6 | 62.6 | — | — |
| 3 | target 回 500 | 5.3 | 25.2 | 53.8 | 53.8 | 86.6 | 86.6 |
| 4 | 值 0 → 1 | 6.3 | 23.8 | 57.5 | 57.5 | — | — |
| 5 | target 回 500 | 1.8 | 22.4 | 49.8 | 49.8 | 82.6 | 82.6 |
| 6 | 值 0 → 1 | 13.6 | 8.6 | 62.6 | 62.6 | — | — |
| 7 | target 回 500 | 1.8 | 22.2 | 49.8 | 49.8 | 82.6 | 82.6 |
| 8 | 值 0 → 1 | 10.1 | 8.5 | 58.8 | 58.8 | — | — |

- 8 次兩台的 alert 時間都相同。兩台 VM 對同一個 target 的 scrape 時間點相同（VictoriaMetrics 依 target 決定相位），所以兩台看到的是同一批 sample，差別只在 dedup。
- 第 6、8 次第一個 `lab_value = 1` 的 sample 在區間前段（8.5–8.6 秒），事後 dedup 那台 export 已經沒有它（換成 15 秒後同一區間的下一個），但 alert 時間沒有變：查詢當下它是區間裡最新的 sample。
- 失敗路徑的 4 次剛好都落在區間後段（22–28 秒），沒有直接量到「第一個 0 在區間前段」的情況；依上面的機制與第 6、8 次的值路徑，結果應該相同，這一點是**推論**。
- 從第一個變化的 sample 到 firing 是 47.5–51.5 秒，符合 E 15s + D 30s 再加上相位與這台機器的時鐘誤差（見下方限制）。

**只失敗一次 scrape（blip）**

| # | 0 sample | 區間位置 | dedup VM 事後還有 0 嗎 | `TDown`：dedup 30s | 沒有 dedup |
|---|---|---|---|---|---|
| 1 | 3.3 | 10.4 | 沒有 | **沒有響** | 54.5 |
| 2 | 1.7 | 21.2 | 有 | 53.0 | 53.0 |
| 3 | 10.6 | 2.1 | 沒有 | **沒有響** | 61.7 |
| 4 | 9.4 | 12.8 | 有 | 60.8 | 60.8 |
| 5 | 2.1 | 8.7 | 沒有 | 50.6 | 50.6 |
| 6 | 14.0 | 4.6 | 沒有 | 65.3 | 65.3 |

- 事後被 dedup 去掉的 0 有 4 次，其中 2 次 vmalert 仍然響了（查詢時下一個 sample 還不在查詢範圍內），2 次沒響。
- 第 4 次的 0 在區間 12.8 秒，下一個 sample 依 15s 間隔應該也在同一區間，0 卻保留下來；這台機器的容器時鐘讓實際 scrape 間隔約 16 秒（見限制），下一個 sample 可能落到下一個區間。這一次的判讀不確定。

## 限制

- 這台機器（Docker Desktop on WSL2）容器內的 monotonic 時鐘比牆上時鐘慢（`lab/vm/README.md` 的限制），dedup 那台 export 出來的 sample 間隔約 32.7 秒而不是 30 秒；秒數在正常機器上應該略短。兩台在同一台機器、同一段時間，比較兩者仍然成立。
- 只有 S = 15s、dedup = 30s（dedup = 2S）一組。dedup 更大時，單次失敗被去掉的機率應該更高，沒有量。
- 失敗用 HTTP 500 模擬（`up = 0` 立刻寫入）。target 沒有回應、要等 `scrape_timeout` 逾時的情況沒有量；那時 d = τ（`fit_contracts.py` 的失敗路徑）。
- 沒有 Alertmanager；只量到 vmalert 的 firing。

## 參考

[^vm-dedup-query-time]: `lib/storage/dedup.go#L9`（寫入與查詢時都 dedup）：https://github.com/VictoriaMetrics/VictoriaMetrics/blob/v1.155.0/lib/storage/dedup.go#L9（`catalog/refs.d/vm-dedup-notify.yaml`）
[^vm-dedup-samples]: `lib/storage/dedup.go#L30`（`DeduplicateSamples`：每個區間留最後一個）：https://github.com/VictoriaMetrics/VictoriaMetrics/blob/v1.155.0/lib/storage/dedup.go#L30
[^vm-dedup-last-kept]: `lib/storage/dedup.go#L71`（最後一個 sample 一定保留）：https://github.com/VictoriaMetrics/VictoriaMetrics/blob/v1.155.0/lib/storage/dedup.go#L71
