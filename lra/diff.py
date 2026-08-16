"""Incremental review — list files changed since a git base ref.

Uses subprocess (no gitpython dependency). Failures (not a git repo, no git)
degrade to an empty list, which callers treat as "review everything".
"""

import subprocess
from pathlib import Path


def _parse_lines(out: str) -> list[str]:
    return [line.strip().replace("\\", "/")
            for line in out.splitlines() if line.strip()]


def _is_git_repo(repo_root: str) -> bool:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_root, text=True,
            stderr=subprocess.DEVNULL, encoding="utf-8")
        return out.strip().lower() == "true"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def git_changes(repo_root: str | Path,
                base_ref: str = "HEAD~1") -> dict:
    """Return ``{"is_git_repo": bool, "files": [...], "errors": [...]}``.

    Unlike :func:`changed_files`, this distinguishes "not a git repository"
    from "a git repository with no changes" — the CLI needs that to implement
    ``--incremental-strict`` (fail on non-git, succeed on empty diff). It also
    surfaces diff command failures (e.g. invalid ``base_ref``) instead of
    silently treating them as "zero changes".
    """
    root = str(repo_root)
    is_git = _is_git_repo(root)
    paths: set[str] = set()
    if not is_git:
        return {"is_git_repo": False, "files": [], "errors": []}

    errors: list[str] = []

    # committed changes base_ref..HEAD
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only", base_ref, "HEAD"],
            cwd=root, text=True,
            stderr=subprocess.DEVNULL, encoding="utf-8")
        paths.update(_parse_lines(out))
    except subprocess.CalledProcessError as e:
        errors.append(f"committed diff ({base_ref}..HEAD) 失败，returncode={e.returncode}")

    # uncommitted (working tree + index)
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only"],
            cwd=root, text=True,
            stderr=subprocess.DEVNULL, encoding="utf-8")
        paths.update(_parse_lines(out))
    except subprocess.CalledProcessError as e:
        errors.append(f"uncommitted diff 失败，returncode={e.returncode}")

    return {"is_git_repo": True, "files": sorted(paths), "errors": errors}


def changed_files(repo_root: str | Path,
                  base_ref: str = "HEAD~1") -> list[str]:
    """List files changed since a git base ref (committed + uncommitted).

    Failures (not a git repo, no git) degrade to an empty list, which callers
    treat as "review everything". Kept as the stable public contract; new code
    that needs the distinction should use :func:`git_changes`.
    """
    return git_changes(repo_root, base_ref)["files"]
