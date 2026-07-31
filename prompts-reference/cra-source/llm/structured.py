"""结构化输出 —— 让小模型稳定吐出合法 JSON 的三板斧。

14B 模型输出 JSON 的翻车方式：套 markdown 围栏、前面加"好的"、
字段名瞎编、类型不对。对策：
  1. 抠：从输出里把 JSON 抠出来（容忍围栏和废话）
  2. 验：用 pydantic schema 校验
  3. 喂：校验失败就把错误信息回喂给模型，让它自我修正（最多重试 N 次）
"""

import json
import re

from pydantic import BaseModel, ValidationError


class StructuredOutputError(Exception):
    """重试耗尽后抛出。上层（Orchestrator）应该捕获它并记录为"该文件审查失败"。"""


# 模型用自然语言说"没问题"的常见表述（中英双语）
_NO_ISSUE_PATTERNS = re.compile(
    r"(没有发现|未发现|不存在|无问题|没有问题|代码质量良好|代码没有|"
    r"no\s+(issues?|bugs?|problems?|findings?)|"
    r"the code (is|looks) (correct|fine|good|clean))",
    re.IGNORECASE,
)

# DeepSeek 等模型可能输出的思考标签
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _extract_json(text: str) -> str:
    """从模型输出里抠出第一个 JSON 对象。

    容忍三种常见污染：
      - ```json 围栏
      - JSON 前后的解释性废话
      - <think> 思考标签（DeepSeek 等模型）
    策略：找第一个 { 和最后一个 }，中间的就是 JSON 本体。
    兜底：如果完全没有 JSON 但模型表达了"没问题"，返回空 findings。
    """
    # strip() 去掉首尾空白，方便后续判断
    t = text.strip()

    # 剥离 \<think\>...\</think\> 思考内容（DeepSeek 等模型偶尔泄漏到 content 里）
    t = _THINK_RE.sub("", t).strip()

    # 先尝试去掉 markdown 围栏
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()

    # 为什么是「找第一个 { 和最后一个 }」？ 因为 14B 模型常在 JSON 前后加废话
    # （"好的，我审查完了：\n\n{...}\n\n希望对你有帮助"），这种首尾定位法对这种污染最鲁棒。
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        return t[start:end + 1]

    # 兜底：模型用自然语言说"没发现问题"但忘了输出 JSON——视为空结果
    if _NO_ISSUE_PATTERNS.search(t):
        return '{"findings": []}'

    raise ValueError("输出里找不到 JSON 对象")


def chat_structured(
    client,
    messages: list[dict],
    schema: type[BaseModel],
    max_retries: int = 3,
    **chat_overrides,
):
    """调模型 -> 解析 -> 校验，失败则回喂错误重试。

    参数 schema 是一个 pydantic 模型类（如 FindingList），
    返回值就是校验通过的模型实例——上层拿到的永远是干净的结构化数据。

    max_retries=3：1 次首发 + 3 次纠错 = 最多 4 次调用。
    补丁加长 system prompt 后模型偶尔忘记 JSON 格式，多给一次机会显著降低失败率。

    **chat_overrides 原样透传给 client.chat（temperature、extra_body 等）。
    """
    convo = list(messages)   # 拷贝一份，不改调用方的列表
    last_err: Exception | None = None

    for attempt in range(max_retries + 1):
        text = client.chat(convo, **chat_overrides)
        try:
            data = json.loads(_extract_json(text))
            # model_validate：pydantic 把 dict 校验并转成模型实例，
            # 字段缺失/类型错误/枚举越界都会抛 ValidationError
            return schema.model_validate(data)
        except (ValueError, ValidationError) as e:
            last_err = e
            # 关键一步：把模型的错误输出和我们的报错一起追加进对话，
            # 模型能看到"我刚才错在哪"，下一轮自我修正。
            # 这就是 agent 开发里的"错误回喂"（error feedback loop）。
            # 第 2 次起用更强的格式提醒（模型"屡教不改"时加大力度）
            if attempt >= 1:
                hint = (
                    f"你的输出未通过校验：{e}\n"
                    "【严格格式要求】你的回复必须以 { 开头、以 } 结尾，"
                    "是一个合法 JSON 对象。不要输出任何解释、思考过程或 markdown 标记。"
                    "如果代码没有问题，输出 {\"findings\": []}。"
                )
            else:
                hint = (
                    f"你的输出未通过校验：{e}\n"
                    "请重新输出，只输出符合要求的 JSON，不要任何其他内容。"
                )
            convo = convo + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": hint},
            ]

    raise StructuredOutputError(f"结构化输出重试 {max_retries} 次后仍失败：{last_err}")
