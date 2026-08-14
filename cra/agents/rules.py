"""规则注入系统 —— 参考阿里 Open Code Review 的四层规则优先级链。

设计：
    用户可以在项目根目录放 .codereview/rules.json，按 glob 模式匹配文件，
    把匹配到的规则文本注入到 reviewer 的 system prompt 中。
    这样不同文件类型/路径拿到的审查指令不同——比通用 prompt 更精准。

优先级（高→低）：
    1. CLI --rule 指定的路径
    2. 项目级 <project_root>/.codereview/rules.json
    3. 无（不注入）

规则文件格式：
    {
      "rules": [
        {"path": "**/*.py", "rule": "检查生成器二次消费、可变默认参数"},
        {"path": "**/*.java", "rule": "所有方法参数必须 null check"},
        {"path": "api/**/*.ts", "rule": "所有端点必须有输入校验和错误处理"}
      ]
    }

path 支持 fnmatch 风格的 glob（** 跨目录、* 单层、? 单字符）。
"""

import fnmatch
import json
from pathlib import Path

# 缓存：避免同一 run 内多次读盘
_rules_cache: dict[str, list[dict]] = {}


def load_rules(project_root: str | Path, cli_rule_path: str | None = None) -> list[dict]:
    """加载规则列表。

    Args:
        project_root: 被审查项目的根目录
        cli_rule_path: CLI --rule 指定的规则文件路径（最高优先级）

    Returns:
        规则列表 [{"path": glob, "rule": 规则文本}, ...]
    """
    # 优先级 1：CLI 指定
    if cli_rule_path:
        return _load_from_file(cli_rule_path)

    # 优先级 2：项目级
    project_rules = Path(project_root) / ".codereview" / "rules.json"
    if project_rules.is_file():
        return _load_from_file(str(project_rules))

    return []


def _load_from_file(path: str) -> list[dict]:
    """从 JSON 文件加载规则（带缓存）。"""
    if path in _rules_cache:
        return _rules_cache[path]

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        rules = data.get("rules", [])
        # 校验格式：每条必须有 path 和 rule
        valid = [r for r in rules if isinstance(r, dict) and "path" in r and "rule" in r]
        _rules_cache[path] = valid
        return valid
    except (json.JSONDecodeError, OSError):
        _rules_cache[path] = []
        return []


def match_rules(file_path: str, rules: list[dict]) -> list[str]:
    """为指定文件匹配所有命中的规则文本。

    Args:
        file_path: 文件的相对路径（如 "src/utils/helpers.py"）
        rules: load_rules() 的产出

    Returns:
        命中的规则文本列表
    """
    matched = []
    for r in rules:
        pattern = r["path"]
        # fnmatch 不支持 ** 跨目录，手动处理：
        # 把 **/ 替换为"任意前缀"的匹配逻辑
        if "**" in pattern:
            # "**/*.py" 应匹配 "a/b/c.py" 和 "c.py"
            # 策略：去掉 "**/" 前缀后做 basename 匹配，或保留完整路径匹配
            if pattern.startswith("**/"):
                suffix_pattern = pattern[3:]  # "*.py"
                # 匹配文件名（任何深度）
                if fnmatch.fnmatch(file_path, pattern.replace("**/", "")):
                    matched.append(r["rule"])
                elif fnmatch.fnmatch(file_path.split("/")[-1], suffix_pattern):
                    matched.append(r["rule"])
                elif fnmatch.fnmatch(file_path, pattern):
                    matched.append(r["rule"])
            else:
                # "api/**/*.ts" 这种中间有 ** 的
                if fnmatch.fnmatch(file_path, pattern):
                    matched.append(r["rule"])
                # 兜底：把 ** 当 * 试一次
                elif fnmatch.fnmatch(file_path, pattern.replace("**", "*")):
                    matched.append(r["rule"])
        else:
            if fnmatch.fnmatch(file_path, pattern):
                matched.append(r["rule"])

    # 去重（同一规则可能被多条 pattern 命中）
    return list(dict.fromkeys(matched))


def format_rules_injection(file_path: str, rules: list[dict]) -> str:
    """为指定文件生成注入 prompt 的规则文本。

    Args:
        file_path: 文件相对路径
        rules: load_rules() 的产出

    Returns:
        格式化的规则注入字符串（空串 = 无匹配规则）
    """
    if not rules:
        return ""

    matched = match_rules(file_path, rules)
    if not matched:
        return ""

    lines = ["【审查规则（必须遵守）】"]
    for i, rule in enumerate(matched, 1):
        lines.append(f"{i}. {rule}")

    return "\n".join(lines)
