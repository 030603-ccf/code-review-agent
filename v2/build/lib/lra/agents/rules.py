"""Project rules — per-repo review guidelines injected into the reviewer prompt.

Rules live in `<project_root>/.codereview/rules.json`. Two accepted shapes:

    {"rules": [{"glob": "**/*.py", "prompt": "...", "category": "security"}]}
    {"glob": "**/*.py": "...", "src/*.ts": "..."}          # simplified mapping

Both normalize to {glob: prompt}; the optional per-rule category is metadata
for humans and is not injected.
"""

import fnmatch
import json
from pathlib import Path


def load_rules(project_root: str | Path) -> dict[str, str]:
    """Read rules.json under the project root; return {} when absent/unreadable."""
    path = Path(project_root) / ".codereview" / "rules.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    rules = data if isinstance(data, dict) else {}
    items = rules.get("rules")
    if isinstance(items, list):  # {"rules": [{"glob": ..., "prompt": ...}]}
        out: dict[str, str] = {}
        for item in items:
            if isinstance(item, dict) and item.get("glob") and item.get("prompt"):
                out[str(item["glob"])] = str(item["prompt"])
        return out
    # simplified {glob: prompt} mapping — keep only string values
    return {str(k): v for k, v in rules.items() if isinstance(v, str)}


def _match_glob(relpath: str, pattern: str) -> bool:
    if fnmatch.fnmatch(relpath, pattern):
        return True
    # "**/*.py" 也应匹配顶层 a.py（递归 glob 语义，fnmatch 的 * 不跨 /）
    return pattern.startswith("**/") and fnmatch.fnmatch(relpath, pattern[3:])


def format_rules_injection(relpath: str, rules: dict[str, str]) -> str:
    """Join the prompts of every rule whose glob matches `relpath`; "" if none."""
    matched = [prompt for pattern, prompt in rules.items()
               if _match_glob(relpath, pattern)]
    if not matched:
        return ""
    lines = ["【项目规则】以下规则适用于本文件，审查时请遵守："]
    lines += [f"- {p}" for p in matched]
    return "\n".join(lines)
