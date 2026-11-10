#!/usr/bin/env python3
"""One-shot review of someone's rule files: inventory → metric sources → lint → time contracts (→ coverage).

  python3 tools/review.py --rules user/*.yml [--config prometheus.yml] [--config-kind prometheus|vmagent] \
      [--alertmanager alertmanager.yml] [--answers env/<name>.sources.yaml] [--merge env/<name>.inventory.yaml] \
      [--env env/<name>.yaml [--tiers 1,2]] --out report/

Runs, in this order, every tool on the same inventory (each reads it with --inventory, nothing is parsed twice):

  1. tools/inventory.py            → <out>/inventory.yaml, inventory.md   (copy it to env/<name>.inventory.yaml
                                     and edit it; pass it back with --merge so edits are kept)
  2. tools/check_metric_sources.py → <out>/metric-sources.md, .json
  3. tools/validate.py --lint-rules → <out>/lint.md
  4. tools/fit_contracts.py        → <out>/contracts-fit.md, .json   (uses metric-sources.json; no S / E needed)
  5. tools/check_coverage.py       → <out>/coverage/coverage.md       (only with --env)

and writes <out>/summary.md: 「建議先處理的 3 件事」 ranked as
  1. rules that never fire / always fire (job, label, dropped metric, absent() on nothing)
  2. the monitoring stack itself: no Watchdog / dead man's switch, the ruler or Alertmanager not scraped
  3. critical alerts that will not page (route label no rule has, only email / empty receivers)
  4. time contracts not met (environment, rule windows)
then the inventory to confirm, the assumptions, and at most 3 next questions (ranked by tools/inventory.py).
Everything is advice (建議 / 代價), not a verdict. Exit code 0 always (CI: use the single tools).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_coverage as CC  # noqa: E402
import check_metric_sources as MS  # noqa: E402
import fit_contracts as FC  # noqa: E402
import inventory as INV  # noqa: E402
import validate as VAL  # noqa: E402


def _quiet(fn, argv) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            rc = fn(argv)
        except SystemExit as e:  # argparse errors
            rc = e.code if isinstance(e.code, int) else 2
    return rc, buf.getvalue()


def _names(xs, n=6) -> str:
    xs = list(dict.fromkeys(xs))
    return "、".join(f"`{x}`" for x in xs[:n]) + (f" 等 {len(xs)} 條" if len(xs) > n else "")


def run_inventory(a) -> dict:
    inv_path = os.path.join(a.out, "inventory.yaml")
    argv = ["--rules", *a.rules, "--out", inv_path, "--md", os.path.join(a.out, "inventory.md")]
    for flag, v in (("--config", a.config), ("--config-kind", a.config_kind), ("--alertmanager", a.alertmanager),
                    ("--answers", a.answers), ("--merge", a.merge)):
        if v:
            argv += [flag, v]
    _quiet(INV.main, argv)
    return INV.C._load(inv_path)


def lint(rules: list[str], out: str) -> tuple[list[str], list[str]]:
    errors, advice = VAL.lint_rules(VAL.load_rule_files(rules))
    with open(os.path.join(out, "lint.md"), "w", encoding="utf-8") as f:
        f.write("# 寫法檢查（validate.py --lint-rules）\n\n")
        f.write("\n".join(f"- 建議：{x}" for x in advice) + ("\n" if advice else ""))
        f.write("\n".join(f"- 引用錯誤：{x}" for x in errors) + ("\n" if errors else ""))
        if not advice and not errors:
            f.write("- 沒有建議。\n")
    return errors, advice


# ---------- ranking ----------

def item_firing(inv: dict, ms_rows: list[dict]) -> dict | None:
    never = [r["name"] for r in inv["rules"] if (r.get("fires") or {}).get("value") == "never"]
    always = [r["name"] for r in inv["rules"] if (r.get("fires") or {}).get("value") == "always"]
    always += [r["rule"] for r in ms_rows if r["status"] == "always_fires" and r["rule"] not in always]
    bad = {"job_mismatch", "label_mismatch", "dropped"}
    never += [r["rule"] for r in ms_rows if r["status"] in bad and r["rule"] not in never and r["rule"] not in always
              and all(x["status"] in bad | {"builtin", "recording"} for x in ms_rows if x["rule"] == r["rule"])
              and any(x["status"] in bad for x in ms_rows if x["rule"] == r["rule"])]
    if not never and not always:
        return None
    f = inv.get("findings_from_files") or []
    jobs = [x for x in f if x["kind"] == "job_not_in_config"]
    why = []
    for x in jobs:
        why.append(f"{x['job']} 在 config 裡沒有" + (f"（最接近的是 `{x['closest']}`）" if x.get("closest") else ""))
    labels = [x for x in f if x["kind"] == "label_not_produced"]
    if labels:
        why.append(f"{len(labels)} 個 selector 要的 label 不存在")
    drops = [x for x in f if x["kind"] == "dropped_metric_used"]
    if drops:
        why.append(f"{len(drops)} 個 metric 被 metric_relabel_configs 丟掉")
    parts = []
    if never:
        parts.append(f"{len(set(never))} 條 rule 永遠不會響（{_names(never)}）")
    if always:
        parts.append(f"{len(set(always))} 條一直在響（{_names(always)}，`absent()` 裡的 selector 選不到任何 series）")
    fix = ("把 rule 的 job 條件改成 config 裡實際的 job"
           + (f"（例如 `job=\"{jobs[0]['closest']}\"`）" if jobs and jobs[0].get("closest") else "")
           + "，或在 scrape config 用 relabel 改名；label、被丟掉的 metric 見 metric-sources.md") if jobs else \
        "照 metric-sources.md 修正 label 或被丟掉的 metric"
    return {"key": "firing", "title": "；".join(parts), "why": "；".join(why) or "selector 一定是空的",
            "advice": fix, "cost": "改 rule 要一起改用同一個 job 的 dashboard；改 job 名稱會讓既有 series 換名、歷史查詢斷開；"
                                   "一直在響的 alert 若已被 silence，修好後要解除",
            "ref": "metric-sources.md、inventory.md"}


def item_stack(inv: dict, cov: list[dict] | None) -> dict | None:
    f = inv.get("findings_from_files") or []
    parts, advice = [], []
    dms_route = any(x["kind"] == "dms_route_without_rule" for x in f)
    if any(x["kind"] == "no_watchdog_rule" for x in f) and not dms_route:
        parts.append("沒有 Watchdog／dead man's switch")
        advice.append("加一條一直在響的 `Watchdog`（`vector(1)`），在 Alertmanager 用獨立 route 送到外部 dead man's switch")
    for x in f:
        if x["kind"] == "dms_route_without_rule":
            parts.append(f"Alertmanager 有 Watchdog 的 route（→ {x['receiver']}），但給的 rule 檔沒有 Watchdog")
            advice.append("確認 Watchdog 在哪個檔案、有被這台 ruler 載入；沒有就加上")
    un = [u for u in (inv.get("unscraped_components") if isinstance(inv.get("unscraped_components"), list) else [])]
    if un:
        parts.append("沒有被 scrape：" + "、".join(u.get("target") or u["component"] for u in un))
        advice.append("為 " + "、".join(sorted({u["component"] for u in un})) + " 加 scrape job（最好由另一台外部觀察者抓）")
    for x in f:
        if x["kind"] == "config_missing":
            parts.append(x["fact"])
    if cov:
        miss = [i for i in cov if i.get("tier") == 1 and i["status"] in CC.ACTION and i.get("signal") in CC.DMS_SIGNALS]
        if miss and not any("Watchdog" in p for p in parts):
            parts.append("覆蓋檢查：" + "、".join(f"{i['component']} {i['signal']}（{CC.STATUS_TEXT[i['status']]}）" for i in miss[:4]))
    if not parts:
        return None
    return {"key": "stack", "title": "監控體系本身停了沒有人知道：" + "；".join(parts),
            "why": "ruler、Alertmanager 或通知路徑停掉時，所有 rule 一起沉默，沒有任何 alert 會說出來",
            "advice": "；".join(advice) or "見 coverage.md",
            "cost": "多一個外部 dead man's switch 服務（或一台觀察者）要維護；Watchdog 的 route 要排在最前面、不要被 inhibit",
            "ref": "inventory.md" + ("、coverage/coverage.md" if cov else "")}


def item_paging(inv: dict) -> dict | None:
    f = inv.get("findings_from_files") or []
    crit = {r["name"] for r in inv["rules"] if r.get("kind") == "alert" and
            INV._sev_critical((r.get("labels_out") or {}).get("severity"))}
    parts, rules = [], []
    for x in f:
        if x["kind"] == "route_label_missing" and crit & set(x["rules"]):
            hit = sorted(crit & set(x["rules"]))
            rules += hit
            parts.append(f"route 要 `{x['label']}`，{len(hit)} 條 critical rule {'都沒有' if x['certain'] else '的 config 與 rule 都沒加（除非 exporter 自己輸出）'}，"
                         f"會送到 {'、'.join(x['fallback'])}")
    values = []
    for x in f:
        if x["kind"] == "route_label_value_mismatch" and crit & set(x["rules"]):
            hit = sorted(crit & set(x["rules"]))
            rules += hit
            values.append(x)
            parts.append(f"route（→ {x['receiver']}）要的 `{x['label']}` 值和 {len(hit)} 條 critical rule 的 {'、'.join(x['values'])} 不同，"
                         f"會送到 {'、'.join(x['fallback'])}")
    for x in f:
        if x["kind"] == "critical_not_paging":
            extra = [n for n in x["rules"] if n not in rules]
            if extra:
                rules += extra
                parts.append(f"{len(extra)} 條 critical rule 只會送到 {'、'.join(x['receivers'])}（{_names(extra, 4)}）")
            elif parts:
                kinds = {r["name"]: ("空 receiver" if r["empty"] else "email") for r in (inv.get("alertmanager") or {}).get("receivers") or []}
                parts[-1] += "（" + "、".join(f"{r} 是{kinds.get(r, 'email')}" for r in x["receivers"]) + "，不會 page）"
    covered = set(rules)
    for x in f:
        hit = [n for n in x.get("rules") or [] if n in crit and n not in covered] if x["kind"] == "routed_to_empty_receiver" else []
        if hit:
            rules += hit
            parts.append(f"{len(hit)} 條 critical rule 會送到空的 receiver {x['receiver']}" + ("" if x["certain"] else "（依 runtime label）"))
    if not parts:
        return None
    labels = sorted({x["label"] for x in f if x["kind"] == "route_label_missing" and crit & set(x["rules"])})
    adv = []
    if labels:
        adv.append(f"讓 alert 帶 {'、'.join('`' + l + '`' for l in labels)}：沒有聚合的 rule 在 target 加 label（`static_configs` labels 或 "
                   "`relabel_configs`），聚合的 rule 在 `by (...)` 保留或在 rule 的 `labels` 寫死；或把 route 改成這些 rule 有的 label")
    for x in values:
        adv.append(f"把 route 的 `{x['label']}` 條件改成實際的值（{'、'.join(x['values'])}），或用 relabel 改成 route 要的值")
    adv = "；".join(adv) or "把 critical 的 route 指到會 page 的 receiver"
    return {"key": "paging", "title": "critical alert 不會 page：" + "；".join(parts),
            "why": "Alertmanager 依 alert 的 label 選 route；需要的 label 不在 alert 上時落到預設的 receiver",
            "advice": adv, "cost": "加 label 要確認 inhibit 的 `equal` 與 group_by 不受影響；改 route 會影響其他送到同一個 receiver 的 alert",
            "ref": "inventory.md 的 Alertmanager"}


def item_contracts(fit: dict | None) -> dict | None:
    if not fit:
        return None
    env_bad = [c for c in fit["env_checks"] if not c["ok"]]
    rows_bad = [r for r in fit["rows"] if not r["ok"] and not r.get("nodata")]
    fors = fit.get("for_checks") or []
    if not env_bad and not rows_bad and not fors:
        return None
    parts = []
    if env_bad:
        parts.append(f"環境 {len(env_bad)} 項不符合（{'；'.join(c['text'] for c in env_bad[:2])}）")
    if rows_bad:
        parts.append(f"{len({r['rule'] for r in rows_bad})} 條 rule 的視窗太短（{_names([r['rule'] for r in rows_bad], 4)}）")
    if fors:
        parts.append(f"{len({x['rule'] for x in fors})} 條有 `for` 的門檻 rule 一次 scrape 失敗就重算（推論）")
    if fit.get("defaults_only"):
        parts.append("（沒有 scrape config，用預設值算的）")
    return {"key": "contracts", "title": "時間契約不成立：" + "；".join(parts),
            "why": "視窗內 sample 不夠時查詢回空、`for` 歸零，尖峰時 alert 晚到或不響",
            "advice": "見 contracts-fit.md：方案 A 改 rule 視窗、方案 B 改 `scrape_interval`／`query_offset`；短視窗是刻意的就標 keep_range",
            "cost": "A 讓 rate 對短暫尖峰反應變慢；B 增加 scrape 量或讓每條 rule 晚判斷", "ref": "contracts-fit.md"}


def item_other(inv: dict) -> list[dict]:
    out = []
    for x in inv.get("findings_from_files") or []:
        if x["kind"] == "inhibit_no_equal":
            out.append({"key": "inhibit", "title": x["fact"], "why": "沒有 `equal` 時抑制不分 instance",
                        "advice": "在 inhibit rule 加 `equal: [instance]`（或 cluster）", "cost": "同一台以外的 warning 會照常送出",
                        "ref": "inventory.md 的 Alertmanager"})
    return out


def rank(inv, ms_rows, fit, cov) -> list[dict]:
    items = [item_firing(inv, ms_rows), item_stack(inv, cov), item_paging(inv), item_contracts(fit)]
    return [i for i in items if i] + item_other(inv)


# ---------- summary ----------

def summary(a, inv, items, fit, ms_rows, lint_out, cov) -> str:
    L = ["# Rule 檢查摘要", "",
         "輸出是建議，不是判決。缺的資料用保守假設先跑完；回答最後的問題或修改 inventory.yaml 後重跑會更新結論。", "",
         "## 建議先處理的 3 件事", ""]
    if not items:
        L.append("- 只看檔案沒有找到要先處理的項目（還沒確認的部分見最後的問題）。")
    for n, it in enumerate(items[:3], 1):
        L += [f"{n}. **{it['title']}**", f"   - 原因：{it['why']}", f"   - **建議**：{it['advice']}",
              f"   - **代價**：{it['cost']}", f"   - 細節：{it['ref']}"]
    if len(items) > 3:
        L += ["", "之後再處理：" + "；".join(i["title"] for i in items[3:])]
    st = inv["stack"]
    L += ["", "## 這是我從檔案理解的環境，哪裡不對？", "",
          "標 **未知**／**推測** 的還沒確認。直接改 `inventory.yaml`（改過的值加 `confirmed: true`），用 `--merge` 重跑。", "",
          "| 項目 | 值 |", "|---|---|"]
    for k, label in (("ruler", "ruler"), ("version", "版本"), ("evaluation_interval", "`evaluation_interval`"),
                     ("scrape_interval", "global `scrape_interval`"), ("alertmanagers", "Alertmanager"),
                     ("external_labels", "`external_labels`"), ("external_observer", "外部觀察者"),
                     ("rules_given_are_loaded", "給的 rule 檔有被載入")):
        if k in st:
            L.append(f"| {label} | {INV._v(st[k])} |")
    L += ["", "| job | `scrape_interval` | `scrape_timeout` | exporter |", "|---|---|---|---|"]
    for j in inv["jobs"]:
        L.append(f"| {j['name']} | {INV._v(j['interval'])} | {INV._v(j['timeout'])} | {INV._v(j['exporter'])} |")
    un = inv.get("unscraped_components")
    if isinstance(un, list) and un:
        L.append("")
        L += [f"- {u['fact']}" for u in un]
    amo = inv.get("alertmanager") or {}
    if "routes_summary" in amo:
        L += ["", f"- Alertmanager 路由：{amo['routes_summary']}"]
    L += ["", "完整清單：`inventory.md`。", "", "## 假設", ""]
    asm = list((fit or {}).get("assumptions") or [])
    if st.get("version", {}).get("from") == "unknown":
        asm.insert(0, "ruler 版本未知：`query_offset`（Prometheus 2.53 起）、`anchored`（3.x）當成可用")
    if (inv.get("alertmanager") or {}).get("from") == "unknown":
        asm.append("沒有 alertmanager.yml：沒有檢查 alert 送到哪裡、會不會 page")
    if not a.config:
        asm.append("沒有 scrape config：job 名稱、`scrape_interval`、被丟掉的 metric 都沒有檢查")
    L += [f"- {x}" for x in asm] or ["- （沒有）"]
    L += ["", "## 接下來想問（最多 3 題）", ""]
    qs = [q for q in inv.get("questions") or [] if q.get("ask_now")][:3]
    L += [f"{n}. {q['q']}（會改變：{'、'.join(q['changes'])}；回答寫到 `{q['answer_key']}`）" for n, q in enumerate(qs, 1)] or ["- （沒有）"]
    errors, advice = lint_out
    cnt: dict = {}
    for r in ms_rows:
        cnt[r["status"]] = cnt.get(r["status"], 0) + 1
    L += ["", "## 各報告", "",
          f"- `inventory.md`：{len(inv.get('findings_from_files') or [])} 項事實、{len(inv.get('questions') or [])} 題",
          f"- `metric-sources.md`：" + "、".join(f"{MS.LABEL[s]} {cnt[s]}" for s in MS.ORDER if cnt.get(s)),
          f"- `lint.md`：{len(advice)} 項建議、{len(errors)} 個引用錯誤"]
    if fit:
        bad = sum(1 for r in fit["rows"] if not r["ok"] and not r.get("nodata"))
        nd = sum(1 for r in fit["rows"] if r.get("nodata"))
        L.append(f"- `contracts-fit.md`：環境 {sum(1 for c in fit['env_checks'] if not c['ok'])} 項、rule 視窗 {bad} 條不符合、"
                 f"沒有資料 {nd} 條、`for` 與 scrape 失敗 {len(fit.get('for_checks') or [])} 條")
    if cov is not None:
        L.append(f"- `coverage/coverage.md`：{len(cov)} 個訊號，{sum(1 for i in cov if i['status'] in CC.ACTION)} 個建議處理")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rules", nargs="+", required=True)
    ap.add_argument("--config")
    ap.add_argument("--config-kind", choices=["prometheus", "vmagent"])
    ap.add_argument("--alertmanager")
    ap.add_argument("--answers")
    ap.add_argument("--merge", help="既有的 inventory（使用者改過的值保留）")
    ap.add_argument("--env", help="env/<name>.yaml：給了才跑覆蓋檢查")
    ap.add_argument("--tiers", default="1")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    inv = run_inventory(a)
    inv_path = os.path.join(a.out, "inventory.yaml")
    ms_argv = ["--inventory", inv_path, "--out", a.out] + (["--answers", a.answers] if a.answers else [])
    _quiet(MS.main, ms_argv)
    with open(os.path.join(a.out, "metric-sources.json"), encoding="utf-8") as f:
        ms_rows = json.load(f)["rows"]
    lint_out = lint(a.rules, a.out)
    rc, txt = _quiet(FC.main, ["--inventory", inv_path, "--sources", os.path.join(a.out, "metric-sources.json"), "--out", a.out])
    fit = None
    fj = os.path.join(a.out, "contracts-fit.json")
    if os.path.exists(fj):
        with open(fj, encoding="utf-8") as f:
            fit = json.load(f)
        fit["defaults_only"] = not a.config
    cov = None
    if a.env:
        _quiet(CC.main, ["--env", a.env, "--inventory", inv_path, "--out", os.path.join(a.out, "coverage"), "--tiers", a.tiers])
        cj = os.path.join(a.out, "coverage", "coverage.json")
        if os.path.exists(cj):
            with open(cj, encoding="utf-8") as f:
                cov = json.load(f)["items"]
    items = rank(inv, ms_rows, fit, cov)
    with open(os.path.join(a.out, "summary.md"), "w", encoding="utf-8") as f:
        f.write(summary(a, inv, items, fit, ms_rows, lint_out, cov))
    print(f"{a.out}/summary.md：建議先處理 {min(3, len(items))} 件（共 {len(items)} 件）")
    for n, it in enumerate(items[:3], 1):
        print(f"  {n}. {it['title'][:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
