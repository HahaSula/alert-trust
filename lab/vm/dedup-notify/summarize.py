"""把 results/trials.jsonl 與 results/blips.jsonl 整理成表格（Markdown），寫到 stdout。"""
import json, os

RES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def rows(name):
    with open(os.path.join(RES, name)) as f:
        return [json.loads(l) for l in f if l.strip() and '"kind"' in l]


print("| # | 情況 | 第一個變化的 sample（秒） | 該 sample 在 30s 區間的位置 | TDown／TValue firing：dedup 30s | 沒有 dedup | TDownFor30s firing：dedup | 沒有 dedup |")
print("|---|---|---|---|---|---|---|---|")
for i, r in enumerate(rows("trials.jsonl"), 1):
    s, f = r["seen_s"], r["first_changed_sample_s"]["nodedup"]
    rule = "TDown" if r["kind"] == "down" else "TValue"
    pos = round((r["t0"] + f) % 30, 1)
    fo = (s.get("dedup|TDownFor30s|firing"), s.get("nodedup|TDownFor30s|firing")) if r["kind"] == "down" else ("—", "—")
    print(f"| {i} | {'target 回 500' if r['kind'] == 'down' else '值 0 → 1'} | {f} | {pos} | {s[f'dedup|{rule}|firing']} | "
          f"{s[f'nodedup|{rule}|firing']} | {fo[0]} | {fo[1]} |")
print()
print("| # | 0 sample（秒） | 在 30s 區間的位置 | dedup VM 事後 export 還有 0 嗎 | TDown：dedup 30s | 沒有 dedup |")
print("|---|---|---|---|---|---|")
for i, r in enumerate(rows("blips.jsonl"), 1):
    z = r["zeros_s"]
    print(f"| {i} | {z['nodedup']} | {r['zero_mod30']} | {'有' if z['dedup'] else '沒有'} | "
          f"{r['tdown_seen_s']['dedup'] if r['tdown_seen_s']['dedup'] is not None else '沒有響'} | {r['tdown_seen_s']['nodedup']} |")
