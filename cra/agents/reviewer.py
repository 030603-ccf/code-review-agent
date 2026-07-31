"""ReviewerAgent —— 代码审查员（Phase 2：按块审查 + 签名上下文注入）。

agent 的最小公式（每个 agent 都是它的变体）：
    agent = system prompt（人设与规则）+ user 消息（材料）+ 输出 schema（契约）

Phase 2 的两处升级：
  1. 输入从"整文件"变成"符号切块"——不再腰斩函数
  2. 给模型补"块外符号签名"——修复上下文丢失导致的误判
"""

from pathlib import Path

from cra.llm.prompts import load_prompt, profile_of, PROMPTS_DIR
from cra.llm.structured import chat_structured, StructuredOutputError
from cra.schemas.finding import Finding, FindingList

# ---------- 语言补丁加载 ----------
_SUPPLEMENTS_DIR = PROMPTS_DIR / "supplements"

# 扩展名 → 补丁文件名映射
_EXT_SUPPLEMENT_MAP: dict[str, str] = {
    "java": "java.md",
    "py": "python.md",
    "js": "javascript.md",
    "ts": "javascript.md",
    "jsx": "javascript.md",
    "tsx": "javascript.md",
    "vue": "javascript.md",
}

# 缓存已加载的补丁内容，避免每次审查都读磁盘
_supplement_cache: dict[str, str] = {}


def _load_supplement(filename: str) -> str:
    """加载补丁文件内容（带缓存）。"""
    if filename not in _supplement_cache:
        path = _SUPPLEMENTS_DIR / filename
        _supplement_cache[filename] = path.read_text(encoding="utf-8") if path.is_file() else ""
    return _supplement_cache[filename]


# JSON 输出格式——作为 prompt 的最后一块拼接，确保模型聚焦
_FMT = (
    "【输出格式】你的完整回复必须是且仅是一个合法 JSON 对象"
    "（以 { 开头、以 } 结尾），不要输出任何解释、注释或 markdown 标记：\n"
    '{"findings": [{"id": "F1",'
    '"category": "security|performance|readability|best_practice",'
    '"severity": "critical|high|medium|low",'
    '"file_path": "用户给的文件路径",'
    '"line_start": 起始行号, "line_end": 结束行号,'
    '"title": "一句话标题",'
    '"description": "问题是什么、为什么有害",'
    '"evidence": "从文件照抄的代码",'
    '"suggestion": "怎么改",'
    '"confidence": 0.0到1.0}]}'
)


def _build_system_prompt(client, file_ext: str) -> str:
    """拼装 system prompt = 基础 prompt + 语言补丁 + JSON 格式（末尾）。"""
    base = load_prompt("reviewer", profile_of(client))
    parts = [base]

    # 语言专属补丁：按扩展名匹配
    lang_supplement = _EXT_SUPPLEMENT_MAP.get(file_ext, "")
    if lang_supplement:
        content = _load_supplement(lang_supplement)
        if content:
            parts.append(content)

    # JSON 格式放在最后：模型读到末尾时注意力最集中，输出最准确
    parts.append(_FMT)

    return "\n\n".join(parts)

# 输出预留上限：findings JSON 实测很少超过 1500 token，2048 已经宽裕。
# 动态计算会在这个上限和“剩余空间”之间取小值。
MAX_OUTPUT_TOKENS = 2048
# 安全边距：给模型的 stopping token / 格式开销留余量
TOKEN_BUFFER = 128
# 字符→token 的粗略换算：代码约 3.5 字符/token（中英混合偏保守）
CHARS_PER_TOKEN = 3.5
# 默认上下文长度（测试替身没有 config 时的回退值）
DEFAULT_CONTEXT_LENGTH = 8192


def _estimate_max_tokens(messages: list[dict], context_length: int) -> int:
    """根据输入长度动态计算 max_tokens，避免超出模型上下文窗口。

    教训：曾经写死 4096，加了错题本注入后输入变长，
    4303 input + 4096 output > 8192 直接爆（vLLM 400 错误）。
    动态计算 = 总预算 - 输入估算 - 安全边距，永远不爆。

    context_length 从 client.config.context_length 读：
    本地 14B = 8192，云端模型 = 128000——不同模型自适应。
    """
    total_chars = sum(len(m.get("content", "")) for m in messages)
    est_input = int(total_chars / CHARS_PER_TOKEN)
    available = context_length - est_input - TOKEN_BUFFER
    # 下限 512：就算输入很长，也给模型留够输出空间（否则截断更糟）
    return max(512, min(MAX_OUTPUT_TOKENS, available))


