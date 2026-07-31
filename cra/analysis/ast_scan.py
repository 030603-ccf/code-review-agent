"""项目扫描器 —— 零 LLM 的项目结构索引构建。

为什么用代码扫描而不是让模型去读项目结构：
- 确定性：行号、函数名永远准确，不会幻觉
- 零成本：不花一个 token，速度毫秒级
- 它是整个"记忆系统"的地基：之后所有 agent 的上下文切片都靠这份索引定位

多语言分工（同一份索引，两种提取器）：
- .py 走标准库 ast：把源代码解析成语法树——"编译器视角下的代码结构"，精确零误判
- 其他注册扩展（js/ts/vue/java/cs/go/rs/cpp/php/rb）走 languages.py 的
  启发式提取：正则找符号起点 + 数括号找终点，宁可少提不可错提
- 提不到符号的文件照样索引（symbols=[]），由 chunking 的窗口兜底接手
"""

import ast
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from cra.analysis.languages import LANG_BY_EXT, extract_symbols

if TYPE_CHECKING:
    from cra.analysis.symbol_backend import SymbolBackend

# 扫描时忽略的目录：这些不是项目代码，扫进去只会污染索引，用set而不用list是为了 O(1) 查找
IGNORE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
    "node_modules", "runs", ".idea", ".vscode", "dist", "build",
}


def _sha1(path: Path) -> str:
    """文件内容的哈希指纹。修改前后对比 hash，就知道文件有没有变化
    ——这是 Phase 3"增量更新索引"的钥匙。"""
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _signature_of(node) -> str:
    """提取函数/类的签名（只要第一行，不要函数体）。
    ast.unparse 是 ast 节点的"反向序列化"：把语法树节点转回源码字符串。
    ast.FunctionDef, ast.AsyncFunctionDef：这里同时匹配普通函数 def 和异步函数 async def"""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        # node.returns 是返回值注解（-> str），有就带上，索引信息更完整
        # unparse：语法树转换回源码字符串，方便索引里直接展示签名
        ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        return f"def {node.name}({ast.unparse(node.args)}){ret}"
    # 匹配类定义：class MyClass(Base1, Base2)
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        return f"class {node.name}({bases})" if bases else f"class {node.name}"
    return ""


