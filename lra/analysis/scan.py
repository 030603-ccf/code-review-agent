"""Project scanner — builds a deterministic index (file list + symbol table).

Zero LLM. Python uses the stdlib `ast`; other registered languages fall back to
the heuristics in languages.py. Files that fail to parse are still indexed and
marked with `parse_error` rather than dropped.
"""

import ast
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from lra.analysis.languages import LANG_BY_EXT, extract_symbols
from lra.ignore import path_is_ignored


def _signature_of(node) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        return f"def {node.name}({ast.unparse(node.args)}){ret}"
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        return f"class {node.name}({bases})" if bases else f"class {node.name}"
    return ""


def _extract(tree: ast.AST) -> tuple[list[dict], list[str]]:
    symbols: list[dict] = []
    imports: list[str] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append({
                "kind": "function", "name": node.name,
                "line_start": node.lineno, "line_end": node.end_lineno,
                "signature": _signature_of(node),
                "docstring": ast.get_docstring(node) or "",
            })
        elif isinstance(node, ast.ClassDef):
            symbols.append({
                "kind": "class", "name": node.name,
                "line_start": node.lineno, "line_end": node.end_lineno,
                "signature": _signature_of(node),
                "docstring": ast.get_docstring(node) or "",
            })
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append({
                        "kind": "method", "name": f"{node.name}.{sub.name}",
                        "line_start": sub.lineno, "line_end": sub.end_lineno,
                        "signature": _signature_of(sub),
                        "docstring": ast.get_docstring(sub) or "",
                    })
        elif isinstance(node, ast.Assign):
            targets = ", ".join(ast.unparse(t) for t in node.targets)
            symbols.append({
                "kind": "variable", "name": targets,
                "line_start": node.lineno, "line_end": node.end_lineno,
                "signature": ast.unparse(node)[:80], "docstring": "",
            })
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            symbols.append({
                "kind": "variable", "name": ast.unparse(node.target),
                "line_start": node.lineno, "line_end": node.end_lineno,
                "signature": ast.unparse(node)[:80], "docstring": "",
            })
        elif isinstance(node, ast.Import):
            imports.extend(f"import {a.name}" for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = ", ".join(a.name for a in node.names)
            imports.append(f"from {module} import {names}")

    return symbols, imports


def scan_file(path: Path, relpath: str) -> dict:
    # 一次读盘：字节 → sha1 / 大小，再解码成文本 → 行数。旧实现 read_text +
    # read_bytes + stat 三趟 I/O；大项目下是纯浪费，且 sha1 与 text 可能读到
    # 不同快照（文件在两次读之间被改），导致缓存键与实际文本不一致。
    data = path.read_bytes()
    text = data.decode("utf-8", errors="replace")
    entry = {
        "relpath": relpath,
        "sha1": hashlib.sha1(data).hexdigest(),
        "size_bytes": len(data),
        "line_count": len(text.splitlines()),
        "symbols": [], "imports": [], "parse_error": None,
    }
    if path.suffix.lower() == ".py":
        try:
            tree = ast.parse(text)
            entry["symbols"], entry["imports"] = _extract(tree)
        except SyntaxError as e:
            entry["parse_error"] = f"{e.msg} (line {e.lineno})"
    else:
        entry["symbols"] = extract_symbols(relpath, text.splitlines())
    return entry


def scan_project(root: str | Path) -> dict:
    root = Path(root).resolve()
    exts = {".py"} | set(LANG_BY_EXT)
    files = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        if path_is_ignored(p.parts):
            continue
        files.append(scan_file(p, p.relative_to(root).as_posix()))

    return {
        "root": str(root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(files),
        "files": files,
    }
