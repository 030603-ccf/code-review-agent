"""Incremental review — list files changed since a git base ref.

Uses subprocess (no gitpython dependency). Failures (not a git repo, no git)
degrade to an empty list, which callers treat as "review everything".
"""

import subprocess
from pathlib import Path


def _parse_lines(out: str) -> list[str]:
    return [line.strip().replace("\\", "/")
            for line in out.splitlines() if line.strip()]


def changed_files(repo_root: str | Path,
                  base_ref: str = "HEAD~1") -> list[str]:
    paths: set[str] = set()

    # committed changes base_ref..HEAD
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only", base_ref, "HEAD"],
            cwd=str(repo_root), text=True,
            stderr=subprocess.DEVNULL, encoding="utf-8")
        paths.update(_parse_lines(out))
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # uncommitted (working tree + index)
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only"],
            cwd=str(repo_root), text=True,
            stderr=subprocess.DEVNULL, encoding="utf-8")
        paths.update(_parse_lines(out))
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    return sorted(paths)
