"""Heuristic symbol extraction for non-Python languages.

Python uses the stdlib `ast` (see scan.py); everything else uses regex to find
a symbol's start line and brace/paren counting to find its end line.

Policy: prefer under-extraction. A missed symbol is harmless (the file falls
back to window chunking); a wrong one corrupts the index and misleads the LLM.
"""

import re
from pathlib import Path

LANG_BY_EXT = {
    ".py": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".jsx": "javascript", ".tsx": "typescript",
    ".vue": "vue",
    ".java": "java",
    ".cs": "csharp",
    ".go": "go",
    ".rs": "rust",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
    ".php": "php",
    ".rb": "ruby",
}

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

# Each language maps to a list of (regex, kind). The regex must capture the
# symbol NAME in group "name"; group "kind" is taken from the tuple.
_DEF_PATTERNS: dict[str, list[tuple[re.Pattern, str]]] = {}


def _p(lang: str, pattern: str, kind: str) -> None:
    _DEF_PATTERNS.setdefault(lang, []).append(
        (re.compile(pattern, re.MULTILINE), kind))


# Java: method/constructor/class/interface/enum
_p("java", rf"^\s*(?:public|private|protected|static|final|abstract|synchronized|\s)*"
          rf"(?:[\w<>\[\],.?]+\s+)+(?P<name>{_IDENT})\s*\([^;]*\)\s*\{{?", "function")
_p("java", rf"^\s*(?:public|private|protected|static|final|abstract|\s)*"
          rf"(?:class|interface|enum|record)\s+(?P<name>{_IDENT})", "class")

# JavaScript / TypeScript: function decl, arrow/const-assigned fn, class, method
_p("javascript", rf"^\s*(?:async\s+)?function\s*\*?\s*(?P<name>{_IDENT})\s*\(", "function")
_p("javascript", rf"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>{_IDENT})\s*=\s*"
                  rf"(?:async\s*)?(?:function|\()", "function")
_p("javascript", rf"^\s*(?:export\s+)?class\s+(?P<name>{_IDENT})", "class")
_p("javascript", rf"^\s*(?:async\s+)?(?P<name>{_IDENT})\s*\([^)]*\)\s*\{{?\s*$", "function")
_p("typescript", rf"^\s*(?:async\s+)?function\s*\*?\s*(?P<name>{_IDENT})\s*\(", "function")
_p("typescript", rf"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>{_IDENT})\s*[:=]", "function")
_p("typescript", rf"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(?P<name>{_IDENT})", "class")
_p("typescript", rf"^\s*(?:async\s+)?(?P<name>{_IDENT})\s*\([^)]*\)\s*\{{?\s*$", "function")

# Go / Rust: func / fn
_p("go", rf"^func\s+(?:\([^)]*\)\s*)?(?P<name>{_IDENT})\s*\(", "function")
_p("go", rf"^type\s+(?P<name>{_IDENT})\s+(?:struct|interface)\b", "class")
_p("rust", rf"^\s*(?:pub\s+)?fn\s+(?P<name>{_IDENT})\s*\(", "function")
_p("rust", rf"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+(?P<name>{_IDENT})", "class")

# C / C++ / C# / PHP: function definitions and classes
_p("c", rf"^\s*(?:static\s+)?[\w\s\*]+?(?P<name>{_IDENT})\s*\([^;]*\)\s*\{{?\s*$", "function")
_p("cpp", rf"^\s*(?:[\w:<>&\*~]+\s+)+(?P<name>~?{_IDENT})\s*\([^;]*\)", "function")
_p("cpp", rf"^\s*(?:class|struct|namespace)\s+(?P<name>{_IDENT})", "class")
_p("csharp", rf"^\s*(?:public|private|protected|internal|static|\s)*"
             rf"(?:[\w<>\[\],.?]+\s+)+(?P<name>{_IDENT})\s*\([^)]*\)", "function")
_p("csharp", rf"^\s*(?:public|private|protected|internal|\s)*(?:class|struct|interface|enum)\s+"
             rf"(?P<name>{_IDENT})", "class")
_p("php", rf"^\s*(?:public|private|protected|static|\s)*function\s+(?P<name>{_IDENT})\s*\(", "function")
_p("php", rf"^\s*(?:abstract\s+)?(?:class|interface|trait)\s+(?P<name>{_IDENT})", "class")

# Ruby: def / class / module
_p("ruby", rf"^\s*def\s+(?P<name>{_IDENT}[!?=]?)", "function")
_p("ruby", rf"^\s*(?:class|module)\s+(?P<name>{_IDENT})", "class")


def _find_end_brace(lines: list[str], start: int) -> int:
    """From a 0-based `start` line, count braces until balanced. Return 1-based end."""
    depth = 0
    opened = False
    for i in range(start, len(lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if "{" in lines[i]:
            opened = True
        if opened and depth <= 0:
            return i + 1
    return len(lines)


def extract_symbols(relpath: str, lines: list[str]) -> list[dict]:
    """Extract top-level symbols heuristically. Returns list of symbol dicts."""
    ext = Path(relpath).suffix.lower()
    lang = LANG_BY_EXT.get(ext, "")
    patterns = _DEF_PATTERNS.get(lang, [])
    if not patterns:
        return []

    text = "\n".join(lines)
    symbols: list[dict] = []
    for regex, kind in patterns:
        for m in regex.finditer(text):
            name = m.group("name")
            line_start = text[:m.start()].count("\n") + 1
            if kind in ("function", "class"):
                line_end = _find_end_brace(lines, line_start - 1)
            else:
                line_end = line_start
            signature = lines[line_start - 1].strip()
            symbols.append({
                "kind": kind, "name": name,
                "line_start": line_start, "line_end": line_end,
                "signature": signature, "docstring": "",
            })

    # drop duplicates (same name + same start line) that overlapping regexes create
    seen: set[tuple] = set()
    out: list[dict] = []
    for s in symbols:
        key = (s["name"], s["line_start"])
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return sorted(out, key=lambda s: s["line_start"])
