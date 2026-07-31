"""splice.py —— AST 外科缝合：把模型给的"零件"装回"原件"。

Python 专属模块，服务 fixer 的 api 后端的缝合模式（大文件）。
"""

import ast
from dataclasses import dataclass

IMPORTS = "<imports>"
GUARD = "<import_guard>"


def _is_import_guard(node: ast.Try) -> bool:
    """判断一个 Try 节点是不是"可选依赖守卫"。"""
    def _only_imports(stmts) -> bool:
        return all(isinstance(s, (ast.Import, ast.ImportFrom)) for s in stmts)

    def _only_safe_handler(stmts) -> bool:
        return all(isinstance(s, (ast.Assign, ast.Import, ast.ImportFrom, ast.Pass))
                   for s in stmts)

    return (_only_imports(node.body)
            and all(_only_safe_handler(h.body) for h in node.handlers)
            and (not node.orelse or _only_imports(node.orelse))
            and (not node.finalbody or _only_imports(node.finalbody)))


@dataclass
class Part:
    """一个零件：一个顶层函数/类的完整源码，或一组 import 行。"""
    name: str
    source: str


def parse_parts(code: str) -> list[Part]:
    """把一个代码块解析成零件列表。"""
    tree = ast.parse(code)
    lines = code.splitlines()
    parts: list[Part] = []
    imports: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts.append(Part(node.name,
                              "\n".join(lines[node.lineno - 1:node.end_lineno])))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append("\n".join(lines[node.lineno - 1:node.end_lineno]))
        elif isinstance(node, ast.Try) and _is_import_guard(node):
            parts.append(Part(GUARD,
                              "\n".join(lines[node.lineno - 1:node.end_lineno])))
        else:
            raise ValueError(
                f"零件里有不支持的内容：{type(node).__name__}"
                f"（只允许顶层 def / class / import）")
    if imports:
        parts.insert(0, Part(IMPORTS, "\n".join(imports)))
    return parts


def splice(original: str, parts: list[Part]) -> str:
    """把零件缝合进原文件，返回新文件全文。"""
    src_lines = original.splitlines()
    tree = ast.parse(original)

    replacements: list[tuple[int, int, list[str]]] = []
    new_imports: list[str] = []
    guards: list[str] = []
    for part in parts:
        if part.name == IMPORTS:
            new_imports += [l for l in part.source.splitlines() if l.strip()]
            continue
        if part.name == GUARD:
            guards.append(part.source)
            continue
        match = None
        for node in tree.body:
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and node.name == part.name):
                match = node
                break
        if match is None:
            raise KeyError(f"原文件里找不到 {part.name!r}："
                           f"模型可能改了名字或发明了新函数")
        replacements.append((match.lineno, match.end_lineno,
                             part.source.splitlines()))

    for lineno, end_lineno, new_lines in sorted(replacements, reverse=True):
        src_lines[lineno - 1:end_lineno] = new_lines

    if new_imports or guards:
        existing_lines = {l.strip() for l in src_lines}
        existing_text = "\n".join(src_lines)
        todo_lines = [l for l in new_imports if l.strip() not in existing_lines]
        todo_guards = [g for g in guards if g.strip() not in existing_text]
        block: list[str] = []
        block += todo_lines
        for g in todo_guards:
            block += g.splitlines()
        if block:
            tree2 = ast.parse("\n".join(src_lines))
            insert_at = 0
            body = tree2.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                insert_at = body[0].end_lineno
            for node in body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    insert_at = max(insert_at, node.end_lineno)
            src_lines[insert_at:insert_at] = block

    return "\n".join(src_lines) + "\n"
