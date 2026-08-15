"""Cross-file dependency graph — deterministic, zero LLM.

import / require are syntactic facts, not "understanding"; regex does them
100% accurately for free. The graph lets the reviewer see "who this file
depends on and who depends on it", which catches cross-file contract bugs
(parameter type mismatches, unpropagated exceptions).
"""

import posixpath
import re
from pathlib import PurePosixPath


def _py_module_to_paths(module: str, all_files: set[str]) -> list[str]:
    parts = module.split(".")
    candidates = [
        "/".join(parts) + ".py",
        "/".join(parts) + "/__init__.py",
    ]
    if len(parts) == 1:
        candidates.append(parts[0] + "/__init__.py")
    return [c for c in candidates if c in all_files]


def _extract_py_imports(imports: list[str]) -> list[str]:
    modules = []
    for imp in imports:
        if imp.startswith("from "):
            m = re.match(r"from\s+([\w.]+)\s+import", imp)
            if m:
                modules.append(m.group(1))
        elif imp.startswith("import "):
            m = re.match(r"import\s+([\w.]+)", imp)
            if m:
                modules.append(m.group(1))
    return modules


_JS_IMPORT_RE = re.compile(
    r"""(?:import\s+.*?\s+from\s+|require\s*\(\s*)['"]([^'"]+)['"]""",
    re.MULTILINE,
)


def _extract_js_imports(content: str) -> list[str]:
    return [m for m in _JS_IMPORT_RE.findall(content) if m.startswith(".")]


def _resolve_js_path(from_file: str, rel_import: str, all_files: set[str]) -> str | None:
    base = PurePosixPath(from_file).parent
    target = posixpath.normpath(str(base / rel_import))
    for ext in (".ts", ".js", ".tsx", ".jsx", ".vue", "/index.ts", "/index.js"):
        if target + ext in all_files:
            return target + ext
    return target if target in all_files else None


_JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;", re.MULTILINE)


def _extract_java_imports(content: str) -> list[str]:
    return _JAVA_IMPORT_RE.findall(content)


def _java_import_to_path(fqcn: str, all_files: set[str]) -> str | None:
    parts = fqcn.split(".")
    candidate = "/".join(parts) + ".java"
    if candidate in all_files:
        return candidate
    class_name = parts[-1] + ".java"
    matches = [f for f in all_files if f.endswith("/" + class_name) or f == class_name]
    return matches[0] if len(matches) == 1 else None


def build_dep_graph(project_map: dict,
                    file_contents: dict[str, str] | None = None) -> dict:
    files = project_map.get("files", [])
    all_paths = {f["relpath"] for f in files}
    file_contents = file_contents or {}

    graph = {f["relpath"]: {"depends_on": [], "depended_by": []} for f in files}

    for entry in files:
        rel = entry["relpath"]
        ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
        deps: set[str] = set()

        if ext == "py":
            for mod in _extract_py_imports(entry.get("imports", [])):
                for target in _py_module_to_paths(mod, all_paths):
                    if target != rel:
                        deps.add(target)
        elif ext in ("js", "ts", "jsx", "tsx", "vue"):
            content = file_contents.get(rel, "")
            for imp in _extract_js_imports(content):
                target = _resolve_js_path(rel, imp, all_paths)
                if target and target != rel:
                    deps.add(target)
        elif ext == "java":
            content = file_contents.get(rel, "")
            for fqcn in _extract_java_imports(content):
                target = _java_import_to_path(fqcn, all_paths)
                if target and target != rel:
                    deps.add(target)

        graph[rel]["depends_on"] = sorted(deps)

    for rel, info in graph.items():
        for dep in info["depends_on"]:
            if dep in graph:
                graph[dep]["depended_by"].append(rel)
    for info in graph.values():
        info["depended_by"] = sorted(info["depended_by"])

    return graph


def format_dep_context(rel: str, graph: dict, project_map: dict,
                       max_deps: int = 5, max_dependents: int = 5) -> str:
    info = graph.get(rel)
    if not info:
        return ""
    depends_on = info["depends_on"][:max_deps]
    depended_by = info["depended_by"][:max_dependents]
    if not depends_on and not depended_by:
        return ""

    file_symbols: dict[str, list[str]] = {}
    for f in project_map.get("files", []):
        if f["relpath"] in depends_on:
            sigs = [s["signature"] for s in f.get("symbols", [])
                    if s.get("kind") in ("function", "method", "class")][:8]
            file_symbols[f["relpath"]] = sigs

    lines = ["【跨文件依赖】"]
    if depends_on:
        lines.append("本文件依赖：")
        for dep in depends_on:
            sigs = file_symbols.get(dep, [])
            if sigs:
                sig_str = "、".join(s.split("(")[0].replace("def ", "").replace("class ", "")
                                     for s in sigs[:5])
                lines.append(f"  - {dep}（提供：{sig_str}）")
            else:
                lines.append(f"  - {dep}")
    if depended_by:
        lines.append(f"被依赖：{', '.join(depended_by)}")
    lines.append("审查时注意：接口契约是否匹配、参数类型是否一致、异常是否向上传播。")
    return "\n".join(lines)
