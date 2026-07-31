"""splice.py —— AST 外科缝合：把模型给的"零件"装回"原件"。

⚠️ 语言适用范围：本模块是 **Python 专属**（依赖标准库 ast 解析零件与原件）。
    它只服务 fixer 的 **api 后端**的"缝合模式"（模型输出零件，代码负责装配）。
    默认的 **opencode 后端**是专业编程 agent，自己读文件、改代码、跑命令，
    不经过本模块——因此多语言项目（js/vue/java/cs...）走 opencode 后端
    不受此限制，缝合模块无需也不需要泛化。

整文件重写为什么不可靠（真实运行量出来的）：
让推理模型输出 812 行的完整文件，它写到约 100 行就"擅自收工"
（finish_reason=stop，根本没撞到 token 上限）——输出越长越不可靠。
但让它只输出三五个函数，毫无压力。

对策是分工，和 Aggregator 同一个原则——能确定做的事，绝不交给概率模型：
    模型负责"理解"：哪些函数要改、改成什么样（它只输出这些"零件"）
    AST 负责"装配"：按名字找到原函数，整段替换（精确到行，零幻觉）

零件约定（写在外科模式的 system prompt 里）：
    - 每个代码块只放需要修改的顶层 def / class 的完整新版本
    - 需要新 import 时，单独一个块只写 import 行
    - 不许输出模块级调用（print、赋值等），那没法按名字缝合
"""

import ast
from dataclasses import dataclass

# 表示"这是个 import 零件"的特殊名字：import 没有名字，给它一个
IMPORTS = "<imports>"
# 可选依赖守卫零件（try: import x except ImportError: x = None）——
# 真实模型回复里见过的惯用法，必须整块原样保留，不能按行拆
GUARD = "<import_guard>"


def _is_import_guard(node: ast.Try) -> bool:
    """判断一个 Try 节点是不是"可选依赖守卫"。

    允许的形体（超出就拒收，防止任意 try 块被搬到文件顶部）：
        try 体：      只有 import
        except 体：   只有 赋值 / import / pass（典型：OpenAI = None）
        else/finally：只有 import
    """
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

    name: str       # 顶层 def/class 的名字；import 零件为 IMPORTS
    source: str     # 完整源码


def parse_parts(code: str) -> list[Part]:
    """把一个代码块解析成零件列表。

    ast.parse 先保证这段代码本身合法（模型写个半个函数在这里就炸）；
    node.lineno / node.end_lineno 是 AST 节点的真实行号区间，
    按行切片取出来就是"这个定义的完整源码"——不用自己数括号数缩进。
    """
    tree = ast.parse(code)          # 语法不合法直接抛 SyntaxError
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
            # 守卫块整块保留为一个独立零件（不能并进普通 import 里按行拆）
            parts.append(Part(GUARD,
                              "\n".join(lines[node.lineno - 1:node.end_lineno])))
        else:
            # 模块级调用没法按名字缝合，硬塞进来会破坏文件——拒收
            raise ValueError(
                f"零件里有不支持的内容：{type(node).__name__}"
                f"（只允许顶层 def / class / import）")
    if imports:
        parts.insert(0, Part(IMPORTS, "\n".join(imports)))
    return parts


def splice(original: str, parts: list[Part]) -> str:
    """把零件缝合进原文件，返回新文件全文。

    缝合规则：
        同名 def/class  -> 整段替换（含装饰器区间，AST 行号覆盖它们）
        import 零件     -> 去重后插到"文档字符串之后、最后一个 import 之后"

    为什么按行号降序应用替换：替换会改变行数（新函数可能更长），
    从文件尾部往前改，前面未处理的行号纹丝不动——
    这是"多处编辑不串行"的经典技巧，和文本编辑器多光标同理。
    """
    src_lines = original.splitlines()
    tree = ast.parse(original)

    # ---------- 第 1 步：收集替换区间、新 import、import 守卫块 ----------
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
            # 模型改了个不存在的名字（幻觉/擅自改名）——宁可失败也不硬塞
            raise KeyError(f"原文件里找不到 {part.name!r}："
                           f"模型可能改了名字或发明了新函数")
        replacements.append((match.lineno, match.end_lineno,
                             part.source.splitlines()))

    # ---------- 第 2 步：降序应用替换（行号以原文件为准，始终有效）----------
    for lineno, end_lineno, new_lines in sorted(replacements, reverse=True):
        src_lines[lineno - 1:end_lineno] = new_lines

    # ---------- 第 3 步：合并新 import 与守卫块 ----------
    # 注意要在替换之后重新解析：替换可能改变了行数，旧行号已失效
    if new_imports or guards:
        existing_lines = {l.strip() for l in src_lines}
        existing_text = "\n".join(src_lines)
        todo_lines = [l for l in new_imports if l.strip() not in existing_lines]
        # 守卫按"整块文本"去重：文件里已有同款守卫就不重复插
        todo_guards = [g for g in guards if g.strip() not in existing_text]
        block: list[str] = []
        block += todo_lines                      # 普通 import 在前
        for g in todo_guards:                    # 守卫块整块殿后
            block += g.splitlines()
        if block:
            tree2 = ast.parse("\n".join(src_lines))
            insert_at = 0
            body = tree2.body
            # 模块文档字符串之后（保持它"第一个语句"的地位）
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                insert_at = body[0].end_lineno
            # 有现成 import 就跟在最后一个后面（更符合工程习惯）
            for node in body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    insert_at = max(insert_at, node.end_lineno)
            src_lines[insert_at:insert_at] = block

    return "\n".join(src_lines) + "\n"
