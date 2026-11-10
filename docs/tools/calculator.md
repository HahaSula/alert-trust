# 契約計算器

代入你的 interval 與尖峰耗時，看 rule 的[信任契約](../contracts.md)還剩多少邊際。可以直接貼上 rule 的表達式，計算器會抓出最短的 range 和 offset。

**要用尖峰值，不要用設定值。**`d` 用 `max_over_time(scrape_duration_seconds[7d])`，`T` 用 `max_over_time(prometheus_rule_group_last_duration_seconds[7d])`。時間可以寫 `30`、`30s`、`2m`、`1h30m`。

<div class="at-calc" markdown="0">
<form autocomplete="off">
  <fieldset class="at-mode">
    <legend>Ruler</legend>
    <label><input type="radio" name="mode" value="prometheus" checked> Prometheus</label>
    <label><input type="radio" name="mode" value="vmalert"> vmalert</label>
  </fieldset>

  <div data-mode="prometheus" class="at-grid">
    <label>S：scrape interval<input name="S" value="2m"></label>
    <label>d：尖峰 scrape 耗時<input name="d" value="100s"></label>
    <label>E：evaluation interval<input name="E" value="30s"></label>
    <label>T：尖峰 group 耗時<input name="T" value="5s"></label>
    <label>R：range<input name="R" value="5m"></label>
    <label>O：offset<input name="O" value="0"></label>
    <label>F：for<input name="F" value="3m"></label>
    <label>k：容忍 scrape 失敗次數<input name="k" value="1" inputmode="numeric"></label>
    <label>lookback<input name="lookback" value="5m"></label>
    <label>resend-delay<input name="resend" value="1m"></label>
    <label>通知時限（選填）<input name="target" value=""></label>
    <label class="at-wide">Rule 表達式（選填，會覆蓋 R 與 O）<textarea name="expr" rows="2" placeholder='rate(my_metric_total{job="my-job"}[5m]) > 100'></textarea></label>
  </div>

  <div data-mode="vmalert" class="at-grid" hidden>
    <label>I：實際資料間隔<input name="vI" value="30s"></label>
    <label>L：資料可查詢延遲<input name="vL" value="60s"></label>
    <label>D：eval_delay<input name="vD" value="30s"></label>
    <label>E：group interval<input name="vE" value="30s"></label>
    <label>T：尖峰 group 耗時（選填）<input name="vT" value=""></label>
    <label>R：range<input name="vR" value="2m"></label>
    <label>k：容忍 scrape 失敗次數<input name="vk" value="1" inputmode="numeric"></label>
    <label>-datasource.queryStep<input name="vstep" value="5m"></label>
    <label>F：for<input name="vF" value="3m"></label>
    <label>-rule.resendDelay<input name="vresend" value="0"></label>
    <label>可能的查詢中斷<input name="voutage" value="8m"></label>
    <label>通知時限（選填）<input name="vtarget" value=""></label>
    <label class="at-wide">Rule 表達式（選填，會覆蓋 R）<textarea name="vexpr" rows="2" placeholder="rate(m[2m]) > 0"></textarea></label>
  </div>
</form>
<div class="at-calc-out" aria-live="polite"></div>
</div>

預設值是實驗用過的條件：scrape interval 2m、target 回應延遲 100s、`rate[5m]`、`for: 3m`、沒有 offset。這時視窗契約不成立，實驗中這條永遠成立的 alert 在 2.51.2、2.55.1、3.13.5、3.15.1 跑 25 分鐘都沒有 firing。把 offset 填成 `2m`、k 填 `0`，就是實驗中能 firing 的寫法；要容忍一次 scrape 失敗（k = 1），range 還要拉到 `6m`。

## 公式從哪裡來

| 契約 | 公式 | 依據 |
|---|---|---|
| [Scrape 在 interval 內完成](../contracts.md#scrape-within-interval) | `d ≤ S` | 讀碼 |
| [視窗](../contracts.md#range-window) | `R ≥ (n+k)·I + max(0, d − O)`；n 依函式（`rate` 類 2、`*_over_time` 1），由表達式判斷 | 實測視窗矩陣 |
| [Group 耗時](../contracts.md#group-within-interval) | `T ≤ E`，預警 `0.8 × E` | 實測 |
| [續送](../contracts.md#resend-endsat) | 中斷 < `4 × max(resend, E) − P`，`P = E × (⌊resend ÷ E⌋ + 1)`（最差情況） | EndsAt 實測；最差情況讀碼 |
| [vmalert 視窗](../contracts.md#vm-range-window) | `R ≥ (n+k)·I + max(0, L − D)`；MetricsQL 用視窗前一個 sample 時 n = 1 | 實測 120 組合 |
| [Log 延遲](../contracts.md#vl-log-delay) | `L ≤ D`，`L > D + E` 必漏 | 實測 |

偵測延遲上限：Prometheus 為 `I + max(O, d) + E' + T + F`，vmalert 為 `I + max(L, D) + E + T + F`。這是推算的上限，不是保證值。公式只保證查詢不會回傳空，門檻太貼近實際值時 `rate` 仍可能被低估而抖動。

計算在瀏覽器內完成，不會送出任何資料。
