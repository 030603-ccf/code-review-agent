"""anti_pattern_scanner.py —— 零 LLM 语言反模式检测器。

检测各语言的高频陷阱——这些是已知的、确定的模式，
不需要 LLM 来判断。每条发现直接注入审查流水线。

覆盖：
  Java:   == 比较 String、int 除法截断、Arrays.asList 不可变
  Python: 可变默认参数（委托 structural.py）、is vs 字面量
  JS:     == vs ===、parseInt 无基数、sort 默认字符串排序
"""

import re
from pathlib import Path

_CONFIDENCE = 0.95

# ========== Java 反模式 ==========

# String 用 == 比较（不是 .equals）
_JAVA_STRING_EQ_RE = re.compile(
    r'(?:(\w+)\s*==\s*("[^"]*")|("[^"]*")\s*==\s*(\w+))',
)

# 整数除法在需要浮点结果的上下文中
_JAVA_INT_DIV_RE = re.compile(
    r"(double|float)\s+\w+\s*=\s*(\w+\s*/\s*\w+)(?!\s*\(?\s*(?:double|float))",
)

# Arrays.asList 结果被 add/remove
_JAVA_ASLIST_MUTATE_RE = re.compile(
    r"Arrays\.asList\([^)]+\)\s*\.\s*(add|remove|clear)\s*\(",
)


def _scan_java(relpath: str, content: str) -> list[dict]:
    """Java 反模式扫描。"""
    findings: list[dict] = []

    # String == 比较
    for m in _JAVA_STRING_EQ_RE.finditer(content):
        line_no = content[:m.start()].count("\n") + 1
        findings.append({
            "id": "", "category": "best_practice", "severity": "high",
            "file_path": relpath, "line_start": line_no, "line_end": line_no,
            "title": "String 使用了 == 比较而非 .equals()",
            "description": "== 比较的是对象引用地址而非字符串内容，"
                           "对于通过 new/StringBuilder/substring 等方式创建的字符串，"
                           "== 会返回意外的 false。",
            "evidence": m.group(0).strip(),
            "suggestion": "使用 .equals() 或 Objects.equals() 比较字符串内容。",
            "confidence": _CONFIDENCE,
        })

    # int 除法截断
    for m in _JAVA_INT_DIV_RE.finditer(content):
        line_no = content[:m.start()].count("\n") + 1
        findings.append({
            "id": "", "category": "best_practice", "severity": "medium",
            "file_path": relpath, "line_start": line_no, "line_end": line_no,
            "title": "整数除法可能导致精度丢失",
            "description": f"将整数除法结果赋给 {m.group(1)} 类型变量，"
                           f"除法会在整数运算中截断小数部分。",
            "evidence": m.group(0).strip(),
            "suggestion": "至少将一个操作数强转为 double：`(double) a / b`。",
            "confidence": 0.85,  # 可能是有意为之
        })

    # Arrays.asList 不可变
    for m in _JAVA_ASLIST_MUTATE_RE.finditer(content):
        line_no = content[:m.start()].count("\n") + 1
        findings.append({
            "id": "", "category": "best_practice", "severity": "high",
            "file_path": relpath, "line_start": line_no, "line_end": line_no,
            "title": "对 Arrays.asList() 结果调用修改操作",
            "description": "Arrays.asList() 返回的是固定大小的 List，"
                           "调用 add/remove/clear 会抛出 UnsupportedOperationException。",
            "evidence": m.group(0).strip(),
            "suggestion": "使用 new ArrayList<>(Arrays.asList(...)) 包装一层。",
            "confidence": _CONFIDENCE,
        })

    for i, f in enumerate(findings):
        f["id"] = f"A{i + 1}"
    return findings


# ========== Python 反模式 ==========

# is 与字面量比较（is 5, is "hello" 等不可靠用法）
_PYTHON_IS_LITERAL_RE = re.compile(
    r"(\w+)\s+is\s+(?:\d+|['\"][^'\"]*['\"])",
)

# 生成器/迭代器被多次消费的典型模式
_PY_GENERATOR_REUSE_RE = re.compile(
    r"(\w+)\s*=\s*\([^)]+\)\s*\n.*\bfor\b.*\1\b.*\n.*\bfor\b.*\1\b",
    re.DOTALL,
)