def build_signature_context(entry: dict, chunk: dict, distilled: str | None = None) -> str:
    """给当前块补充“块外符号”的签名摘要。

    场景：函数 A 调用了文件尾部的 helper B，B 不在当前块里。
    模型不知道 B 存在，可能误判 A（比如报“B 未定义”）。
    签名一行一个、成本极低，但模型从此知道这些符号存在、长什么样。

    Args:
        entry: 文件索引条目。
        chunk: 当前代码块。
        distilled: 蒸馏摘要字符串；不为 None 时替代原始签名列表（保留导入注入不变）。
    """
    # 蒸馏摘要优先：有蒸馏结果就用它（更紧凑，节省 token）
    if distilled is not None:
        if not distilled and not entry["imports"]:
            return ""
        lines = ["【参考上下文：本文件其他符号的蒸馏摘要，仅供理解，不要审查它们】"]
        if distilled:
            lines.append(distilled)
        if entry["imports"]:
            lines.append("【导入】 " + "; ".join(entry["imports"][:10]))
        return "\n".join(lines)

    # 列表推导式 + 条件过滤：从索引中挑出“完全不在当前块行号范围内”的符号。
    # if 后面的条件用 or 连接，覆盖两种情况：
    #   s["line_end"] < chunk["line_start"]   符号整个在块之前
    #   s["line_start"] > chunk["line_end"]   符号整个在块之后
    # 与块有交集的符号不用注入——模型在块里能看到它们的完整实现。
    others = [s for s in entry["symbols"]
              if s["line_end"] < chunk["line_start"] or s["line_start"] > chunk["line_end"]]
    # not A and not B：既无块外符号也无导入（说明整个文件就一块），无需注入
    if not others and not entry["imports"]:
        return ""
    lines = ["【参考上下文：本文件其他符号的签名，仅供理解，不要审查它们】"]
    # others[:50] 是列表切片：最多取前 50 个，防止巨型文件的符号表撑爆上下文。
    # （上限从 30 提到 50：登记模块级变量后符号变多，防止函数签名被挤出）
    # 这行也是列表推导式：把每个符号的 signature 字段抽出来组成新列表
    lines += [s["signature"] for s in others[:50]]
    if entry["imports"]:
        # "; ".join(列表)：用分号把列表元素拼成一个字符串；[:10] 同样是截断保护
        lines.append("【导入】 " + "; ".join(entry["imports"][:10]))
    # "\n".join(lines)：把多行文本拼成一整块，最后拼进 user 消息
    return "\n".join(lines)


def review_chunk(client, entry: dict, chunk: dict, distilled: str | None = None,
                 mistakes_text: str = "") -> list[Finding]:
    """审查一个代码块，返回 Finding 列表。

    Args:
        client: LLM 客户端实例。
        entry: 文件索引条目。
        chunk: 当前代码块。
        distilled: 蒸馏摘要字符串；透传给 build_signature_context()。
        mistakes_text: 错题本格式化文本（空串 = 不注入）。
    """
    ctx = build_signature_context(entry, chunk, distilled=distilled)

    # 代码围栏的语言标记直接用文件扩展名（py/js/java/cs/vue...）：
    # 语法高亮提示模型"你在看什么语言"，多语言项目不再一律冒充 python
    lang = chunk["file"].rsplit(".", 1)[-1] if "." in chunk["file"] else ""

    # 拼装 messages：system 立规矩，user 给材料——固定配方
    # 错题本注入：放在代码块之前，让模型先看到“以前犯过的错”再看代码
    user_parts = [
        f"文件路径：{chunk['file']}",
        f"以下代码块覆盖该文件第 {chunk['line_start']}-{chunk['line_end']} 行，"
        f"每行前面的数字就是真实行号，line_start/line_end 必须使用这些行号。",
    ]
    if mistakes_text:
        user_parts.append(mistakes_text)
    if ctx:
        user_parts.append(ctx)
    user_parts.append(f"```{lang}\n{chunk['text']}\n```")

    # 用户线索注入：lra 在调用前把 issue_hint 捎进 entry["_issue_hint"]。
    # 有线索（如"检查是否有 SQL 注入"）就追加到 user 消息**末尾**——
    # 模型读到末尾时注意力最集中。没有线索时这一段不出现，
    # 行为与旧版完全一致（向后兼容）。
    hint = entry.get("_issue_hint")
    if hint:
        user_parts.append(
            f"【用户线索】用户怀疑以下问题可能存在，请重点核查：{hint}")

    messages = [
        # system = 基础 prompt + 算法清单 + 语言补丁（按扩展名动态拼接）
        {"role": "system", "content": _build_system_prompt(client, lang)},
        {"role": "user", "content": "\n".join(user_parts)},
    ]

    try:
        result = chat_structured(
            client,
            messages,
            FindingList,
            temperature=0.2,            # 审查任务要"死板"，不要发挥
            # 动态计算输出预算：从 client 配置读上下文长度，
            # 本地 14B（8192）自动压缩，云端模型（128K）给足 2048。
            max_tokens=_estimate_max_tokens(
                messages,
                getattr(getattr(client, "config", None), "context_length",
                        DEFAULT_CONTEXT_LENGTH)),
        )
    except StructuredOutputError:
        # 最终降级：补丁可能让 prompt 过长导致模型不遵循格式，
        # 用纯基础 prompt（不拼接补丁）再试一次，保证不丢文件
        fallback_sys = (
            load_prompt("reviewer", profile_of(client))
            + "\n\n【重要】你的完整回复必须是且仅是一个合法 JSON 对象"
            "（以 { 开头、以 } 结尾），不要输出任何解释或 markdown 标记。"
            "如果代码没有问题，输出 {\"findings\": []}。"
        )
        fallback_msgs = [
            {"role": "system", "content": fallback_sys},
            {"role": "user", "content": "\n".join(user_parts)},
        ]
        result = chat_structured(
            client, fallback_msgs, FindingList,
            temperature=0.1,
            max_tokens=_estimate_max_tokens(
                fallback_msgs,
                getattr(getattr(client, "config", None), "context_length",
                        DEFAULT_CONTEXT_LENGTH)),
        )

    # 模型填的 file_path 可能幻觉，统一用真实路径覆盖——不信任模型，信任索引
    for f in result.findings:
        f.file_path = chunk["file"]
    return result.findings
