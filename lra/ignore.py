"""Single source of truth for which directories to ignore when walking a tree.

`analysis/scan.py`, `nodes.py` and `optimizer/copier.py` used to each carry
their own drifted copy of this list (three modules, three different contents).
Keep ONE list here and import it everywhere so the "skip .venv / node_modules"
semantics can never diverge again.

Policy: 宁可多忽略，不可误审 — vendored / generated / virtualenv code must never
be reviewed, so err on the side of ignoring too much.
"""

# Directory names skipped at ANY depth of a project tree. Union of the three
# former copies (scan / nodes chunk-filter / optimizer copier).
IGNORE_DIRS: frozenset[str] = frozenset({
    # VCS
    ".git", ".hg", ".svn",
    # virtualenvs
    ".venv", "venv", "env",
    # python caches / test / build tooling
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    # JS / compiled output
    "node_modules", "dist", "build", "out", "target",
    # IDE / tooling
    ".idea", ".vscode",
    # our own run outputs
    "runs",
    # vendored dependencies
    "vendor", "third_party", "thirdparty", "site-packages",
})

# `*.egg-info` is a name *suffix* (e.g. `lra.egg-info`), not a fixed directory
# name, so it cannot live in the exact-match set above.
_EGG_INFO_SUFFIX = ".egg-info"


def is_ignored_dir_name(name: str) -> bool:
    """True when a single directory/file name should be ignored."""
    return name in IGNORE_DIRS or name.endswith(_EGG_INFO_SUFFIX)


def path_is_ignored(parts) -> bool:
    """True when any path part names an ignored directory (suffix-aware)."""
    return any(is_ignored_dir_name(p) for p in parts)
