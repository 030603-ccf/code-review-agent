"""语言插件层 —— 非 Python 文件的"切块级"启发式符号提取。

设计立场（先读这段再改代码）：
    这不是语法分析器（parser），只是"正则找符号起点 + 数括号找终点"的启发式。
    切块器（chunking.py）只需要"大概知道每个函数从哪行到哪行"，
    所以本模块的铁律是：
        **宁可少提符号（退化为窗口切块），不可错提符号（腰斩函数）。**
    漏提 = 审查粒度变粗，但模型看到的代码仍然完整；
    错提 = 函数被从中间切开，审查质量直接崩。两害相权取其轻。

为什么不用 tree-sitter 之类的真解析器：
    这是教学项目，核心原则是"核心全部手写、零重型依赖"。
    正则 + 数括号足以覆盖日常代码风格，且新增一种语言 = 加几行模式。
    （生产系统请换 tree-sitter——那是另一个量级的正确性。）

与 ast_scan.py 的分工：
    .py 走标准库 ast（精确，零误判）；其他注册扩展走本模块（启发式）。
    两边产出的符号 dict 形状完全一致（kind/name/line_start/line_end/
    signature/docstring），下游 chunking / reviewer 无需区分来源。
"""

import re
from dataclasses import dataclass

# 控制关键字：它们后面也可以跟 "(...) {"，但绝不是符号定义。
# 两处用法：① 行首词命中则整行跳过；② 模式捕获的名字命中则丢弃（双保险）。
CONTROL_KEYWORDS = {
    "if", "else", "for", "while", "do", "switch", "case", "default",
    "catch", "try", "finally", "return", "throw", "throws", "new",
    "delete", "typeof", "sizeof", "using", "lock", "foreach", "with",
    "yield", "await", "synchronized", "goto", "in", "of",
}

# 容器型关键字统一归一为 "class"：切块只关心"容器"和"单元"两种形状，
# interface/namespace/trait 更细的分类只有真 AST 才给得起（启发式不硬撑）。
_CONTAINER_KINDS = {
    "class", "interface", "enum", "struct", "record", "trait",
    "namespace", "module", "mod", "impl",
}


def _normalize_kind(raw: str | None) -> str:
    """把各语言的声明关键字归一到 chunking 认识的两种形状。"""
    return "class" if raw in _CONTAINER_KINDS else "function"


@dataclass(frozen=True)
class LanguageSpec:
    """一种语言的提取规格。frozen=True：注册表是全局共享的，不许运行时改。

    symbol_patterns 的约定：
        - 必须带命名组 (?P<name>...) 捕获符号名；
        - 可选命名组 (?P<kind>...) 捕获声明关键字（class/namespace 等），
          用于区分"容器"与"函数"；没有该组的一律视为 function。
    """

    name: str                                   # 语言名（日志/调试用）
    extensions: tuple[str, ...]                 # 负责的扩展名（小写、带点）
    symbol_patterns: tuple[re.Pattern, ...]     # 已编译的符号起点正则
    brace_based: bool = True                    # 是否用 {} 界定符号结束


# ---------------- JS / TS ----------------
# .jsx/.tsx 与 .js/.ts 共用一套模式：JSX 语法不影响"函数/类长什么样"。
_JS_IDENT = r"[A-Za-z_$][\w$]*"   # JS 标识符允许 $ 开头（jQuery 时代遗产）
_JS_PATTERNS = (
    # 类型声明：class Foo / interface Bar（TS）/ enum Baz（TS）
    re.compile(rf"\b(?P<kind>class|interface|enum)\s+(?P<name>{_JS_IDENT})"),
    # 函数声明：function foo( / async function foo( / function* gen(
    re.compile(rf"\bfunction\s*\*?\s*(?P<name>{_JS_IDENT})\s*\("),
    # 箭头函数赋值：const foo = (x) => / let foo = async (x) => / var f = x =>
    re.compile(rf"\b(?:const|let|var)\s+(?P<name>{_JS_IDENT})"
               rf"\s*=\s*(?:async\s*)?(?:\([^)]*\)|{_JS_IDENT})\s*=>"),
    # 对象/类方法（行首锚定，防误吞函数调用）：foo(x) { / async load() {
    # 结尾允许 "}" 前只有可选的 "{"——要求整行"长得像定义"，
    # `console.log(x);` 这种调用因为 ) 后面还有东西而自然落空。
    re.compile(rf"^\s*(?:(?:async|static|get|set|public|private|protected"
               rf"|readonly|abstract|override)\s+)*"
               rf"(?P<name>{_JS_IDENT})\s*\([^)]*\)\s*\{{?\s*$"),
)

