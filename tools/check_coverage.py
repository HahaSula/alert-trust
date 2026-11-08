#!/usr/bin/env python3
"""Coverage checker: does this environment's monitor of monitor cover each component's signals?

  python3 tools/check_coverage.py --env env/prod.yaml --snapshot ./snapshot --out ./report [--live] [--tiers 1,2]

Inputs
  env.yaml                     components, observers, targets (see env/env.example.yaml)
  <snapshot>/<id>/rules.json   raw /api/v1/rules response of every ruler (Prometheus, vmalert, observers),
                               written by tools/collect.py
  --live                       also query each ruler's URL from env.yaml to check that rule selectors
                               match any series (otherwise data presence is reported as unchecked)

Statuses
  covered         a rule watches the signal, is evaluated where the catalog says, and (with --live) has data
  self_only       only the monitored component evaluates it, so it stops when the component dies
  rule_no_data    a rule exists but its selector matches no series (e.g. a job-name regex that does not match)
  missing         no rule references the signal
  not_applicable  the signal's applies_when does not hold for this component
  needs_input     only an interview or inventory can answer it (e.g. the observer's failure domain)

Outputs <out>/coverage.json (for AI) and <out>/coverage.md (for people).
Requires PyYAML (via tools/catalog.py); live queries use the standard library.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog as C  # noqa: E402

TYPE_TO_CARD = {
    "prometheus": "prometheus", "alertmanager": "alertmanager", "vmalert": "vmalert", "vmagent": "vmagent",
    "victoriametrics": "victoriametrics", "vmcluster": "victoriametrics", "vmsingle": "victoriametrics",
    "victorialogs": "victorialogs", "mysqld_exporter": "mysqld_exporter", "postgres_exporter": "postgres_exporter",
}
RULER_TYPES = {"prometheus", "vmalert"}
STATUS_ORDER = ["missing", "rule_no_data", "self_only", "needs_input", "covered", "not_applicable"]
STATUS_TEXT = {"covered": "涵蓋", "self_only": "只在自己身上", "rule_no_data": "規則在但沒資料", "missing": "缺少",
               "not_applicable": "不適用", "needs_input": "需要回答"}
SELECTOR = r"(?<![\w:]){m}(\{{[^}}]*\}})?"


# ---------- inputs ----------

def load_env(path: str) -> dict:
    env = C._load(path) or {}
    env.setdefault("components", [])
    env.setdefault("observers", [])
    env.setdefault("targets", [])
    return env


def load_rules(snapshot: str, ruler_id: str) -> list[dict]:
    path = os.path.join(snapshot, ruler_id, "rules.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data = data.get("data", data)
    return [r for g in data.get("groups", []) for r in g.get("rules", []) if r.get("type") == "alerting"]


def rulers(env: dict) -> dict[str, dict]:
    out = {}
    for c in env["components"]:
        if c.get("type") in RULER_TYPES:
            out[c["id"]] = {**c, "_observer": False}
    for o in env["observers"]:
        out[o["id"]] = {**o, "_observer": True}
    return out


# ---------- matching ----------

def job_of(comp: dict) -> str:
    return str(comp.get("job") or comp["id"])


def job_matches(expr: str, job: str) -> bool:
    """True if the expression has no job matcher, or one that matches this job."""
    found = False
    for op, val in re.findall(r'\bjob\s*(=~|=|!=|!~)\s*"([^"]*)"', expr):
        found = True
        if op == "=" and val == job:
            return True
        if op == "=~" and re.fullmatch(val, job):
            return True
    return not found


def rule_matches(signal: dict, rule: dict, comp: dict) -> bool:
    expr = str(rule.get("query", ""))
    names = {r.split("#", 1)[1] for r in signal.get("rules", [])}
    if rule.get("name") in names:
        return True
    for m in signal.get("metrics", []):
        if not re.search(SELECTOR.format(m=re.escape(m)), expr):
            continue
        if m == "up":
            # up is shared by every job: only count rules whose job matcher selects this component
            if re.search(r'\bjob\s*(=~|=)', expr) and job_matches(expr, job_of(comp)):
                return True
            continue
        if job_matches(expr, job_of(comp)):
            return True
    return False


def placement(ruler_id: str, ruler: dict, comp: dict) -> str:
    if ruler_id == comp["id"]:
        return "self"
    if ruler.get("_observer"):
        scrapes = ruler.get("scrapes")
        same_domain = ruler.get("failure_domain") and ruler.get("failure_domain") == comp.get("failure_domain")
        if (scrapes is None or comp["id"] in scrapes) and not same_domain:
            return "independent"
        return "same_domain"
    if comp.get("scraped_by") == ruler_id:
        return "scraper"  # the Prometheus that scrapes a target: expected place for target signals
    return "other"


# ---------- live data check ----------

def count_series(url: str, selector: str) -> int | None:
    q = urllib.parse.urlencode({"query": f"count({selector})"})
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/v1/query?{q}", timeout=15) as r:  # noqa: S310
            res = json.load(r)["data"]["result"]
        return int(float(res[0]["value"][1])) if res else 0
    except Exception:  # noqa: BLE001 - unreachable ruler is reported as unknown
        return None


def selectors(signal: dict, expr: str) -> list[str]:
    out = []
    for m in signal.get("metrics", []):
        for sel in re.finditer(SELECTOR.format(m=re.escape(m)), expr):
            out.append(sel.group(0))
    return out


# ---------- applies_when ----------

def applies(signal: dict, comp: dict, env: dict) -> tuple[bool, str]:
    cond = str(signal.get("applies_when") or "").strip()
    if not cond:
        return True, ""
    if cond == "cluster":
        ok = comp.get("mode") == "cluster" or comp.get("type") == "vmcluster"
        return ok, "" if ok else "不是 cluster"
    if cond.startswith("replicas"):
        ok = int(comp.get("replicas", 1)) > 1
        return ok, "" if ok else "只有 1 台"
    if cond == "kubernetes":
        ok = bool(env.get("kubernetes"))
        return ok, "" if ok else "env.yaml 沒有標 kubernetes: true"
    if cond == "prometheus in env":
        ok = any(c.get("type") == "prometheus" for c in env["components"])
        return ok, ""
    flags = {str(x) for x in comp.get("features", [])}
    if cond in flags:
        return True, ""
    return True, f"適用條件「{cond}」未確認，請在 env.yaml 的 features 標明"


# ---------- observer card (architecture questions answered from env.yaml) ----------

def observer_item(signal: dict, env: dict) -> tuple[str, str]:
    obs = env["observers"]
    sid = signal["id"]
    if not obs:
        return "missing", "env.yaml 沒有任何外部觀察者"
    if sid == "failure-domain":
        bad = [o["id"] for o in obs if not o.get("failure_domain") or o.get("failure_domain") in ("same", "unknown")]
        return ("needs_input", f"這些觀察者的故障域未標明：{', '.join(bad)}") if bad else ("covered", "依 env.yaml（使用者說的）")
    if sid == "separate-alert-path":
        monitored_am = {c["id"] for c in env["components"] if c.get("type") == "alertmanager"}
        bad = [o["id"] for o in obs if not o.get("notify_via") or o.get("notify_via") in monitored_am]
        return ("missing", f"這些觀察者經過被監控的 Alertmanager 或未標明：{', '.join(bad)}") if bad \
            else ("covered", "依 env.yaml（使用者說的）")
    if sid == "own-deadman":
        ok = all(o.get("deadman") for o in obs)
        return ("covered", "依 env.yaml（使用者說的）") if ok else ("needs_input", "觀察者的 deadman 欄位未填")
    if sid == "deadman-wait":
        dm = env.get("deadman") or {}
        return ("covered", f"max_silence = {dm.get('max_silence')}，請對照契約確認") if dm.get("max_silence") \
            else ("needs_input", "env.yaml 沒有 deadman.max_silence")
    if sid == "scrapes-all-infra":
        infra = {c["id"] for c in env["components"]}
        seen = {s for o in obs for s in (o.get("scrapes") or [])}
        miss = sorted(infra - seen)
        return ("missing", f"沒有觀察者抓這些元件：{', '.join(miss)}") if miss else ("covered", "依 env.yaml 的 scrapes")
    return "needs_input", "這一項要問架構"


# ---------- main evaluation ----------

def evaluate(env: dict, snapshot: str, tiers: set[int], live: bool) -> list[dict]:
    cards = {c["id"]: c for c in C.load_components()}
    rl = rulers(env)
    rules_by_ruler = {rid: load_rules(snapshot, rid) for rid in rl}
    items = []
    instances = [(c, TYPE_TO_CARD.get(c.get("type"))) for c in env["components"]]
    instances += [(t, TYPE_TO_CARD.get(t.get("type"))) for t in env["targets"]]
    instances.append(({"id": "observers", "type": "observer"}, "observer"))
    for comp, card_id in instances:
        if card_id not in cards:
            items.append({"component": comp["id"], "card": None, "signal": None, "tier": None, "status": "needs_input",
                          "note": f"元件類型 {comp.get('type')!r} 沒有對應的元件卡"})
            continue
        card = cards[card_id]
        for s in card["signals"]:
            if s["tier"] not in tiers:
                continue
            item = {"component": comp["id"], "card": card_id, "signal": s["id"], "tier": s["tier"],
                    "question": s["question"], "evaluate_on": s["evaluate_on"], "rules": [], "note": "",
                    "suggested_rules": s.get("rules", []), "runbook": C.runbook_url(card_id, s["id"])}
            ok, why = applies(s, comp, env)
            if not ok:
                item.update(status="not_applicable", note=why)
                items.append(item)
                continue
            if card_id == "observer":
                status, note = observer_item(s, env)
                item.update(status=status, note=note)
                items.append(item)
                continue
            if s["evaluate_on"] in ("interview", "inventory", "external"):
                item.update(status="needs_input",
                            note={"interview": "要問架構", "inventory": "盤點時檢查（/api/v1/rules、/flags）",
                                  "external": "由外部服務判斷，例如 dead man's switch 是否收到"}[s["evaluate_on"]])
                items.append(item)
                continue
            for rid, rules in rules_by_ruler.items():
                for r in rules:
                    if rule_matches(s, r, comp):
                        item["rules"].append({"ruler": rid, "alert": r.get("name"), "placement": placement(rid, rl[rid], comp),
                                              "query": r.get("query", "")})
            if not item["rules"]:
                item["status"] = "missing"
                items.append(item)
                continue
            wants_self = s["evaluate_on"] in ("self", "dedicated")
            good = [r for r in item["rules"]
                    if r["placement"] in (("self", "scraper", "independent") if wants_self else ("independent",))]
            if not good:
                item.update(status="self_only",
                            note="只有被監控的元件（或同一故障域的 ruler）在評估，它掛掉時這條規則也停")
                items.append(item)
                continue
            if live:
                has_data = False
                for r in good:
                    url = rl[r["ruler"]].get("url")
                    counts = [count_series(url, sel) for sel in selectors(s, r["query"])] if url else []
                    r["series"] = counts
                    if any(c for c in counts) or not counts:
                        has_data = True
                item["status"] = "covered" if has_data else "rule_no_data"
                if not has_data:
                    item["note"] = "規則的 selector 查不到任何 series：檢查 job 名稱、label 或這個 metric 是否存在"
            else:
                item.update(status="covered", note="未用 --live 檢查資料是否存在")
            items.append(item)
    return items


def render_md(env: dict, items: list[dict], tiers: set[int]) -> str:
    counts = {k: sum(1 for i in items if i["status"] == k) for k in STATUS_ORDER}
    out = [f"# Monitor of monitor 覆蓋：{env.get('name', '')}", "",
           f"產生時間 {time.strftime('%Y-%m-%d %H:%M')}，檢查第 {'、'.join(map(str, sorted(tiers)))} 級訊號。", "",
           "| " + " | ".join(STATUS_TEXT[k] for k in STATUS_ORDER) + " |",
           "|" + "---|" * len(STATUS_ORDER),
           "| " + " | ".join(str(counts[k]) for k in STATUS_ORDER) + " |", ""]
    out += ["## 要先處理", "", "| 元件 | 訊號 | 狀態 | 說明 | 建議規則 |", "|---|---|---|---|---|"]
    for i in sorted(items, key=lambda x: (STATUS_ORDER.index(x["status"]), x["component"])):
        if i["status"] in ("missing", "rule_no_data", "self_only"):
            sug = "、".join(f"`{r.split('#')[1]}`" for r in i.get("suggested_rules", [])) or "—"
            out.append(f"| {i['component']} | [{i.get('question') or i['signal']}]({i.get('runbook', '')}) | "
                       f"{STATUS_TEXT[i['status']]} | {i.get('note', '')} | {sug} |")
    out.append("")
    for comp in dict.fromkeys(i["component"] for i in items):
        out += [f"## {comp}", "", "| 級 | 訊號 | 狀態 | 規則（ruler：alert，位置） | 說明 |", "|---|---|---|---|---|"]
        for i in [x for x in items if x["component"] == comp]:
            rules = "<br>".join(f"{r['ruler']}：{r['alert']}（{r['placement']}）" for r in i.get("rules", [])[:4]) or "—"
            out.append(f"| {i.get('tier') or ''} | {i.get('question') or i.get('signal') or ''} | {STATUS_TEXT[i['status']]} | "
                       f"{rules} | {i.get('note', '')} |")
        out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env", required=True)
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--live", action="store_true", help="query each ruler to check rule selectors match series")
    ap.add_argument("--tiers", default="1", help="comma-separated tiers, default 1")
    a = ap.parse_args()
    env = load_env(a.env)
    tiers = {int(t) for t in a.tiers.split(",")}
    items = evaluate(env, a.snapshot, tiers, a.live)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "coverage.json"), "w", encoding="utf-8") as f:
        json.dump({"env": env.get("name"), "generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "tiers": sorted(tiers),
                   "live": a.live, "items": items}, f, indent=1, ensure_ascii=False)
    with open(os.path.join(a.out, "coverage.md"), "w", encoding="utf-8") as f:
        f.write(render_md(env, items, tiers))
    bad = sum(1 for i in items if i["status"] in ("missing", "rule_no_data", "self_only"))
    print(f"{len(items)} items, {bad} need action → {a.out}/coverage.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
