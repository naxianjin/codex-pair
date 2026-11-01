"""将同一技能包安装到多个 CODEX_HOME，只处理 skills/codex-pair。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid


PACKAGE_ROOTS = {"SKILL.md", "agents", "scripts", "references", "tests"}
FORBIDDEN = {"auth.json", "config.toml", "credentials.json", "__pycache__", ".git", ".pytest_cache"}
PACKAGE_SUFFIXES = {".md", ".py", ".yaml", ".yml"}


def native_path(path) -> Path:
    """Windows文件操作统一使用扩展绝对路径，不修改系统设置。"""
    value = os.path.abspath(os.fspath(path))
    if os.name == 'nt' and not value.startswith('\\\\?\\'):
        value = '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value
    return Path(value)


def path_stat(path):
    try:
        return path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None


def checked_path(path):
    """同时拒绝符号链接、junction及祖先重定向；其他stat错误向上传播。"""
    path = native_path(path)
    info = path_stat(path)
    if (info and (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400)
            or path.resolve() != path):
        raise ValueError(f'路径包含重定向：{path}')
    return path


def package_manifest(source: Path) -> dict[str, str]:
    source = checked_path(source).resolve(strict=True)
    skill = path_stat(source / 'SKILL.md')
    if not skill or not stat.S_ISREG(skill.st_mode):
        raise ValueError("源目录缺少 SKILL.md")
    manifest = {}
    def visit(path):
        relative = path.relative_to(source)
        if any(part.casefold() in FORBIDDEN for part in relative.parts):
            return
        checked_path(path)
        info = path.stat(follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            # scandir/stat失败必须中止，不能用is_file的false静默省略合格文件。
            with os.scandir(path) as entries:
                children = sorted(entry.name for entry in entries)
            for name in children:
                visit(path / name)
        elif stat.S_ISREG(info.st_mode) and path.suffix.casefold() in PACKAGE_SUFFIXES:
            manifest[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif not stat.S_ISREG(info.st_mode):
            raise ValueError(f'技能包包含非普通文件：{relative}')
    for name in sorted(PACKAGE_ROOTS):
        base = source / name
        if path_stat(base) is not None:
            visit(base)
    return manifest


def install_skill(source: str, homes: list[str]) -> dict:
    source_path = checked_path(source).resolve(strict=True)
    manifest = package_manifest(source_path)
    targets = []
    for home in homes:
        home_path = native_path(home).resolve(strict=True)
        if not home_path.is_dir():
            raise ValueError(f"账号目录不存在：{home}")
        target = checked_path(home_path / "skills" / "codex-pair")
        checked_path(home_path / 'codex-pair-skill-backups')
        if target == source_path or target.is_relative_to(source_path) or source_path.is_relative_to(target):
            raise ValueError(f"安装目标含重定向或与源目录相同：{target}")
        if target in targets:
            continue
        targets.append(target)
    installed = []
    for target in targets:
        checked_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        lock = checked_path(target.parent / '.codex-pair-install-lock')
        lock.mkdir()  # 同一目标的并发安装拒绝，不抢锁或重放。
        stage = None
        backup = None
        try:
            stage = Path(tempfile.mkdtemp(prefix=".codex-pair-install-", dir=target.parent))
            original = path_stat(checked_path(target))
            for relative in manifest:
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(checked_path(source_path / relative), destination)
            if package_manifest(stage) != manifest:
                raise ValueError("安装暂存包哈希不一致")
            current = path_stat(checked_path(target))
            if (original is None) != (current is None) or original and not os.path.samestat(original, current):
                raise ValueError('安装目标在暂存期间发生变化')
            if current is not None:
                backup_root = checked_path(target.parent.parent / "codex-pair-skill-backups")
                backup_root.mkdir(exist_ok=True)
                backup = backup_root / f"codex-pair-{uuid.uuid4().hex}"
                target.rename(backup)
            try:
                checked_path(target)
                if path_stat(target) is not None:
                    raise FileExistsError('安装目标并发出现，拒绝覆盖')
                staged_identity = stage.stat()
                stage.rename(target)
                if package_manifest(target) != manifest:
                    raise ValueError("安装后哈希不一致")
            except Exception:
                # stage仍存在说明尚未安装，不移动并发出现的用户目标。
                current = path_stat(checked_path(target))
                if path_stat(stage) is None and current is not None and os.path.samestat(staged_identity, current):
                    failed = target.with_name(f".codex-pair-failed-{uuid.uuid4().hex}")
                    target.rename(failed)
                if backup is not None and path_stat(target) is None:
                    backup.rename(target)
                raise
            installed.append({"home": str(target.parent.parent), "target": str(target),
                              "backup": str(backup) if backup else None, "verified": True})
        except Exception as error:
            if hasattr(error, 'add_note'):
                error.add_note(f'安装失败现场保留；stage={stage}; backup={backup}; target={target}')
            raise
        finally:
            lock.rmdir()
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