# ---------------- Java ----------------
_JAVA_MODS = r"(?:public|private|protected|static|final|abstract|synchronized|native|default)"
_JAVA_TYPE = r"[\w<>\[\],.?]+"   # 返回类型：String / List<String> / int[] 都罩得住
_JAVA_PATTERNS = (
    re.compile(r"\b(?P<kind>class|interface|enum|record)\s+(?P<name>\w+)"),
    # 方法：修饰符* 返回类型 名字(参数) throws X {   （{ 允许在下一行）
    re.compile(rf"^\s*(?:{_JAVA_MODS}\s+)*(?:{_JAVA_TYPE}\s+)+"
               rf"(?P<name>\w+)\s*\([^)]*\)"
               rf"\s*(?:throws\s+[\w\s,]+)?\{{?\s*$"),
    # 构造器没有返回类型：public Sample() { ——名字大写开头是它的身份证
    re.compile(rf"^\s*(?:{_JAVA_MODS}\s+)*(?P<name>[A-Z]\w*)\s*\([^)]*\)\s*\{{?\s*$"),
)

# ---------------- C# ----------------
# 思路与 Java 相同，修饰符更多（async/internal/override...），外加 namespace。
_CS_MODS = (r"(?:public|private|protected|internal|static|readonly|virtual"
            r"|override|abstract|async|sealed|extern|partial|new)")
_CS_PATTERNS = (
    re.compile(r"\b(?P<kind>namespace|class|interface|enum|struct|record)"
               r"\s+(?P<name>\w+)"),
    re.compile(rf"^\s*(?:{_CS_MODS}\s+)*(?:[\w<>\[\],.?]+\s+)+"
               rf"(?P<name>\w+)\s*\([^)]*\)\s*\{{?\s*$"),
    re.compile(rf"^\s*(?:{_CS_MODS}\s+)*(?P<name>[A-Z]\w*)\s*\([^)]*\)\s*\{{?\s*$"),
)

# ---------------- 顺手注册的便宜语言 ----------------
# 以下语言同理可得。它们不是本项目的审查重点，但注册表加一行就能扩展，
# 顺便演示"插件层"的扩展成本有多低。
_GO_PATTERNS = (
    # func foo( 或带接收者的方法 func (r *Reader) Read(
    re.compile(r"\bfunc\s+(?:\([^)]*\)\s*)?(?P<name>\w+)\s*\("),
    re.compile(r"\btype\s+(?P<name>\w+)\s+(?P<kind>struct|interface)\b"),
)
_RS_PATTERNS = (
    re.compile(r"\bfn\s+(?P<name>\w+)"),
    re.compile(r"\b(?P<kind>struct|enum|trait|impl|mod)\s+(?P<name>\w+)"),
)
_CPP_PATTERNS = (
    re.compile(r"\b(?P<kind>class|struct|namespace|enum)\s+(?P<name>\w+)"),
    # 自由函数：返回类型 名字(参数) { ——参数里不许有分号，
    # 这一句挡掉了 `void foo();` 这种"只有声明没有定义"的行。
    re.compile(r"^\s*(?:[\w:<>\*&~]+\s+)+(?P<name>~?\w+)\s*\([^;]*\)"
               r"\s*(?:const\s*)?\{?\s*$"),
)
_PHP_PATTERNS = (
    re.compile(r"\bfunction\s+(?P<name>\w+)"),
    re.compile(r"\b(?P<kind>class|interface|trait|enum)\s+(?P<name>\w+)"),
)
_RUBY_PATTERNS = (
    re.compile(r"^\s*def\s+(?P<name>[\w!?=]+)"),
    re.compile(r"^\s*(?P<kind>class|module)\s+(?P<name>[\w:]+)"),
)

# 规格清单。新增语言 = 这里加一项（模式 + 扩展名），注册表自动收录。
_SPECS: tuple[LanguageSpec, ...] = (
    LanguageSpec("JavaScript/TypeScript",
                 (".js", ".jsx", ".ts", ".tsx"), _JS_PATTERNS),
    LanguageSpec("Vue", (".vue",), _JS_PATTERNS),   # SFC 的 <script> 块按 JS 提取
    LanguageSpec("Java", (".java",), _JAVA_PATTERNS),
    LanguageSpec("C#", (".cs",), _CS_PATTERNS),
    LanguageSpec("Go", (".go",), _GO_PATTERNS),
    LanguageSpec("Rust", (".rs",), _RS_PATTERNS),
    LanguageSpec("C/C++", (".cpp", ".cc", ".h", ".hpp"), _CPP_PATTERNS),
    LanguageSpec("PHP", (".php",), _PHP_PATTERNS),
    # Ruby 用 def...end 而不是大括号界定，brace_based=False 走另一条终点策略
    LanguageSpec("Ruby", (".rb",), _RUBY_PATTERNS, brace_based=False),
)

# 扩展名 -> 规格 的注册表：ast_scan 按它决定"哪些文件值得索引"。
LANG_BY_EXT: dict[str, LanguageSpec] = {
    ext: spec for spec in _SPECS for ext in spec.extensions
}


def _ext_of(relpath: str) -> str:
    """从相对路径取小写扩展名（带点）；没有扩展名返回空串。"""
    return "." + relpath.rsplit(".", 1)[-1].lower() if "." in relpath else ""