def _extract(tree: ast.AST) -> tuple[list[dict], list[str]]:
    """从语法树中提取符号表和导入表。

    只取顶层定义和类的方法（够索引用），不深入嵌套函数。
    node.lineno / node.end_lineno 就是符号在文件里的行号范围。
    """
    symbols: list[dict] = []
    imports: list[str] = []

    for node in tree.body:  # type: ignore[attr-defined]
        # 匹配同步函数和异步函数
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append({
                "kind": "function",
                "name": node.name,
                "line_start": node.lineno,
                "line_end": node.end_lineno,
                "signature": _signature_of(node),
                "docstring": ast.get_docstring(node) or "",
            })
        # 匹配类
        elif isinstance(node, ast.ClassDef):
            symbols.append({
                "kind": "class",
                "name": node.name,
                "line_start": node.lineno,
                "line_end": node.end_lineno,
                "signature": _signature_of(node),
                "docstring": ast.get_docstring(node) or "",
            })
            # 类的方法也登记，名字用"类名.方法名"限定
            for sub in node.body:
                # 匹配类里面的方法
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append({
                        "kind": "method",
                        "name": f"{node.name}.{sub.name}",
                        "line_start": sub.lineno,
                        "line_end": sub.end_lineno,
                        "signature": _signature_of(sub),
                        "docstring": ast.get_docstring(sub) or "",
                    })
        # ================= 模块级变量登记（A/B 对比实验催生的修复）=================
        # 背景：切块审查时，模型只能看到"函数/类"的签名，
        # 看不到模块顶部定义的变量（如 CONSERVATIVE_REPLIES = [...]）。
        # DeepSeek 看到某行用了这个变量却找不到定义，就自信地误报 NameError。
        # 修复：把模块级赋值语句也登记为符号，签名注入时模型就知道它存在。
        elif isinstance(node, ast.Assign):
            # ast.Assign = 普通赋值节点，对应 "X = ..." 这种语句。
            # node.targets 是赋值号左边的"目标列表"——因为 Python 支持多目标赋值：
            #   a = b = 1   → targets 里有两个元素 (Name('a'), Name('b'))
            # node.targets 里可能是 Name（普通变量）、Tuple（解包）、Attribute（obj.x）等，
            # ast.unparse 把节点转回源码字符串，统一处理这些情况。
            targets = ", ".join(ast.unparse(t) for t in node.targets)
            symbols.append({
                "kind": "variable",
                "name": targets,
                "line_start": node.lineno,
                "line_end": node.end_lineno,
                # signature 直接存整条赋值语句（如 "QUERY_PREFIX = '...'"），
                # 签名注入时模型一眼看到"这个变量定义过、初值是什么"。
                # [:80] 防止超长赋值（比如几百行的列表字面量）撑爆上下文。
                "signature": ast.unparse(node)[:80],
                "docstring": "",   # 变量没有 docstring，占位保持结构一致
            })
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            # ast.AnnAssign = 带类型注解的赋值，对应 "x: int = 5"。
            # 它的左边只有单个 target（不支持多目标），所以不用 join。
            # node.value is not None 排除 "x: int" 这种只声明不赋值的情况——
            # 那只是类型声明，没有实际定义，登记了反而会误导模型。
            symbols.append({
                "kind": "variable",
                "name": ast.unparse(node.target),
                "line_start": node.lineno,
                "line_end": node.end_lineno,
                "signature": ast.unparse(node)[:80],
                "docstring": "",
            })
        elif isinstance(node, ast.Import):
            imports.extend(f"import {a.name}" for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = ", ".join(a.name for a in node.names)
            imports.append(f"from {module} import {names}")

    return symbols, imports


def scan_file(path: Path, relpath: str, backend: "SymbolBackend | None" = None) -> dict:
    """扫描单个文件，产出索引条目。语法错误的文件不炸，标记后继续。

    Args:
        path: 文件绝对路径。
        relpath: 相对路径。
        backend: 符号提取后端实例；为 None 时走默认启发式提取。
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    entry = {
        "relpath": relpath,
        "sha1": _sha1(path),
        "size_bytes": path.stat().st_size,
        "line_count": len(text.splitlines()),
        "symbols": [],
        "imports": [],
        "parse_error": None,
    }
    if path.suffix.lower() == ".py":
        # Python：标准库 ast 精确解析，符号表 + 导入表一次拿全
        try:
            tree = ast.parse(text) # 把源代码解析成语法树，如果有语法错误，会在这里报错SyntaxError
            entry["symbols"], entry["imports"] = _extract(tree)
        except SyntaxError as e:
            # 文件本身有语法错误：如实记录（这本身就是一个“审查发现”）。
            # parse_error 只对 Python 生效——其他语言根本没有“解析”这一步，
            # 启发式提取遇到烂代码顶多提不出符号，不存在“语法错误”的概念。
            entry["parse_error"] = f"{e.msg} (line {e.lineno})"
    else:
        # 非 Python：如果提供了符号提取后端，优先用它；否则走启发式
        # （languages.py）。提不到符号的文件照样索引（symbols=[]）——
        # chunking 有窗口兆底，静默丢弃会让这些文件永远逃离审查。
        # imports 留空：正则抠 import 收益低误报高，签名注入没有它也够用。
        if backend is not None:
            entry["symbols"] = backend.extract(relpath, text)
        else:
            entry["symbols"] = extract_symbols(relpath, text.splitlines())
    return entry


def scan_project(root: str | Path, backend: "SymbolBackend | None" = None) -> dict:
    """扫描整个项目，产出 project_map 索引（可直接序列化为 JSON）。

    Args:
        root: 项目根目录。
        backend: 符号提取后端实例；透传给 scan_file()。
    """
    root = Path(root).resolve()
    files = []
    # 发现范围 = .py + 语言注册表里的全部扩展名。
    # rglob("*") 一次遍历按后缀过滤，比每个扩展名各扫一遍目录树便宜。
    exts = {".py"} | set(LANG_BY_EXT)
    candidates = (p for p in root.rglob("*")
                  if p.is_file() and p.suffix.lower() in exts)
    for p in sorted(candidates):   # sorted 保证结果稳定（可复现）
        # p.parts 是路径的每一节；任何一节在忽略名单里就跳过
        if any(part in IGNORE_DIRS for part in p.parts):
            continue
        rel = p.relative_to(root).as_posix()   # relative : 计算相对目录 as_posix : \转换为/
        files.append(scan_file(p, rel, backend=backend))

    return {
        "root": str(root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(files),
        "files": files,
    }
