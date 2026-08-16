"""Zero-LLM security pattern scanner.

Honesty rules: each rule carries a confidence that reflects its false-positive
rate — regex heuristics never claim 0.95. Patterns are written narrowly.
"""

import re

from lra.tools import normalize_lang

# Hardcoded secret: assignment of a literal string (>=8 chars) to a
# secret-looking name. env/config indirection (os.environ, getenv, config[...])
# never matches because those values are not quoted string literals.
_HARDCODED_SECRET_RE = re.compile(
    r'(?im)^\s*(?:(?:DB_)?PASSWORD|PASSWD|API_KEY|API_SECRET|SECRET_KEY|'
    r'ACCESS_KEY|AUTH_TOKEN|PRIVATE_KEY|DB_PASS)\s*=\s*'
    r'["\'][^"\'\n]{8,}["\']'
)

_DANGEROUS_CALLS: dict[str, list[tuple[str, str]]] = {
    "python": [
        (r"\beval\s*\(", "eval() 执行动态字符串，可能导致任意代码执行"),
        (r"\bexec\s*\(", "exec() 执行动态代码"),
        (r"\bsubprocess\.(?:call|run|Popen)\s*\([^)]*shell\s*=\s*True",
         "subprocess 使用 shell=True，可能导致命令注入"),
        (r"\bos\.system\s*\(", "os.system() 执行 shell 命令"),
        (r"\bpickle\.(?:load|loads)\s*\(", "pickle 反序列化任意对象"),
        (r"\byaml\.load\s*\(\s*(?!.*SafeLoader)", "yaml.load() 未使用 SafeLoader"),
    ],
    "java": [
        (r"\bRuntime\.getRuntime\(\)\.exec\s*\(", "Runtime.exec() 执行外部命令"),
    ],
    "javascript": [
        (r"\beval\s*\(", "eval() 执行动态代码"),
        (r"\.innerHTML\s*=\s*[^;]*\+", "innerHTML 拼接可能导致 XSS"),
    ],
}

# SQL built by concatenation / interpolation — flag only obvious same-statement
# string building, not every execute() call.
_SQL_INJECTION_RES = [
    re.compile(r"\.(?:execute|raw|executemany)\s*\(\s*f[\"']", re.IGNORECASE),
    re.compile(r"\.(?:execute|raw|executemany)\s*\(\s*[\"'][^\"']*[\"']\s*\+"),
    re.compile(r"\.(?:execute|raw|executemany)\s*\(\s*[\"'][^\"']*[\"']\s*\.\s*format\s*\("),
    re.compile(r"\.(?:execute|raw|executemany)\s*\(\s*[\"'][^\"']*[\"']\s*%"),
]

_BARE_EXCEPT_RE = re.compile(r"^\s*except\s*:", re.MULTILINE)


def _finding(relpath, line_no, category, severity, title, desc, evidence,
             suggestion, confidence):
    return {
        "id": "", "category": category, "severity": severity,
        "file_path": relpath, "line_start": line_no, "line_end": line_no,
        "title": title, "description": desc, "evidence": evidence[:200],
        "suggestion": suggestion, "confidence": confidence,
    }


def scan_security(relpath: str, content: str, lang: str = "") -> list[dict]:
    lang = normalize_lang(relpath, lang)
    findings: list[dict] = []

    for m in _HARDCODED_SECRET_RE.finditer(content):
        line_no = content[:m.start()].count("\n") + 1
        var = m.group(0).split("=")[0].strip()
        findings.append(_finding(
            relpath, line_no, "security", "critical", "硬编码密钥/密码",
            f"变量 {var} 被赋值为字面量字符串，密钥应走环境变量或密钥管理。",
            m.group(0).strip(),
            f"将 {var} 移到环境变量或密钥管理服务。", 0.7))

    for pattern, desc in _DANGEROUS_CALLS.get(lang, []):
        for m in re.finditer(pattern, content, re.IGNORECASE):
            line_no = content[:m.start()].count("\n") + 1
            findings.append(_finding(
                relpath, line_no, "security", "critical",
                desc.split("，")[0], desc, m.group(0).strip(),
                "校验输入，或替换为安全替代方案。", 0.8))

    for regex in _SQL_INJECTION_RES:
        for m in regex.finditer(content):
            line_no = content[:m.start()].count("\n") + 1
            findings.append(_finding(
                relpath, line_no, "security", "critical", "SQL 注入风险",
                "SQL 语句由字符串拼接/插值构造，应使用参数化查询。",
                m.group(0).strip(),
                "使用参数化查询（占位符），不要拼接 SQL。", 0.6))

    for m in _BARE_EXCEPT_RE.finditer(content):
        line_no = content[:m.start()].count("\n") + 1
        findings.append(_finding(
            relpath, line_no, "best_practice", "medium", "裸异常捕获",
            "异常被捕获但未处理，故障发生时无法排查。",
            m.group(0).strip(),
            "在 except 块中记录日志或重新抛出。", 0.6))

    for i, f in enumerate(findings):
        f["id"] = f"S{i + 1}"
    return findings
