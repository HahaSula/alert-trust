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

and writes <out>/summary.md. It opens with 「會不會（在 X 內）通知到人」: one sentence that answers end to end, then
detection (rule logic), rule loading (rule_files), routing (Alertmanager) and self-monitoring (Watchdog / DMS /
observer) separately; --notify-within X (or --env critical.notify_within) adds the worst-case times (推算). Then
「建議先處理的 3 件事」 ranked as
  1. rules that never fire / always fire (job, label, dropped metric, absent() on nothing)
  2. the monitoring stack itself: no Watchdog / dead man's switch, the ruler or Alertmanager not scraped
  3. critical alerts that will not page (route label no rule has, only email / empty receivers)
  3b. rules that do not fit this deployment (「這條 rule 不適用這個部署」: feature or label)
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
                    ("--answers", a.answers), ("--merge", a.merge), ("--env", a.env)):
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
    bad = {"job_mismatch", "label_mismatch", "dropped", "no_source"}
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
        advice.append("加一條一直在響的 `Watchdog`（`vector(1)`），在 Alertmanager 用獨立 route 送到外部 dead man's switch"
                      "（提案檔用合法的佔位 URL，例如 `https://dms.example.invalid/ping`，讓 `amtool check-config` 對交付的檔案也通過）")
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
    for x in f:
        if x["kind"] == "watchdog_no_dedicated_route":
            wr = (inv.get("alertmanager") or {}).get("watchdog_route") or {}
            parts.append(f"Watchdog 沒有專用 route（繼承 `repeat_interval` {wr.get('repeat_interval', '—')}，"
                         f"DMS 至少等 {wr.get('dms_min_wait', '—')}，推算）")
            advice.append("給 Watchdog 一條專用 route（排在最前面，`group_wait: 0s`、`group_interval: 1m`、`repeat_interval: 1m`），"
                          "範本見 inventory.yaml 的 `alertmanager.watchdog_route.template`")
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


def item_deploy(inv: dict) -> dict | None:
    f = [x for x in inv.get("findings_from_files") or [] if x["kind"] == "rule_not_applicable"]
    if not f:
        return None
    names = [n for x in f for n in x["rules"]]
    why = "；".join(f"{_names(x['rules'], 4)}：{x['reason']}" for x in f)
    return {"key": "deploy", "title": f"{len(set(names))} 條 rule 不適用這個部署（不只是沒有資料）",
            "why": why, "advice": "移除，或改寫成這個部署的版本（例如 per-host exporter 用每台的 `up`；沒有 `cluster` label 時先在 "
                                  "target 加 label 再用 `by (cluster)`）；功能其實有的話在 inventory.yaml 的 jobs[].features 列出後重跑",
            "cost": "移除後這個部署沒有對應的訊號；加 label 要同步改 dashboard 與 inhibit 的 `equal`",
            "ref": "inventory.md 的「只看檔案就知道的事實」"}


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


def offset_options(fit: dict | None) -> str:
    """Option B text, gated by the ruler version: `query_offset` from Prometheus 2.53, `anchored` 3.x experimental."""
    stack = (fit or {}).get("stack") or "prometheus"
    if stack == "vmalert":
        return "方案 B 改 `scrape_interval`／`eval_delay`"
    v = FC.parse_version((fit or {}).get("prom_version"))
    if v is None:
        return ("方案 B 改 `scrape_interval`；`query_offset`「" + FC.QO_UNKNOWN + "」、`anchored`「" + FC.ANCHORED_UNKNOWN + "」")
    if v < (2, 53):
        return "方案 B 改 `scrape_interval`（這個版本沒有 `query_offset`、`anchored`）"
    if v < (3,):
        return "方案 B 改 `scrape_interval`／`query_offset`（`anchored` 要 3.x 實驗功能）"
    return "方案 B 改 `scrape_interval`／`query_offset`；保留視窗時可用 `anchored`（實驗功能）"


