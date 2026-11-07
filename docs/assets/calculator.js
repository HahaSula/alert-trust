// 契約計算器：純前端，公式與 docs/contracts.md 相同。
(function () {
  "use strict";

  function parseDur(v) {
    if (v === undefined || v === null) return NaN;
    var s = String(v).trim();
    if (s === "") return NaN;
    if (/^\d+(\.\d+)?$/.test(s)) return parseFloat(s);
    var re = /(\d+(?:\.\d+)?)(ms|s|m|h|d|w)/g, m, total = 0, used = 0;
    var unit = { ms: 0.001, s: 1, m: 60, h: 3600, d: 86400, w: 604800 };
    while ((m = re.exec(s)) !== null) { total += parseFloat(m[1]) * unit[m[2]]; used += m[0].length; }
    return used === s.length ? total : NaN;
  }

  function fmt(sec) {
    if (!isFinite(sec)) return "—";
    if (sec < 120) return Math.round(sec * 10) / 10 + "s";
    var m = Math.floor(sec / 60), s = Math.round(sec % 60);
    return s ? m + "m" + s + "s" : m + "m";
  }

  function fromExpr(expr) {
    var out = {};
    var ranges = [], m, re = /\[(\d+[smhdw](?:\d+[smhdw])*)(?::[^\]]*)?\]/g;
    while ((m = re.exec(expr)) !== null) ranges.push(parseDur(m[1]));
    if (ranges.length) out.R = Math.min.apply(null, ranges);
    var offs = [], ro = /offset\s+(-?\d+[smhdw](?:\d+[smhdw])*)/g;
    while ((m = ro.exec(expr)) !== null) offs.push(parseDur(m[1]));
    if (offs.length) out.O = Math.min.apply(null, offs);
    out.hasRange = ranges.length > 0;
    return out;
  }

  function status(margin, warnBelow) {
    if (!isFinite(margin)) return ["未填", "na"];
    if (margin < 1) return ["不成立", "bad"];
    if (margin < (warnBelow || 1.25)) return ["接近", "warn"];
    return ["成立", "ok"];
  }

  function row(name, anchor, value, need, margin, st, note) {
    return "<tr><td><a href=\"../../contracts/#" + anchor + "\">" + name + "</a></td><td>" + value +
      "</td><td>" + need + "</td><td class=\"at-num\">" + (isFinite(margin) ? margin.toFixed(2) : "—") +
      "</td><td><span class=\"at-chip at-res-" + st[1] + "\">" + st[0] + "</span></td><td>" + (note || "") + "</td></tr>";
  }

  function val(form, name) { var el = form.querySelector("[name=" + name + "]"); return el ? el.value : ""; }

  function calcPrometheus(form) {
    var S = parseDur(val(form, "S")), d = parseDur(val(form, "d")), E = parseDur(val(form, "E")),
      T = parseDur(val(form, "T")), F = parseDur(val(form, "F")) || 0, k = parseInt(val(form, "k"), 10) || 0,
      lb = parseDur(val(form, "lookback")) || 300, resend = parseDur(val(form, "resend")) || 60,
      target = parseDur(val(form, "target"));
    var R = parseDur(val(form, "R")), O = parseDur(val(form, "O")) || 0;
    var ex = fromExpr(val(form, "expr"));
    if (ex.R !== undefined) R = ex.R;
    if (ex.O !== undefined) O = ex.O;
    var I = d > S ? Math.ceil(d / S) * S : S;
    var Ep = T > E ? Math.ceil(T / E) * E : E;
    var needR = (2 + k) * I + (O >= d ? 0 : d);
    var rows = [];
    rows.push(row("Scrape 在 interval 內完成", "scrape-within-interval", "d = " + fmt(d), "≤ S = " + fmt(S), S / d, status(S / d, 1.25),
      d > S ? "實際間隔 I ≈ " + fmt(I) : ""));
    rows.push(row("視窗內至少 2 個可見樣本", "range-window", "R = " + fmt(R) + (O ? "，O = " + fmt(O) : ""),
      "≥ " + fmt(needR) + (O >= d ? "（O ≥ d）" : "（含 d）"), R / needR, status(R / needR, 1.25),
      O < d && O > 0 ? "offset 小於 d，仍要多涵蓋 d" : ""));
    rows.push(row("Rule group 在 interval 內跑完", "group-within-interval", "T = " + fmt(T), "≤ 0.8 × E = " + fmt(0.8 * E),
      (0.8 * E) / T, status(E / T, 1.25), T > E ? "實際 evaluation 間隔 ≈ " + fmt(Ep) : ""));
    rows.push(row("Lookback（不用 range 的 selector）", "lookback", "I + d = " + fmt(I + d), "< " + fmt(lb), lb / (I + d),
      status(lb / (I + d), 1.25), ex.hasRange ? "這條表達式有 range，主要看上一列" : ""));
    var endsAt = 4 * Math.max(resend, E);
    rows.push(row("續送中斷短於 EndsAt", "resend-endsat", "T = " + fmt(T), "遠小於 " + fmt(endsAt), endsAt / T,
      status(endsAt / T, 2), "查詢中斷超過 " + fmt(endsAt) + " 會假 resolved"));
    var lat = I + Math.max(O, d) + Ep + T + F;
    return { rows: rows, latency: lat, target: target };
  }

  function calcVmalert(form) {
    var I = parseDur(val(form, "vI")), L = parseDur(val(form, "vL")), D = parseDur(val(form, "vD")),
      E = parseDur(val(form, "vE")), T = parseDur(val(form, "vT")), resend = parseDur(val(form, "vresend")) || 0,
      outage = parseDur(val(form, "voutage")), F = parseDur(val(form, "vF")) || 0, target = parseDur(val(form, "vtarget"));
    var R = parseDur(val(form, "vR"));
    var ex = fromExpr(val(form, "vexpr"));
    if (ex.R !== undefined) R = ex.R;
    if (!isFinite(D)) D = 30;
    var needR = Math.max(I + L - D, 0.001);
    var rows = [];
    rows.push(row("vmalert 視窗", "vm-range-window", "R = " + fmt(R), "≥ I + L − D = " + fmt(needR), R / needR, status(R / needR, 1.25), ""));
    if (isFinite(T)) rows.push(row("Group 在 interval 內跑完", "group-within-interval", "T = " + fmt(T), "≤ 0.8 × E = " + fmt(0.8 * E),
      (0.8 * E) / T, status(E / T, 1.25), "查詢飽和時 iteration_missed 可能仍為 0"));
    var endsAt = 4 * Math.max(E, resend);
    rows.push(row("續送中斷短於 EndsAt", "resend-endsat", "可能中斷 " + fmt(outage), "< " + fmt(endsAt), endsAt / outage,
      status(endsAt / outage, 1.25), "Watchdog 所在的 vmalert 不要設 resendDelay"));
    var logSt, logMargin = D / L;
    if (!isFinite(L)) logSt = ["未填", "na"];
    else if (L <= D) logSt = ["成立", "ok"];
    else if (L <= D + E) logSt = ["看相位", "warn"];
    else logSt = ["必定漏", "bad"];
    rows.push(row("Log rule 延遲（無 _time）", "vl-log-delay", "L = " + fmt(L), "≤ D = " + fmt(D) + "；> D + E 必漏", logMargin, logSt,
      "只適用 type: vlogs 且沒寫 _time 的 rule"));
    var lat = I + Math.max(L, D) + E + (isFinite(T) ? T : 0) + F;
    return { rows: rows, latency: lat, target: target };
  }

  function render(root) {
    var form = root.querySelector("form");
    var out = root.querySelector(".at-calc-out");
    var mode = form.querySelector("[name=mode]:checked").value;
    root.querySelectorAll("[data-mode]").forEach(function (el) { el.hidden = el.getAttribute("data-mode") !== mode; });
    var r = mode === "prometheus" ? calcPrometheus(form) : calcVmalert(form);
    var latTxt = fmt(r.latency);
    var latNote = "";
    if (isFinite(r.target) && isFinite(r.latency)) {
      latNote = r.latency <= r.target ? "，在通知時限 " + fmt(r.target) + " 內" : "，<strong>超過通知時限 " + fmt(r.target) + "</strong>";
    }
    out.innerHTML = "<div class=\"at-table-wrap\"><table><thead><tr><th>契約</th><th>現況</th><th>需要</th><th>邊際</th><th>狀態</th><th>說明</th></tr></thead><tbody>" +
      r.rows.join("") + "</tbody></table></div>" +
      "<p>偵測延遲上限約 <strong>" + latTxt + "</strong>（推算值，未含 Alertmanager 的 group_wait 與 group_interval）" + latNote + "。</p>";
  }

  function init() {
    document.querySelectorAll(".at-calc").forEach(function (root) {
      if (root.dataset.ready) return;
      root.dataset.ready = "1";
      var form = root.querySelector("form");
      form.addEventListener("input", function () { render(root); });
      form.addEventListener("submit", function (e) { e.preventDefault(); render(root); });
      render(root);
    });
  }

  if (typeof window !== "undefined") {
    if (window.document$ && window.document$.subscribe) window.document$.subscribe(init);
    else if (document.readyState !== "loading") init();
    else document.addEventListener("DOMContentLoaded", init);
    window.alertTrustCalc = { parseDur: parseDur, fromExpr: fromExpr };
  }
  if (typeof module !== "undefined") module.exports = { parseDur: parseDur, fromExpr: fromExpr };
})();