def _vue_script_region(lines: list[str]) -> tuple[int, int] | None:
    """定位 Vue 单文件组件的 <script> 块行区间（含首尾标签行）。

    <template> / <style> 区域不提取符号——那里没有函数定义，
    硬扫只会把 `{{ message }}` 之类的大括号当代码数，纯属添乱。
    找不到 <script> 返回 None（纯模板组件）。
    """
    start = None
    for i, line in enumerate(lines, 1):
        if start is None:
            if re.match(r"\s*<script\b", line):   # 允许带属性：<script setup lang="ts">
                start = i
        elif re.match(r"\s*</script\s*>", line):
            return (start, i)
    # 防御：标签没闭合就当它延伸到文件尾（启发式的惯用兜底）
    return (start, len(lines)) if start is not None else None


def _brace_end(lines: list[str], start: int, hi: int) -> int:
    """从符号声明行起数大括号，深度归零的那一行就是符号结束行。

    朴素实现，已知局限（注释里必须诚实）：
        字符串/注释/字符字面量里的 {} 会干扰计数（如 "}" 出现在正则里）。
        对"切块"这个用途够了——我们不是编译器，错一两行不会腰斩函数，
        只会让一个块稍微大一点或小一点。

    "起始行没有 {" 的两种命运（必须先区分，再决定数不数）：
        - Allman 风格：`public void foo()` 换行后 `{` 独占一行
          → 下一个非空行以 { 开头，正常数括号；
        - 单行符号：`const f = (x) => x * 2;`（单行箭头函数）
          → 下一个非空行是别的代码，结束行 = 起始行。
        不做这个区分的话，单行符号会把下一个符号的 {} 当成自己的，
        一路数到人家的结束行——错提，比漏提更糟。
    兜底：括号到区域末行都没归零（代码写了一半）→ 结束 = 区域末行。
    """
    # ---------- 第 1 步：找到"属于本符号"的第一个 { ----------
    open_at = None
    if "{" in lines[start - 1]:
        open_at = start                      # 常规风格：{ 就在声明行
    else:
        probe = start + 1
        while probe <= hi and not lines[probe - 1].strip():
            probe += 1                       # 跳过声明与 { 之间的空行
        if probe <= hi and lines[probe - 1].strip().startswith("{"):
            open_at = probe                  # Allman 风格：{ 独占一行
    if open_at is None:
        return start                         # 单行符号：结束 = 起始

    # ---------- 第 2 步：从 { 所在行开始数深度 ----------
    depth = 0
    for lineno in range(open_at, hi + 1):
        for ch in lines[lineno - 1]:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return lineno
    return hi                                # 括号没收尾：兜底到区域末行


def extract_symbols(relpath: str, lines: list[str]) -> list[dict]:
    """从非 Python 源文件中启发式提取符号表。

    返回与 ast_scan._extract **完全同构**的 dict 列表
    （chunking 只认这个形状：name/line_start/line_end 排序切片，
    reviewer 用 signature 做上下文注入）。
    未注册的扩展名、提不到符号的文件一律返回 []——
    调用方（ast_scan）照样索引该文件，由 chunking 的窗口兜底接手。
    """
    spec = LANG_BY_EXT.get(_ext_of(relpath))
    if spec is None:
        return []

    # 提取范围默认是整个文件；Vue SFC 只扫 <script> 块
    lo, hi = 1, len(lines)
    if spec.name == "Vue":
        region = _vue_script_region(lines)
        if region is None:
            return []
        lo, hi = region

    symbols: list[dict] = []
    for lineno in range(lo, hi + 1):
        line = lines[lineno - 1]
        stripped = line.strip()
        # 空行、各语言的注释行、预处理行（#include）、Vue 标签行一律跳过
        if not stripped or stripped.startswith(("//", "/*", "*", "#", "<")):
            continue
        # 第一道闸：行首词是控制关键字，整行不可能是符号定义
        # （if (x) { / for (...) { / return foo(x) 都死在这里）
        first = re.match(r"\w+", stripped)
        if first and first.group(0) in CONTROL_KEYWORDS:
            continue
        for pat in spec.symbol_patterns:
            m = pat.search(line)
            if not m:
                continue
            name = m.group("name")
            # 第二道闸：模式本身也可能把关键字吞成"名字"（双保险）
            if name in CONTROL_KEYWORDS:
                break
            symbols.append({
                "kind": _normalize_kind(m.groupdict().get("kind")),
                "name": name,
                "line_start": lineno,
                # 结束行先占位为起始行，循环结束后统一计算
                "line_end": lineno,
                # signature 就是声明行本身（[:80] 与 ast_scan 的截断约定一致）
                "signature": stripped[:80],
                "docstring": "",   # 启发式不解析文档注释，占位保持结构一致
            })
            break   # 一行至多一个符号：第一个命中的模式说了算

    # ---------- 统一计算结束行 ----------
    if spec.brace_based:
        for s in symbols:
            s["line_end"] = _brace_end(lines, s["line_start"], hi)
    else:
        # 非大括号语言（Ruby 的 def...end）：不数括号，
        # "下一个符号的起点 - 1"就是上一个符号的终点，末尾符号到区域结束。
        # 朴素但对顶层 def/class 平铺的代码足够准。
        for i, s in enumerate(symbols):
            nxt = symbols[i + 1] if i + 1 < len(symbols) else None
            s["line_end"] = (nxt["line_start"] - 1) if nxt else hi

    return symbols
