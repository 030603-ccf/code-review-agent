"""security_scanner.py —— 零 LLM 安全模式检测器。

纯正则扫描，不调模型、不烧 token。检测以下类型：
  - 硬编码密钥/密码
  - 危险函数调用（eval/exec/subprocess/os.system/pickle/yaml）
  - SQL 注入模式（字符串拼接/F-string 构造 SQL）
  - 裸异常捕获

每条发现输出与 Finding schema 兼容的 dict，直接 merge 进 LLM 结果。
"""

import re
from pathlib import Path
from typing import Dict, List, Tuple

# ========== 硬编码密钥/密码 ==========
_HARDCODED_SECRET_RE = re.compile(
    r"""(?ix)                                   # 忽略大小写、允许注释
    ^\s*                                        # 行首空白
    (?:                                         # 变量名含这些关键词
        (?:DB_)?PASSWORD|PASSWD|PWD|API_KEY|API_SECRET|SECRET_KEY|
        ACCESS_KEY|TOKEN|AUTH_TOKEN|PRIVATE_KEY|DB_PASS
    )
    \s*=\s*                                     # 赋值
    ["']                                        # 字面量字符串开始
    (                                           # 值：至少 4 个非空白字符
        (?!\s*(?:os\.environ|config\[|getenv|os\.getenv|\$|\{|\())
        [^"'\n]{4,}
    )
    ["']                                        # 字符串结束
""",
)

# ========== 危险函数调用 ==========
_DANGEROUS_CALLS: dict[str, list[tuple[str, str]]] = {
    "python": [
        (r"\beval\s*\([^)]*\)", "eval() 执行动态字符串，可能导致任意代码执行"),
        (r"\bexec\s*\([^)]*\)", "exec() 执行动态代码字符串"),
        (r"\bsubprocess\.(?:call|run|Popen)\s*\([^)]*shell\s*=\s*True",
         "subprocess 使用 shell=True，可能导致命令注入"),
        (r"\bos\.system\s*\([^)]*\)", "os.system() 执行 shell 命令"),
        (r"\bpickle\.(?:load|loads)\s*\(", "pickle 反序列化任意对象"),
        (r"\byaml\.load\s*\(\s*(?!.*SafeLoader)", "yaml.load() 未使用 SafeLoader"),
    ],
    "java": [
        (r"\bRuntime\.getRuntime\(\)\.exec\s*\(", "Runtime.exec() 执行外部命令"),
    ],
    "javascript": [
        (r"\beval\s*\([^)]*\)", "eval() 执行动态代码"),
        (r"\.innerHTML\s*=\s*[^;]*\+", "innerHTML 拼接可能导致 XSS"),
    ],
}

# ========== SQL 注入 ==========
_SQL_INJECTION_RE = re.compile(
    r"""(?ix)
    (?: \.execute\s*\(\s*["'][^"']* | \.raw\s*\(\s*["'][^"']* )
    .*?
    (?:\+|\.format\(|f["\'])
""",
)

# ========== 裸异常 ==========
_BARE_EXCEPT_RE = re.compile(
    r"(?:except\s*:|except\s*Exception\s*:|catch\s*\(\s*Exception|catch\s*\(\s*Throwable"
    r")(?!.*(?:log|logger|logging|print|raise|trace|warn|error))",
)

_CONFIDENCE = 0.95


def scan_security(relpath: str, content: str, lang: str = "") -> list[dict]:
    """扫描单个文件的安全问题。返回 Finding 兼容 dict 列表。"""
    findings: list[dict] = []
    lang = _norm_lang(lang, relpath)

    # 1. 硬编码密钥
    for m in _HARDCODED_SECRET_RE.finditer(content):
        line_no = content[:m.start()].count("\n") + 1
        var_name = m.group(0).split("=")[0].strip()
        findings.append({
            "id": "", "category": "security", "severity": "critical",
            "file_path": relpath, "line_start": line_no, "line_end": line_no,
            "title": "硬编码密钥/密码",
            "description": f"变量 {var_name} 被赋值为字面量字符串。密钥应通过环境变量或配置文件注入。",
            "evidence": m.group(0).strip()[:200],
            "suggestion": f"将 {var_name} 移到环境变量或密钥管理服务中。",
            "confidence": _CONFIDENCE,
        })

    # 2. 危险函数调用
    for pattern, desc in _DANGEROUS_CALLS.get(lang, []):
        for m in re.finditer(pattern, content, re.IGNORECASE):
            line_no = content[:m.start()].count("\n") + 1
            findings.append({
                "id": "", "category": "security", "severity": "critical",
                "file_path": relpath, "line_start": line_no, "line_end": line_no,
                "title": desc.split("，")[0],
                "description": desc,
                "evidence": m.group(0).strip(),
                "suggestion": "使用经过校验的输入，或替换为安全的替代方案。",
                "confidence": _CONFIDENCE,
            })

    # 3. SQL 注入
    for m in _SQL_INJECTION_RE.finditer(content):
        line_no = content[:m.start()].count("\n") + 1
        findings.append({
            "id": "", "category": "security", "severity": "critical",
            "file_path": relpath, "line_start": line_no, "line_end": line_no,
            "title": "SQL 注入风险",
            "description": "SQL 语句使用字符串拼接/F-string 构造，使用参数化查询替代。",
            "evidence": m.group(0).strip()[:200],
            "suggestion": "使用参数化查询（Python: %s 占位符；Java: PreparedStatement；JS: ? 占位符）。",
            "confidence": _CONFIDENCE,
        })

    # 4. 裸异常
    for m in _BARE_EXCEPT_RE.finditer(content):
        line_no = content[:m.start()].count("\n") + 1
        findings.append({
            "id": "", "category": "best_practice", "severity": "medium",
            "file_path": relpath, "line_start": line_no, "line_end": line_no,
            "title": "裸异常捕获（静默吞错）",
            "description": "异常被捕获但未做任何处理，故障发生时无法排查。",
            "evidence": m.group(0).strip()[:100],
            "suggestion": "在 except/catch 块中至少添加日志记录或重新抛出异常。",
            "confidence": _CONFIDENCE,
        })

    for i, f in enumerate(findings):
        f["id"] = f"S{i + 1}"
    return findings


def _norm_lang(lang: str, relpath: str) -> str:
    """把语言标签规范化为 python/java/javascript。"""
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
