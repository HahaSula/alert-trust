#!/usr/bin/env python3
"""Checks for proposed files (proposed/) before saying 「可以動手」: placeholders, interval consistency, new rules.

  python3 tools/proposal_check.py --rules proposed/*.yml [--config proposed/prometheus.yml] \
      [--alertmanager proposed/alertmanager.yml] [--original rules/*.yml] [--original-config prometheus.yml] \
      [--tests proposed/tests/*.test.yml] [--out report/]

  placeholders   every RFC 2606 / 6761 placeholder host (`*.invalid`, `*.example`, `example.com|org|net`) in the
                 given files, listed as 「上線前要換」 (file, line, value). They are not findings about the environment:
                 inventory.py does not report a placeholder Alertmanager as an unscraped component.
  intervals      with --original-config / --original: a group whose jobs' `scrape_interval` changed while its
                 E (group `interval`, else global `evaluation_interval`) did not, or the other way round, and the two
                 now differ (「E 與 S 不一致」). A group `interval` is E for that group's timing math.
  new_rules      alerts in --rules that are not in --original: each needs a promtool test (--tests: at least one
                 alert_rule_test with exp_alerts and one without, i.e. firing and not firing); a rule whose name an
                 upstream ref names (catalog refs, e.g. a mixin alert pinned to a tag and line) must be copied
                 verbatim from that pinned ref (the ref's url / file#Lline in a comment or annotation) or labelled
                 「依結構改寫，非原文」.
Writes <out>/proposal-check.md and .json (with --out). review.py runs it and adds the lists to summary.md.
Exit code 1 when anything is listed, else 0.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog as C  # noqa: E402
import check_metric_sources as MS  # noqa: E402
import fit_contracts as FC  # noqa: E402
import inventory_use as IU  # noqa: E402

REWRITTEN = "依結構改寫，非原文"
REPLACE_BEFORE = "上線前要換"


def _read(p: str) -> str:
    with open(p, encoding="utf-8") as f:
        return f.read()


def _load(p: str | None) -> dict:
    if not p or not os.path.exists(p):
        return {}
    try:
        return yaml.safe_load(_read(p)) or {}
    except yaml.YAMLError:
        return {}


# ---------- placeholders ----------

def placeholders(paths: list[str]) -> list[dict]:
    """[{file, line, host, text}] for every placeholder host in the files (comment lines skipped)."""
    out = []
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        for n, line in enumerate(_read(p).splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for m in IU.PLACEHOLDER_RE.finditer(line):
                out.append({"file": p, "line": n, "host": m.group(1), "text": line.strip()[:160]})
    return out


# ---------- intervals ----------

def _intervals(cfg: dict, rule_docs: list[dict]) -> dict:
    """{group: {E, E_src, jobs: {job: S}}} from a Prometheus config and rule files."""
    g = cfg.get("global") or {}
    gS = FC.dur(str(g.get("scrape_interval") or "1m"))
    gE = FC.dur(str(g.get("evaluation_interval") or "1m"))
    S = {str(j.get("job_name")): FC.dur(str(j.get("scrape_interval"))) if j.get("scrape_interval") else gS
         for j in cfg.get("scrape_configs") or [] if isinstance(j, dict) and j.get("job_name")}
    out = {}
    for doc in rule_docs:
        for grp in (doc or {}).get("groups") or []:
            jobs = {}
            for r in grp.get("rules") or []:
                for sel in MS.selectors(str(r.get("expr", ""))):
                    for (l, op, v) in sel["matchers"]:
                        if l != "job":
                            continue
                        for j, s in S.items():
                            hit = (j == v) if op == "=" else (op == "=~" and _full(v, j))
                            if hit:
                                jobs[j] = s
            iv = grp.get("interval")
            out[str(grp.get("name"))] = {"E": FC.dur(str(iv)) if iv else gE,
                                         "E_src": "group `interval`" if iv else "global `evaluation_interval`", "jobs": jobs}
    return out


def _full(rx: str, s: str) -> bool:
    try:
        return re.fullmatch(rx, s) is not None
    except re.error:
        return False


def interval_changes(orig_cfg: dict, orig_docs: list[dict], new_cfg: dict, new_docs: list[dict]) -> list[dict]:
    """Groups where the proposal changed S (a job the group reads) but not E, or E but not S, and E ≠ S now."""
    old, new = _intervals(orig_cfg, orig_docs), _intervals(new_cfg, new_docs)
    out = []
    for name, n in new.items():
        o = old.get(name)
        if o is None:
            continue
        dE = o["E"] != n["E"]
        for job, s_new in n["jobs"].items():
            s_old = o["jobs"].get(job)
            if s_old is None:
                continue
            dS = s_old != s_new
            if dS != dE and n["E"] != s_new:
                what = (f"`scrape_interval` 改了（{job} {FC.fmt(s_old)} → {FC.fmt(s_new)}），E 沒改（{n['E_src']} {FC.fmt(n['E'])}）"
                        if dS else f"E 改了（{n['E_src']} {FC.fmt(o['E'])} → {FC.fmt(n['E'])}），`scrape_interval` 沒改（{job} {FC.fmt(s_new)}）")
                out.append({"group": name, "job": job, "S_old": s_old, "S_new": s_new, "E_old": o["E"], "E_new": n["E"],
                            "E_src": n["E_src"],
                            "fact": f"group {name}：{what}，E 與 S 不一致"
                                    + ("（E < S：同一批資料被重算，pending 依相位循環）" if n["E"] < s_new else
                                       "（E > S：偵測最多晚 E，`for` 的評估次數跟著變）")
                                    + "；一起改，或在回覆說明為什麼不一樣"})
    return out


# ---------- new rules ----------

def _rule_names(docs: list[dict]) -> list[str]:
    return [str(r["alert"]) for d in docs for g in (d or {}).get("groups") or [] for r in g.get("rules") or [] if r.get("alert")]


def _tests(paths: list[str]) -> dict[str, dict]:
    """alertname -> {firing: bool, not_firing: bool} from promtool test files."""
    out: dict = {}
    for p in paths:
        for t in (_load(p).get("tests") or []):
            for at in (t or {}).get("alert_rule_test") or []:
                n = str(at.get("alertname"))
                e = out.setdefault(n, {"firing": False, "not_firing": False})
                if at.get("exp_alerts"):
                    e["firing"] = True
                else:
                    e["not_firing"] = True
    return out


def _segment(text: str, name: str) -> str:
    """The YAML text of one rule (from `alert: <name>` to the next rule), comments included."""
    m = re.search(r"^(\s*)-?\s*alert:\s*['\"]?" + re.escape(name) + r"['\"]?\s*$", text, re.M)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"^\s*-\s*(alert|record):", rest, re.M)
    # comments just above the rule belong to it too
    before = text[:m.start()].splitlines()[-3:]
    return "\n".join(before) + text[m.start():m.end()] + (rest[:nxt.start()] if nxt else rest)


def upstream_refs(name: str, refs: dict) -> list[tuple[str, dict]]:
    rx = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
    return [(k, r) for k, r in refs.items() if rx.search(str(r.get("title", ""))) and r.get("url")]


def new_rules(orig_docs: list[dict], new_paths: list[str], tests: list[str], refs: dict | None = None) -> list[dict]:
    refs = refs if refs is not None else C.load_refs()
    old = set(_rule_names(orig_docs))
    texts = {p: _read(p) for p in new_paths if os.path.exists(p)}
    tested = _tests(tests)
    out = []
    for p, text in texts.items():
        for name in _rule_names([_load(p)]):
            if name in old:
                continue
            t = tested.get(name) or {}
            missing = [w for w, k in (("firing", "firing"), ("不 firing", "not_firing")) if not t.get(k)]
            seg = _segment(text, name)
            ups = upstream_refs(name, refs)
            cited = [k for k, r in ups if r["url"] in seg or f"{r.get('file')}#L{r.get('line')}" in seg
                     or (r.get("version") and str(r["version"]) in seg and str(r.get("file", "")) in seg)]
            e = {"rule": name, "file": p, "tests_missing": missing,
                 "upstream": [{"ref": k, "url": r["url"], "version": r.get("version"), "file": r.get("file"), "line": r.get("line")}
                              for k, r in ups]}
            if ups and not cited and REWRITTEN not in seg:
                e["upstream_unlabelled"] = True
            out.append(e)
    return out


def new_rule_text(e: dict) -> str:
    parts = []
    if e["tests_missing"]:
        parts.append(f"沒有 promtool 測試（缺 {'、'.join(e['tests_missing'])} 的情境）")
    if e.get("upstream_unlabelled"):
        u = e["upstream"][0]
        parts.append(f"上游有同名 rule（{u['ref']}：{u['version']} `{u['file']}#L{u['line']}`）：從這個釘住的版本逐字複製，"
                     f"在註解寫 tag 與行號（{u['url']}），或標「{REWRITTEN}」")
    return f"新加的 `{e['rule']}`：" + "；".join(parts) if parts else ""


# ---------- all ----------

def check(rules: list[str], config: str | None = None, alertmanager: str | None = None, original: list[str] | None = None,
          original_config: str | None = None, tests: list[str] | None = None) -> dict:
    new_docs = [_load(p) for p in rules]
    orig_docs = [_load(p) for p in original or []]
    out = {"placeholders": placeholders([*rules, config, alertmanager, *(tests or [])]),
           "intervals": [], "new_rules": []}
    if original or original_config:
        out["intervals"] = interval_changes(_load(original_config or config), orig_docs or new_docs, _load(config), new_docs)
    if original:
        out["new_rules"] = [e for e in new_rules(orig_docs, rules, tests or []) if new_rule_text(e)]
    return out


def markdown(res: dict) -> list[str]:
    L = []
    if res["placeholders"]:
        L += [f"### {REPLACE_BEFORE}（佔位值）", "", "| 檔案 | 行 | 佔位 | 內容 |", "|---|---|---|---|"]
        L += [f"| `{p['file']}` | {p['line']} | `{p['host']}` | `{p['text'].replace('|', '/')}` |" for p in res["placeholders"]]
        L += ["", "佔位值不是環境的發現（例如佔位的 Alertmanager 沒被 scrape 不列成問題）；換成實際值後重跑。", ""]
    if res["intervals"]:
        L += ["### `scrape_interval` 與 E（group `interval`）", ""] + [f"- {x['fact']}" for x in res["intervals"]] + [""]
    if res["new_rules"]:
        L += ["### 新加的 rule", ""] + [f"- {new_rule_text(e)}" for e in res["new_rules"]] + [""]
    return L


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rules", nargs="+", required=True)
    ap.add_argument("--config")
    ap.add_argument("--alertmanager")
    ap.add_argument("--original", nargs="+", default=[])
    ap.add_argument("--original-config")
    ap.add_argument("--tests", nargs="+", default=[])
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    res = check(a.rules, a.config, a.alertmanager, a.original, a.original_config, a.tests)
    md = ["# 提案檔檢查", ""] + (markdown(res) or ["- 沒有要列的項目。"])
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "proposal-check.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(md) + "\n")
        with open(os.path.join(a.out, "proposal-check.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    print("\n".join(md))
    return 1 if any(res.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
