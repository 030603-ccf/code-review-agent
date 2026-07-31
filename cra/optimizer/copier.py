"""copier.py —— 副本机制：优化永远发生在副本上，原项目一根手指都不碰。

铁律：
    所有修改只写进 runs/<run_id>/optimized_copy/，
    验证通过前绝不回写原项目；要不要合并、合并哪些，永远由人拍板。

三个纯函数：
    create_workspace  建副本
    hash_tree         给整棵树算哈希（"修改前快照"）
    diff_hashes       对比两份快照，算出"动了哪些文件"
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

from cra.analysis.ast_scan import IGNORE_DIRS

COPY_DIR_NAME = "optimized_copy"


def create_workspace(target_root: Path, run_dir: Path) -> Path:
    """把目标项目完整复制到 run_dir/optimized_copy/，返回副本路径。"""
    target_root = Path(target_root).resolve()
    copy_root = Path(run_dir) / COPY_DIR_NAME

    if copy_root.exists():
        shutil.rmtree(copy_root)

    def _ignore(dir_path: str, names: list[str]) -> set[str]:
        return {n for n in names if n in IGNORE_DIRS}

    shutil.copytree(target_root, copy_root, ignore=_ignore)

    # 给副本 git init：编程 agent 会向上找 .git 确定项目根，
    # 副本有了自己的 .git，agent 的势力范围被钉死在副本里。
    try:
        subprocess.run(["git", "init", "-q"], cwd=copy_root,
                       capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return copy_root


def hash_tree(root) -> dict[str, str]:
    """给目录里每个文件算 sha256，返回 {相对路径: 哈希值}。"""
    hashes: dict[str, str] = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_dir():
            continue
        rel = p.relative_to(root)
        if any(part in IGNORE_DIRS for part in rel.parts):
            continue
        hashes[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return hashes


def diff_hashes(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    """对比修复前后两份哈希快照，返回 changed/added/deleted 三类。"""
    common = before.keys() & after.keys()
    changed = sorted(p for p in common if before[p] != after[p])
    added = sorted(after.keys() - before.keys())
    deleted = sorted(before.keys() - after.keys())
    return {"changed": changed, "added": added, "deleted": deleted}
