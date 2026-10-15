from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.install_skill import install_skill, package_manifest


class InstallSkillTests(unittest.TestCase):
    def test_three_homes_identical_with_recoverable_backup_and_untouched_login(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "SKILL.md").write_text("---\nname: codex-pair\n---\n", encoding="utf-8")
            (source / "scripts").mkdir()
            (source / "scripts" / "pair.py").write_text("print('中文')", encoding="utf-8")
            (source / "scripts" / "auth.json").write_text("never copy", encoding="utf-8")
            (source / "scripts" / "state.sqlite3").write_text("never copy", encoding="utf-8")
            (source / "auth.json").write_text("never copy", encoding="utf-8")
            homes = []
            for name in (".codex", ".codex-b", ".codex-c"):
                home = root / name
                old = home / "skills" / "codex-pair"
                old.mkdir(parents=True)
                (old / "user-note.txt").write_text("preserve", encoding="utf-8")
                (home / "auth.json").write_bytes(b"login stays byte identical")
                (home / "config.toml").write_bytes(b"config stays byte identical")
                homes.append(str(home))
            result = install_skill(str(source), homes)
            self.assertEqual(3, len(result["installations"]))
            for item in result["installations"]:
                self.assertEqual(package_manifest(source), package_manifest(Path(item["target"])))
                self.assertEqual("preserve", (Path(item["backup"]) / "user-note.txt").read_text())
                self.assertFalse((Path(item["target"]) / "scripts" / "auth.json").exists())
                self.assertFalse((Path(item["target"]) / "scripts" / "state.sqlite3").exists())
                self.assertEqual(b"login stays byte identical", (Path(item["home"]) / "auth.json").read_bytes())
                self.assertEqual(b"config stays byte identical", (Path(item["home"]) / "config.toml").read_bytes())

    def test_rejects_missing_home_and_self_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "skills" / "codex-pair"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("skill", encoding="utf-8")
            with self.assertRaises(ValueError):
                install_skill(str(source), [str(root)])
            with self.assertRaises(FileNotFoundError):
                install_skill(str(source), [str(root / "missing")])


if __name__ == "__main__":
    unittest.main()
