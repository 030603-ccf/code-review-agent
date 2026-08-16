"""Zero-LLM language anti-pattern scanner (known, deterministic traps)."""

import re

from lra.tools import normalize_lang


def _f(relpath, line_no, category, severity, title, desc, evidence,
       suggestion, confidence):
    return {
        "id": "", "category": category, "severity": severity,
        "file_path": relpath, "line_start": line_no, "line_end": line_no,
        "title": title, "description": desc, "evidence": evidence[:200],
        "suggestion": suggestion, "confidence": confidence,
    }


def _line(content: str, m: re.Match) -> int:
    return content[:m.start()].count("\n") + 1


# ---- Java ----
_JAVA_STRING_EQ_RE = re.compile(
    r'(?:(\w+)\s*==\s*("[^"]*")|("[^"]*")\s*==\s*(\w+))')
_JAVA_INT_DIV_RE = re.compile(
    r"(double|float)\s+\w+\s*=\s*(\w+\s*/\s*\w+)(?!\s*\(?\s*(?:double|float))")
_JAVA_ASLIST_RE = re.compile(r"Arrays\.asList\([^)]+\)\s*\.\s*(add|remove|clear)\s*\(")


def _scan_java(relpath, content):
    out = []
    for m in _JAVA_STRING_EQ_RE.finditer(content):
        out.append(_f(relpath, _line(content, m), "best_practice", "high",
                      "String 用 == 比较", "== 比较引用而非内容，非字面量字符串会得到意外 false。",
                      m.group(0).strip(), "用 .equals() 或 Objects.equals()。", 0.9))
    for m in _JAVA_INT_DIV_RE.finditer(content):
        out.append(_f(relpath, _line(content, m), "best_practice", "medium",
                      "整数除法精度丢失", "整数除法在赋值给浮点变量前已截断小数。",
                      m.group(0).strip(), "至少一个操作数强转 double。", 0.85))
    for m in _JAVA_ASLIST_RE.finditer(content):
        out.append(_f(relpath, _line(content, m), "best_practice", "high",
                      "对 Arrays.asList 结果做修改", "Arrays.asList 返回固定大小 List，add/remove 会抛异常。",
                      m.group(0).strip(), "用 new ArrayList<>(Arrays.asList(...))。", 0.9))
    return out


# ---- Python ----
_PY_IS_LITERAL_RE = re.compile(r"(\w+)\s+is\s+(?:\d+|['\"][^'\"]*['\"])")


def _scan_python(relpath, content):
    out = []
    for m in _PY_IS_LITERAL_RE.finditer(content):
        out.append(_f(relpath, _line(content, m), "best_practice", "medium",
                      "用 is 比较字面量", "is 比较对象身份而非值，对字面量不可移植。",
                      m.group(0).strip(), "改为 == 比较值；判断 None 仍用 is None。", 0.7))
    return out


# ---- JavaScript ----
_JS_EQEQ_RE = re.compile(r"(?<!\w)(\w[\w.]*)\s*==\s*(?!null\b|undefined\b)(\w[\w.]*)")
_JS_PARSEINT_RE = re.compile(r"parseInt\s*\(\s*[^,)]+\s*\)(?!\s*,)")
_JS_SORT_RE = re.compile(r"\.sort\s*\(\s*\)")


def _scan_javascript(relpath, content):
    out = []
    for m in _JS_EQEQ_RE.finditer(content):
        out.append(_f(relpath, _line(content, m), "best_practice", "high",
                      "用了 == 而非 ===", "== 触发隐式类型转换，结果难预测。",
                      m.group(0).strip(), "改用 === / !==。", 0.6))
    for m in _JS_PARSEINT_RE.finditer(content):
        out.append(_f(relpath, _line(content, m), "best_practice", "medium",
                      "parseInt 未指定基数", "默认可能按十六进制/八进制解析。",
                      m.group(0).strip(), "改为 parseInt(x, 10)。", 0.7))
    for m in _JS_SORT_RE.finditer(content):
        out.append(_f(relpath, _line(content, m), "best_practice", "medium",
                      "Array.sort() 未传比较函数", "默认按字符串排序，[10,9].sort() 得 [10,9]。",
                      m.group(0).strip(), "数字数组用 .sort((a,b)=>a-b)。", 0.6))
    return out


_LANG_SCANNERS = {"python": _scan_python, "java": _scan_java,
                  "javascript": _scan_javascript}


def scan_anti_patterns(relpath: str, content: str, lang: str = "") -> list[dict]:
    lang = normalize_lang(relpath, lang)
    scanner = _LANG_SCANNERS.get(lang)
    if scanner is None:
        return []
    findings = scanner(relpath, content)
    for i, f in enumerate(findings):
        f["id"] = f"A{i + 1}"
    return findings