def item_contracts(fit: dict | None) -> dict | None:
    if not fit:
        return None
    env_bad = [c for c in fit["env_checks"] if c["ok"] is False]
    remeasure = [c for c in fit["env_checks"] if c["ok"] is None]
    rows_bad = [r for r in fit["rows"] if not r["ok"] and not r.get("nodata")]
    sem = sorted({r["rule"] for r in fit["rows"] if r.get("semantic_changed")})
    unk = list(fit.get("keep_unknown") or [])
    fors = fit.get("for_checks") or []
    if not env_bad and not rows_bad and not fors and not sem and not unk and not remeasure:
        return None
    parts = []
    if sem:
        parts.append(f"{len(sem)} 條 rule 的視窗和原本的不同：**{FC.SEMANTIC_CHANGED}**，不算符合（{_names(sem, 4)}）")
    if unk:
        parts.append(f"{len(unk)} 條 rule 的視窗很短，先問「{FC.KEEP_Q}」（{_names(unk, 4)}）")
    if env_bad:
        parts.append(f"環境 {len(env_bad)} 項不符合（{'；'.join(c['text'] for c in env_bad[:2])}）")
    if remeasure:
        parts.append(f"{len(remeasure)} 項要用新的 τ 重新量（{'；'.join(c['text'] for c in remeasure[:2])}）：**需要重新量**")
    if rows_bad:
        parts.append(f"{len({r['rule'] for r in rows_bad})} 條 rule 的視窗太短（{_names([r['rule'] for r in rows_bad], 4)}）")
    if fors:
        parts.append(f"{len({x['rule'] for x in fors})} 條有 `for` 的門檻 rule 一次 scrape 失敗就重算（推論）")
    if fit.get("defaults_only"):
        parts.append("（沒有 scrape config，用預設值算的）")
    return {"key": "contracts", "title": "時間契約不成立：" + "；".join(parts),
            "why": "視窗內 sample 不夠時查詢回空、`for` 歸零，尖峰時 alert 晚到或不響",
            "advice": (f"見 contracts-fit.md：視窗可能是刻意的，回答「{FC.KEEP_Q}」之前先列保留視窗的做法（{offset_options(fit)}），"
                       "和改視窗並列，不要先改視窗；答案寫進 answers.yaml 的 `keep_range`（true 保留、false 可以改）後用 `--merge` 重跑"
                       if unk or sem else f"見 contracts-fit.md：{offset_options(fit)}，或方案 A 改 rule 視窗；短視窗是刻意的就標 keep_range"),
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
    items = [item_firing(inv, ms_rows), item_stack(inv, cov), item_paging(inv), item_deploy(inv), item_contracts(fit)]
    return [i for i in items if i] + item_other(inv)


# ---------- end to end: will a person be paged (within X)? ----------

def end_to_end(a, inv: dict, fit: dict | None, ms_rows: list[dict]) -> list[str]:
    """First sentence answers 「X 內會不會 page 到人」 end to end; then the four parts separately: detection (rule
    logic), rule loading (rule_files), routing (Alertmanager), self-monitoring (Watchdog / DMS / observer)."""
    X = FC.fmt(a.notify_within) if getattr(a, "notify_within", None) else None
    f = inv.get("findings_from_files") or []
    st = inv["stack"]
    crit = [r["name"] for r in inv["rules"] if r.get("kind") == "alert" and
            INV._sev_critical((r.get("labels_out") or {}).get("severity"))]
    no, unk, partial, parts = [], [], [], []
    # 1. detection
    nodata = {r["name"] for r in inv["rules"] if (r.get("fires") or {}).get("value") == "never"}
    bad = {"job_mismatch", "label_mismatch", "dropped", "no_source"}
    nodata |= {r["rule"] for r in ms_rows if r["status"] in bad and r["rule"] in crit
               and all(x["status"] in bad | {"builtin", "recording"} for x in ms_rows if x["rule"] == r["rule"])}
    na = {n for x in f if x["kind"] == "rule_not_applicable" for n in x["rules"]}
    cnd = [n for n in crit if n in nodata]
    if not crit:
        det = "沒有 severity critical 的 alert rule：沒有任何 alert 被標成要 page"
        no.append("沒有 critical rule")
    else:
        det = f"{len(crit)} 條 critical rule；{len(cnd)} 條永遠不會響（{_names(cnd, 4)}）" if cnd else f"{len(crit)} 條 critical rule 都讀得到資料（依檔案）"
        if len(cnd) == len(crit):
            no.append("critical rule 全部不會響")
        elif cnd:
            partial.append(f"{len(cnd)} 條 critical rule 不會響")
        nac = [n for n in crit if n in na]
        if nac:
            det += f"；{len(nac)} 條不適用這個部署（{_names(nac, 4)}）"
        if X and fit and fit.get("notify"):
            nr = [n for n in fit["notify"] if not n.get("nodata")]
            late = [n for n in nr if n["ok"] is False]
            worst = max(nr, key=lambda n: n["total"]) if nr else None
            if worst:
                det += (f"；推算最壞偵測＋第一次通知 {FC.fmt(worst['total'])}（`{worst['rule']}`：{worst['formula']}，推算）"
                        + (f"，{len(late)} 條超過 {X}（{_names([n['rule'] for n in late], 4)}）" if late else f"，都在 {X} 內"))
                assumed = sorted({x["name"] for x in worst.get("inputs") or [] if str(x["source"]).startswith(("assumed", "default"))})
                if assumed:
                    det += f"；用了假設／預設值：{'、'.join(assumed)}（見 contracts-fit.md 第 6 節的來源與敏感度）"
                warn = [n for n in nr if n.get("margin_warn")]
                if warn:
                    det += (f"；**警告：{_names([n['rule'] for n in warn], 4)} 離 {X} 不到 {FC.fmt(FC.MARGIN_WARN)}**"
                            f"（最少剩 {FC.fmt(min(n['margin'] for n in warn))}）")
                    partial.append(f"離 {X} 的邊際小於 {FC.fmt(FC.MARGIN_WARN)}")
                if any(n.get("remeasure") is not None for n in nr):
                    det += "；d_max 是在舊的 τ 下量的，需要重新量"
                if late and len(late) == len(nr):
                    no.append(f"偵測就超過 {X}")
                elif late:
                    partial.append(f"{len(late)} 條偵測超過 {X}")
        elif X:
            unk.append("偵測時間（沒有算出來）")
    parts.append(("偵測（rule 的邏輯）", det))
    # 2. loading
    cm = [x["fact"] for x in f if x["kind"] in ("config_missing", "rules_not_loaded") and "rule_files" in x["fact"]]
    loaded = (st.get("rules_given_are_loaded") or {}).get("value")
    if cm:
        load = "；".join(cm)
        no.append("rule 沒有被載入")
    elif loaded is True:
        load = "給的 rule 檔符合 `rule_files` 的 glob（依檔案）"
    elif loaded is False:
        load = "給的 rule 檔不符合 `rule_files` 的 glob"
        no.append("rule 沒有被載入")
    elif (st.get("rule_files_loaded") or {}).get("from") == "file":
        load = ("未知：檔名符合 `rule_files` 的 glob（" + ", ".join((st["rule_files_loaded"].get("value") or [])) + "），"
                "但路徑對不上；請貼 `/api/v1/rules` 輸出裡的 group 名稱確認")
    else:
        load = "未知：沒有 config，或 vmalert 用 `-rule` 參數載入（請貼 `/api/v1/rules` 的 group 名稱，或啟動參數）"
        unk.append("rule 有沒有被載入")
    parts.append(("載入（`rule_files`／`-rule`）", load))
    # 3. routing
    am = inv.get("alertmanager") or {}
    noalert = [x["fact"] for x in f if x["kind"] == "config_missing" and "alerting" in x["fact"]]
    routed_bad = set()
    for x in f:
        if x["kind"] in ("critical_not_paging", "route_label_missing", "route_label_value_mismatch", "routed_to_empty_receiver"):
            routed_bad |= {n for n in x.get("rules") or [] if n in crit}
    if noalert:
        route = "；".join(noalert)
        no.append("alert 送不到 Alertmanager")
    elif "routes" not in am:
        route = "未知：沒有 alertmanager.yml"
        unk.append("Alertmanager 路由")
    elif crit and routed_bad:
        route = f"{len(routed_bad)}／{len(crit)} 條 critical rule 不會 page（{_names(sorted(routed_bad), 4)}；見下面第 1～3 件）"
        if len(routed_bad) >= len([n for n in crit if n not in nodata]):
            no.append("critical 不會 page")
        else:
            partial.append(f"{len(routed_bad)} 條 critical rule 不會 page")
    else:
        rv = sorted({x for r in inv["rules"] if r["name"] in crit for x in ((r.get("route") or {}).get("receivers") or [])})
        route = f"critical rule 會送到 {'、'.join(rv) or '—'}（依檔案；receiver 本身會不會通知到人要確認）"
    parts.append(("路由（Alertmanager）", route))
    # 4. self-monitoring
    sm = []
    if any(x["kind"] == "no_watchdog_rule" for x in f):
        sm.append("沒有 Watchdog")
    if any(x["kind"] == "dms_route_without_rule" for x in f):
        sm.append("有 DMS route 但沒有 Watchdog rule")
    wr = am.get("watchdog_route") if isinstance(am, dict) else None
    if wr and not wr.get("dedicated"):
        sm.append(f"Watchdog 沒有專用 route（繼承 `repeat_interval` {wr['repeat_interval']}）")
    if wr:
        sm.append(f"DMS 依契約 deadman-wait 至少等 {wr['dms_min_wait']}（推算）"
                  + (f"，{'超過' if wr['dms_min_wait_seconds'] > a.notify_within else '在'} {X}{'' if wr['dms_min_wait_seconds'] > a.notify_within else ' 內'}" if X else ""))
    eo = st.get("external_observer") or {}
    if eo.get("from") == "unknown":
        sm.append("外部觀察者未知")
        unk.append("ruler 停掉時誰知道")
    elif not IU_true(eo.get("value")):
        sm.append("沒有外部觀察者")
    un = inv.get("unscraped_components")
    if isinstance(un, list) and un:
        sm.append("沒被抓：" + "、".join(u.get("target") or u["component"] for u in un))
    parts.append(("自我監控（Watchdog／DMS／觀察者）", "；".join(sm) or "有 Watchdog 與專用 route（依檔案）"))
    if any(x["kind"] in ("no_watchdog_rule", "dms_route_without_rule") for x in f):
        partial.append("ruler 或 Alertmanager 停掉時沒有人知道")
    within = f" {X} 內" if X else ""
    if no:
        head = f"**{within.strip() + '，' if X else ''}不會有人被 page**（{'；'.join(dict.fromkeys(no))}）。"
    elif partial:
        head = (f"**{within.strip() + '，' if X else ''}只有部分情況會有人被 page**：{'；'.join(dict.fromkeys(partial))}"
                + (f"；還不知道：{'、'.join(dict.fromkeys(unk))}" if unk else "") + "。")
    elif unk:
        head = f"**{within.strip() + '，' if X else ''}還不能確定會不會有人被 page**：{'、'.join(dict.fromkeys(unk))}還不知道。"
    else:
        head = f"**推算{within}會有人被 page**（偵測、載入、路由依檔案都成立；receiver 本身與 ruler 停掉的情況見下表）。"
    L = ["## 會不會" + (f"在 {X} 內" if X else "") + "通知到人（端到端）", "", head, "",
         "「偵測得到」不等於「會通知到人」：四段都成立才會 page。", "", "| 段 | 依檔案的結論 |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in parts]
    return L + [""]


def IU_true(v) -> bool:
    return v is True or str(v).strip().lower() in ("yes", "true", "有")


# ---------- summary ----------

def summary(a, inv, items, fit, ms_rows, lint_out, cov) -> str:
    L = ["# Rule 檢查摘要", "",
         "輸出是建議，不是判決。缺的資料用保守假設先跑完；回答最後的問題或修改 inventory.yaml 後重跑會更新結論。", "",
         *end_to_end(a, inv, fit, ms_rows),
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
        asm.insert(0, "ruler 版本未知：用到 `query_offset` 的方案標「" + FC.QO_UNKNOWN + "」，`anchored` 標「"
                   + FC.ANCHORED_UNKNOWN + "」；貼 `/api/v1/status/buildinfo` 的輸出後重跑")
    if (inv.get("alertmanager") or {}).get("from") == "unknown":
        asm.append("沒有 alertmanager.yml：沒有檢查 alert 送到哪裡、會不會 page")
    if not a.config:
        asm.append("沒有 scrape config：job 名稱、`scrape_interval`、被丟掉的 metric 都沒有檢查")
    L += [f"- {x}" for x in asm] or ["- （沒有）"]
    if (st.get("ruler") or {}).get("value") == "vmalert" or (fit or {}).get("stack") == "vmalert":
        L += ["", "## 證據等級（ruler 是 vmalert）", "", f"- {FC.VMALERT_EVIDENCE}"]
    L += ["", "## 接下來想問（最多 3 題）", ""]
    qs = [q for q in inv.get("questions") or [] if q.get("ask_now")][:3]
    L += [f"{n}. {q['q']}（{'**必問**；' if q.get('mandatory') else ''}可能推翻 {q.get('overturns', '?')} 個結論 × 代價 "
          f"{q.get('cost', '?')}：{'、'.join(q['changes'])}；回答寫到 `{q['answer_key']}`）" for n, q in enumerate(qs, 1)] or ["- （沒有）"]
    L += ["", "每個回答都寫進 answers.yaml（或 inventory.yaml，加 `confirmed: true`），用 `--merge` 重跑後再回覆；回答過的題目不會再問。"]
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
        sem = len({r["rule"] for r in fit["rows"] if r.get("semantic_changed")})
        L.append(f"- `contracts-fit.md`：環境 {sum(1 for c in fit['env_checks'] if c['ok'] is not True)} 項、rule 視窗 {bad} 條不符合、"
                 f"{FC.SEMANTIC_CHANGED} {sem} 條、沒有資料 {nd} 條、`for` 與 scrape 失敗 {len(fit.get('for_checks') or [])} 條")
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
    ap.add_argument("--original", nargs="+", default=[],
                    help="使用者原本的 rule 檔（--rules 是提案時）：視窗改過、使用者沒確認的 rule 標「語意已改，未確認」")
    ap.add_argument("--env", help="env/<name>.yaml：給了才跑覆蓋檢查")
    ap.add_argument("--tiers", default="1")
    ap.add_argument("--notify-within", help="使用者要求的「X 內通知到人」（例如 5m）；沒給時用 --env 的 critical.notify_within")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if not a.notify_within and a.env and os.path.exists(a.env):
        a.notify_within = ((INV.C._load(a.env) or {}).get("critical") or {}).get("notify_within")
    a.notify_within = FC.dur(a.notify_within) if a.notify_within else None
    os.makedirs(a.out, exist_ok=True)
    inv = run_inventory(a)
    inv_path = os.path.join(a.out, "inventory.yaml")
    ms_argv = ["--inventory", inv_path, "--out", a.out] + (["--answers", a.answers] if a.answers else [])
    _quiet(MS.main, ms_argv)
    with open(os.path.join(a.out, "metric-sources.json"), encoding="utf-8") as f:
        ms_rows = json.load(f)["rows"]
    lint_out = lint(a.rules, a.out)
    rc, txt = _quiet(FC.main, ["--inventory", inv_path, "--sources", os.path.join(a.out, "metric-sources.json"), "--out", a.out]
                     + (["--notify-within", FC.fmt(a.notify_within)] if a.notify_within else [])
                     + (["--answers", a.answers] if a.answers else []) + (["--env", a.env] if a.env else [])
                     + (["--original", *a.original] if a.original else []))
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
