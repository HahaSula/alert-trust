"""MkDocs hook: turn [^key] markers into footnotes using catalog/refs.yaml and catalog/refs-code.yaml.

Pages only write the marker; the full citation (pinned source line or experiment result) is appended here,
so every page cites sources the same way and links are maintained in one place.
"""
from __future__ import annotations

import os
import re

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARK = re.compile(r"\[\^([a-z0-9][a-z0-9-]*)\](?!:)")
_CACHE: dict = {}


def load_refs() -> dict:
    if not _CACHE:
        for name in ("refs-code.yaml", "refs.yaml"):
            with open(os.path.join(ROOT, "catalog", name), encoding="utf-8") as f:
                _CACHE.update((yaml.safe_load(f) or {}).get("refs") or {})
    return _CACHE


def render(ref: dict) -> str:
    if ref.get("kind") == "code":
        return f"讀碼：[{ref['project']} {ref['version']} `{ref['file']}:{ref['line']}`]({ref['url']})"
    if ref.get("url"):
        return f"實驗：[{ref['title']}]({ref['url']})"
    return f"實驗：{ref['title']}（未公開；{ref.get('note', '')}）"


def on_page_markdown(markdown, page, config, files):  # noqa: ARG001 - MkDocs hook signature
    refs = load_refs()
    used: list[str] = []
    for m in MARK.finditer(markdown):
        key = m.group(1)
        if key in refs and key not in used and f"[^{key}]:" not in markdown:
            used.append(key)
    if not used:
        return markdown
    return markdown.rstrip() + "\n\n" + "\n".join(f"[^{k}]: {render(refs[k])}" for k in used) + "\n"
