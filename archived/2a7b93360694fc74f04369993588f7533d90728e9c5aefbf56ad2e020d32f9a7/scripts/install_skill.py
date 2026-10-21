"""将同一技能包安装到多个 CODEX_HOME，只处理 skills/codex-pair。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import uuid


PACKAGE_ROOTS = {"SKILL.md", "agents", "scripts", "references", "tests"}
FORBIDDEN = {"auth.json", "config.toml", "credentials.json", "__pycache__", ".git", ".pytest_cache"}
PACKAGE_SUFFIXES = {".md", ".py", ".yaml", ".yml"}


def package_manifest(source: Path) -> dict[str, str]:
    source = source.resolve(strict=True)
    if not (source / "SKILL.md").is_file():
        raise ValueError("源目录缺少 SKILL.md")
    manifest = {}
    for name in sorted(PACKAGE_ROOTS):
        base = source / name
        if not base.exists():
            continue
        candidates = [base] if base.is_file() else sorted(base.rglob("*"))
        for path in candidates:
            relative = path.relative_to(source)
            if any(part.casefold() in FORBIDDEN for part in relative.parts) or path.suffix.casefold() not in PACKAGE_SUFFIXES:
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(source):
                raise ValueError(f"技能包包含路径重定向：{relative}")
            if path.is_file():
                manifest[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def install_skill(source: str, homes: list[str]) -> dict:
    source_path = Path(source).resolve(strict=True)
    manifest = package_manifest(source_path)
    targets = []
    for home in homes:
        home_path = Path(home).resolve(strict=True)
        if not home_path.is_dir():
            raise ValueError(f"账号目录不存在：{home}")
        target = home_path / "skills" / "codex-pair"
        if target.is_symlink() or target.resolve() != target or target.resolve() == source_path:
            raise ValueError(f"安装目标含重定向或与源目录相同：{target}")
        if target in targets:
            continue
        targets.append(target)
    installed = []
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".codex-pair-install-", dir=target.parent))
        backup = None
        try:
            for relative in manifest:
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path / relative, destination)
            if package_manifest(stage) != manifest:
                raise ValueError("安装暂存包哈希不一致")
            if target.exists():
                backup_root = target.parent.parent / "codex-pair-skill-backups"
                backup_root.mkdir(exist_ok=True)
                backup = backup_root / f"codex-pair-{uuid.uuid4().hex}"
                target.rename(backup)
            try:
                stage.rename(target)
                if package_manifest(target) != manifest:
                    raise ValueError("安装后哈希不一致")
            except Exception:
                if target.exists():
                    failed = target.with_name(f".codex-pair-failed-{uuid.uuid4().hex}")
                    target.rename(failed)
                if backup is not None:
                    backup.rename(target)
                raise
            installed.append({"home": str(target.parent.parent), "target": str(target),
                              "backup": str(backup) if backup else None, "verified": True})
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return {"source": str(source_path), "files": manifest, "installations": installed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--home", required=True, action="append")
    args = parser.parse_args()
    print(json.dumps(install_skill(args.source, args.home), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
