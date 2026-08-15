"""copier.py — 副本机制：优化永远发生在副本上，原项目一根手指都不碰。

    create_workspace  把项目复制到 run_dir/optimized_copy/（按 IGNORE_DIRS 过滤）
    hash_tree         给整棵树算 sha256 快照 {relpath: sha256}
    diff_hashes       对比两份快照，分出 changed / added / deleted
"""

import hashlib
import shutil
from pathlib import Path

COPY_DIR_NAME = "optimized_copy"

# 复制与哈希都跳过的目录（任意层级命中即跳过）
IGNORE_DIRS = {
    ".git", ".hg", ".svn",
    ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    "node_modules", "dist", "build",
    ".idea", ".vscode",
    "runs",
}


def _ignored(rel_parts) -> bool:
    return any(p in IGNORE_DIRS or p.endswith(".egg-info") for p in rel_parts)


def create_workspace(target_root, run_dir) -> Path:
    """把目标项目完整复制到 run_dir/optimized_copy/，返回副本路径。"""
    target_root = Path(target_root).resolve()
    copy_root = Path(run_dir) / COPY_DIR_NAME

    if copy_root.exists():
        shutil.rmtree(copy_root)

    def _ignore(dir_path: str, names: list[str]) -> set[str]:
        return {n for n in names if n in IGNORE_DIRS or n.endswith(".egg-info")}

    shutil.copytree(target_root, copy_root, ignore=_ignore)
    return copy_root


def hash_tree(root) -> dict[str, str]:
    """给目录里每个文件算 sha256，返回 {相对路径: 哈希}（跳过 IGNORE_DIRS）。"""
    hashes: dict[str, str] = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_dir():
            continue
        rel = p.relative_to(root)
        if _ignored(rel.parts):
            continue
        hashes[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return hashes


def diff_hashes(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    """对比两份哈希快照，返回 changed/added/deleted 三类。"""
    common = before.keys() & after.keys()
    return {
        "changed": sorted(p for p in common if before[p] != after[p]),
        "added": sorted(after.keys() - before.keys()),
        "deleted": sorted(before.keys() - after.keys()),
    }
