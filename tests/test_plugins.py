"""Plugin layout: marketplace.json, plugins/<dir>/ manifests, skills and shared-data symlinks match build_map.PLUGINS."""
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_map as M  # noqa: E402

SHARED = {"catalog": "../../catalog", "tools": "../../tools", "rules": "../../rules", "env": "../../env"}
FORBIDDEN = {"docs", "lab"}


def load(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return json.load(f)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def frontmatter(path):
    text = read(path)
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    return dict(line.split(": ", 1) for line in m.group(1).splitlines() if ": " in line) if m else {}


class PluginLayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.market = load(".claude-plugin/marketplace.json")

    def test_no_root_plugin_and_old_skills_gone(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, ".claude-plugin", "plugin.json")))
        self.assertFalse(os.path.exists(os.path.join(ROOT, "skills")))

    def test_marketplace_lists_every_plugin(self):
        entries = {p["name"]: p for p in self.market["plugins"]}
        self.assertEqual(set(entries), set(M.PLUGINS))
        for name, info in M.PLUGINS.items():
            self.assertEqual(entries[name]["source"], f"./plugins/{info['dir']}")
            self.assertTrue(entries[name].get("description"))
            self.assertNotIn("version", entries[name], "version 只寫在 plugin.json")
        self.assertEqual(self.market["renames"], {"alert-trust": M.CORE})

    def test_manifests_versions_dependencies(self):
        versions = set()
        for name, info in M.PLUGINS.items():
            pj = load(f"plugins/{info['dir']}/.claude-plugin/plugin.json")
            self.assertEqual(pj["name"], name)
            versions.add(pj["version"])
            if name == M.CORE:
                self.assertNotIn("dependencies", pj)
            else:
                self.assertEqual(pj["dependencies"], [M.CORE])
        self.assertEqual(len(versions), 1, f"所有 plugin.json 的 version 要相同：{versions}")
        self.assertRegex(versions.pop(), r"^0\.3\.\d+$")

    def test_skills_exist_and_named(self):
        for name, info in M.PLUGINS.items():
            sk_dir = os.path.join(ROOT, "plugins", info["dir"], "skills")
            self.assertEqual(sorted(os.listdir(sk_dir)), sorted(info["skills"]))
            for sk in info["skills"]:
                fm = frontmatter(os.path.join(sk_dir, sk, "SKILL.md"))
                self.assertEqual(fm.get("name"), sk)
                self.assertTrue(fm.get("description"))

    def test_shared_data_symlinks(self):
        for name, info in M.PLUGINS.items():
            pdir = os.path.join(ROOT, "plugins", info["dir"])
            want = dict(SHARED)
            if name != M.CORE:
                want["references"] = "../core/references"
            for link, target in want.items():
                p = os.path.join(pdir, link)
                self.assertTrue(os.path.islink(p), f"{p} 要是 symlink")
                self.assertEqual(os.readlink(p), target)
                self.assertTrue(os.path.isdir(p), f"{p} 指不到目錄")
            for bad in FORBIDDEN:
                self.assertFalse(os.path.lexists(os.path.join(pdir, bad)), f"{pdir} 不能帶 {bad}/")
            self.assertTrue(os.path.isfile(os.path.join(pdir, "catalog", "map.yaml")))
            self.assertTrue(os.path.isfile(os.path.join(pdir, "references", "five-sections.md")))

    def test_skills_reference_existing_shared_files(self):
        """`$REF/<file>` in any SKILL.md must exist in plugins/core/references; no stale skills/ paths."""
        refs = set(os.listdir(os.path.join(ROOT, "plugins", "core", "references")))
        for info in M.PLUGINS.values():
            for sk in info["skills"]:
                path = os.path.join(ROOT, "plugins", info["dir"], "skills", sk, "SKILL.md")
                text = read(path)
                for f in re.findall(r"\$REF/([\w.-]+\.md)", text):
                    self.assertIn(f, refs, f"{path}: $REF/{f} 不存在")
                self.assertNotRegex(text, r"\$AT/skills/|database-targets|/alert-trust:", path)


if __name__ == "__main__":
    unittest.main()
