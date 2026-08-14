"""跨文件依赖图 —— 零 LLM 的确定性算法构建文件间依赖关系。

设计立场（与 ast_scan.py 同一原则）：
    能用代码确定的，绝不交给概率模型。
    import/require 是语法事实，不是"理解"——正则/AST 做 100% 准确、零 token。

产出：dep_graph dict，键是 relpath，值是依赖信息：
    {
      "utils.py": {
        "depends_on": [{"file": "db.py", "symbols": ["get_connection", "close"]}],
        "depended_by": ["main.py", "api.py"],
      }
    }

下游用途：
    reviewer 的 prompt 注入——让模型看到"这个文件被谁依赖、依赖了谁"，
    从而能发现跨文件接口不一致（参数类型错配、异常未传播等）。
"""

import re
from pathlib import PurePosixPath


# ---------- Python 模块名 → 文件路径映射 ----------

def _py_module_to_paths(module: str, all_files: set[str]) -> list[str]:
    """把 Python 模块名（如 'utils.helpers'）解析为项目内文件路径。

    匹配规则：
      - utils/helpers.py
      - utils/helpers/__init__.py
      - utils.py（顶级模块）
    """
    parts = module.split(".")
    candidates = [
        "/".join(parts) + ".py",
        "/".join(parts) + "/__init__.py",
    ]
    # 单模块也可能是 pkg/__init__.py
    if len(parts) == 1:
        candidates.append(parts[0] + "/__init__.py")
    return [c for c in candidates if c in all_files]


def _extract_py_imports(imports: list[str]) -> list[str]:
    """从 ast_scan 已提取的 imports 列表中取出模块名。

    输入格式：["from utils import parse_config", "import os", "from db import get_conn"]
    输出：["utils", "db"]（只保留项目内可能存在的模块名）
    """
    modules = []
    for imp in imports:
        if imp.startswith("from "):
            # "from X.Y import Z" → 取 X.Y
            m = re.match(r"from\s+([\w.]+)\s+import", imp)
            if m:
                modules.append(m.group(1))
        elif imp.startswith("import "):
            # "import X.Y" → 取 X（顶级包）
            m = re.match(r"import\s+([\w.]+)", imp)
            if m:
                modules.append(m.group(1))
    return modules


# ---------- JS/TS import 提取 ----------

_JS_IMPORT_RE = re.compile(
    r"""(?:import\s+.*?\s+from\s+|require\s*\(\s*)['"]([^'"]+)['"]""",
    re.MULTILINE,
)


def _extract_js_imports(content: str) -> list[str]:
    """提取 JS/TS 文件中的相对路径 import。只保留 './' 或 '../' 开头的。"""
    return [m for m in _JS_IMPORT_RE.findall(content) if m.startswith(".")]


def _resolve_js_path(from_file: str, rel_import: str, all_files: set[str]) -> str | None:
    """解析 JS 相对 import 为项目内文件路径。

    './utils' from 'src/main.ts' → 尝试 'src/utils.ts', 'src/utils.js', 'src/utils/index.ts'
    """
    base = PurePosixPath(from_file).parent
    target = str(base / rel_import)
    # 规范化路径（处理 ../）
    target = str(PurePosixPath(target))

    # 尝试各种扩展名
    for ext in (".ts", ".js", ".tsx", ".jsx", ".vue", "/index.ts", "/index.js"):
        candidate = target + ext
        if candidate in all_files:
            return candidate
    # 可能已经带扩展名
    if target in all_files:
        return target
    return None


# ---------- Java import 提取 ----------

_JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;", re.MULTILINE)


def _extract_java_imports(content: str) -> list[str]:
    """提取 Java 的全限定 import（如 com.project.utils.Helper）。"""
    return _JAVA_IMPORT_RE.findall(content)


def _java_import_to_path(fqcn: str, all_files: set[str]) -> str | None:
    """把 Java 全限定类名映射为项目内文件路径。

    com.project.utils.Helper → 尝试 com/project/utils/Helper.java
    也匹配末尾类名（简化：项目内同名文件）
    """
    parts = fqcn.split(".")
    # 完整路径
    candidate = "/".join(parts) + ".java"
    if candidate in all_files:
        return candidate
    # 只匹配类名（最后一段）
    class_name = parts[-1] + ".java"
    matches = [f for f in all_files if f.endswith("/" + class_name) or f == class_name]
    return matches[0] if len(matches) == 1 else None


# ---------- 主入口 ----------

def build_dep_graph(project_map: dict, file_contents: dict[str, str] | None = None) -> dict:
    """从 project_map 构建跨文件依赖图。

    Args:
        project_map: scan_project() 的产出（含 files 列表，每个有 relpath/imports）
        file_contents: 可选，{relpath: content} 字典。JS/Java 需要读内容提取 import；
                       Python 直接用 ast_scan 已提取的 imports 字段即可。

    Returns:
        dep_graph: {relpath: {"depends_on": [...], "depended_by": [...]}}
    """
    files = project_map.get("files", [])
    all_paths: set[str] = {f["relpath"] for f in files}
    file_contents = file_contents or {}

    # 初始化
    graph: dict[str, dict] = {
        f["relpath"]: {"depends_on": [], "depended_by": []}
        for f in files
    }

    # 逐文件提取依赖
    for entry in files:
        rel = entry["relpath"]
        ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
        deps: set[str] = set()

        if ext == "py":
            # Python：用 ast_scan 已提取的 imports
            modules = _extract_py_imports(entry.get("imports", []))
            for mod in modules:
                for target in _py_module_to_paths(mod, all_paths):
                    if target != rel:
                        deps.add(target)

        elif ext in ("js", "ts", "jsx", "tsx", "vue"):
            # JS/TS：需要文件内容
            content = file_contents.get(rel, "")
            if content:
                for imp in _extract_js_imports(content):
                    target = _resolve_js_path(rel, imp, all_paths)
                    if target and target != rel:
                        deps.add(target)

        elif ext == "java":
            # Java：需要文件内容
            content = file_contents.get(rel, "")
            if content:
                for fqcn in _extract_java_imports(content):
                    target = _java_import_to_path(fqcn, all_paths)
                    if target and target != rel:
                        deps.add(target)

        # 兜底：同目录文件视为弱关联（只在小目录内生效，避免 node_modules 式爆炸）
        # 暂不启用——等实测后决定是否需要

        graph[rel]["depends_on"] = sorted(deps)

    # 反向索引：depended_by
    for rel, info in graph.items():
        for dep in info["depends_on"]:
            if dep in graph:
                graph[dep]["depended_by"].append(rel)

    # 排序 depended_by
    for info in graph.values():
        info["depended_by"] = sorted(info["depended_by"])

    return graph


def format_dep_context(rel: str, graph: dict, project_map: dict,
                       max_deps: int = 5, max_dependents: int = 5) -> str:
    """为指定文件生成注入 prompt 的依赖上下文字符串。

    Args:
        rel: 当前文件的 relpath
        graph: build_dep_graph() 的产出
        project_map: 用于查找依赖文件的符号签名
        max_deps: 最多展示几个依赖文件
        max_dependents: 最多展示几个被依赖文件

    Returns:
        格式化的依赖上下文字符串（空串 = 无依赖信息）
    """
    info = graph.get(rel)
    if not info:
        return ""

    depends_on = info["depends_on"][:max_deps]
    depended_by = info["depended_by"][:max_dependents]

    if not depends_on and not depended_by:
        return ""

    # 构建文件 → 符号签名的快速索引
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
