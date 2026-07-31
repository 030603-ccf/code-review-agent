"""prompts.py —— 提示词加载器：按 profile 定制，逐级回退。

为什么需要它（你提 minimax 时问的那个问题）：
不同模型的能力、上下文、指令遵循风格差别很大，一套提示词打天下是有损的——
14B 要短指令死格式关思考，推理模型要给足上下文留推理空间，
MiniMax-M3 又有自己的脾气。给模型定制提示词，本质是因材施教。

回退链（约定优于配置，不加一行 yaml）：
    prompts/<name>.<profile>.md   某模型专版（如 verifier.local_vllm.md）
    prompts/<name>.md             通用版（没有专版时的兜底）

想给新模型定制？把通用版复制成 <name>.<profile>.md 改就行，
其他模型的行为一个字节都不会被影响——定制的意义就是隔离。
"""

from pathlib import Path

# prompts 目录定位：cra/llm/prompts.py -> 上两级是项目根
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def load_prompt(name: str, profile: str | None = None) -> str:
    """加载 agent 的 system prompt：有 profile 专版用专版，没有用通用版。

    name     提示词角色名（reviewer / verifier / optimizer...）
    profile  模型配置名（client.config.name），None 时直接用通用版
    """
    if profile:
        special = PROMPTS_DIR / f"{name}.{profile}.md"
        # is_file 而不是 exists：同名目录也存在但读不了，is_file 更精确
        if special.is_file():
            return special.read_text(encoding="utf-8")
    # 通用版必须存在，不存在就让 FileNotFoundError 炸出来——
    # 提示词文件缺失是部署错误，不该静默降级成空人设
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def profile_of(client) -> str | None:
    """从 client 上取 profile 名；取不到（测试替身没有 config）返回 None。

    getattr(对象, "属性", 默认值)：属性不存在时返回默认值而不是抛
    AttributeError——测试里的 FakeChecker/FakeClient 没有 .config，
    两层 getattr 防御后返回 None，load_prompt 自然回退通用版。
    真实 client 一定走 LLMConfig.name，专版提示词才会命中。
    """
    return getattr(getattr(client, "config", None), "name", None)