def _scan_python(relpath: str, content: str) -> list[dict]:
    """Python 反模式扫描。"""
    findings: list[dict] = []

    # is 与字面量比较
    for m in _PYTHON_IS_LITERAL_RE.finditer(content):
        line_no = content[:m.start()].count("\n") + 1
        findings.append({
            "id": "", "category": "best_practice", "severity": "medium",
            "file_path": relpath, "line_start": line_no, "line_end": line_no,
            "title": "使用 is 比较字面量",
            "description": "`is` 比较对象身份（内存地址），而非值。"
                           "对数字/字符串使用 `is` 的行为由 CPython 实现细节决定，"
                           "不可移植，可能随时改变。",
            "evidence": m.group(0).strip(),
            "suggestion": "将 `is` 改为 `==` 进行值比较。"
                         "判断 None 时使用 `is None` 是正确的惯用法。",
            "confidence": 0.90,
        })

    for i, f in enumerate(findings):
        f["id"] = f"A{i + 1}"
    return findings


# ========== JavaScript 反模式 ==========

# == 而非 ===（排除 null == undefined 这种合理用法）
_JS_EQEQ_RE = re.compile(
    r"(?<!\w)(\w[\w.]*)\s*==\s*(?!null\b|undefined\b)(\w[\w.]*)",
)

# parseInt 没有显式基数
_JS_PARSEINT_RE = re.compile(r"parseInt\s*\(\s*[^,)]+\s*\)(?!\s*,)")

# sort() 默认字符串排序
_JS_SORT_RE = re.compile(r"\.sort\s*\(\s*\)")


def _scan_javascript(relpath: str, content: str) -> list[dict]:
    """JavaScript/TypeScript 反模式扫描。"""
    findings: list[dict] = []

    # == 而非 ===
    for m in _JS_EQEQ_RE.finditer(content):
        line_no = content[:m.start()].count("\n") + 1
        findings.append({
            "id": "", "category": "best_practice", "severity": "high",
            "file_path": relpath, "line_start": line_no, "line_end": line_no,
            "title": "使用了 == 而非 ===（隐式类型转换）",
            "description": "== 会触发隐式类型转换（如 '0' == false 为 true），"
                           "结果难以预测。应使用 ===/!== 进行严格比较。",
            "evidence": m.group(0).strip(),
            "suggestion": "将 == 替换为 ===，将 != 替换为 !==。",
            "confidence": _CONFIDENCE,
        })

    # parseInt 无基数
    for m in _JS_PARSEINT_RE.finditer(content):
        line_no = content[:m.start()].count("\n") + 1
        findings.append({
            "id": "", "category": "best_practice", "severity": "medium",
            "file_path": relpath, "line_start": line_no, "line_end": line_no,
            "title": "parseInt 未指定基数",
            "description": "parseInt 会自动识别十六进制（0x 前缀），"
                           "且旧引擎中 '08' 可能被当作八进制解析。"
                           "建议始终传第二个参数指定基数。",
            "evidence": m.group(0).strip(),
            "suggestion": "改为 parseInt(x, 10) 显式指定十进制。",
            "confidence": _CONFIDENCE,
        })

    # sort() 无比较函数
    for m in _JS_SORT_RE.finditer(content):
        line_no = content[:m.start()].count("\n") + 1
        findings.append({
            "id": "", "category": "best_practice", "severity": "medium",
            "file_path": relpath, "line_start": line_no, "line_end": line_no,
            "title": "Array.sort() 未传比较函数",
            "description": "sort() 默认按字符串排序，[10, 9].sort() 返回 [10, 9]。"
                           "排序数字数组时必须传 (a,b)=>a-b。",
            "evidence": m.group(0).strip(),
            "suggestion": "对数字数组使用 .sort((a, b) => a - b)。",
            "confidence": _CONFIDENCE,
        })

    for i, f in enumerate(findings):
        f["id"] = f"A{i + 1}"
    return findings


# ========== 统一入口 ==========

_LANG_SCANNERS = {
    "python": _scan_python,
    "java": _scan_java,
    "javascript": _scan_javascript,
}


def scan_anti_patterns(relpath: str, content: str, lang: str = "") -> list[dict]:
    """扫描单文件的语言反模式。返回 Finding 兼容 dict 列表。"""
    lang = _norm_lang(relpath, lang)
    scanner = _LANG_SCANNERS.get(lang)
    if scanner is None:
        return []
    return scanner(relpath, content)


def _norm_lang(relpath: str, lang: str) -> str:
    if lang in ("py", "python"):
        return "python"
    if lang in ("java",):
        return "java"
    if lang in ("js", "ts", "jsx", "tsx", "javascript", "typescript"):
        return "javascript"
    ext = Path(relpath).suffix.lower()
    if ext in (".py",):
        return "python"
    if ext in (".java",):
        return "java"
    if ext in (".js", ".ts", ".jsx", ".tsx"):
        return "javascript"
    return ""
