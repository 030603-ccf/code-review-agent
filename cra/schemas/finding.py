"""Finding —— agent 之间传递漏洞信息的"契约"（pydantic 模型）。

为什么契约这么重要：
多 agent 系统里最脆弱的地方就是 agent 之间的交接。Reviewer 输出的东西
Aggregator 必须能读懂、Optimizer 必须能执行——全靠这个 schema 保证。
pydantic 会在校验失败时给出人类可读的错误，这个错误还能回喂给模型让它自我修正。
"""

from typing import Literal

from pydantic import BaseModel, Field

# Literal 把取值限制成枚举：模型只能四选一，不允许自由发挥
Category = Literal["security", "performance", "readability", "best_practice"]
Severity = Literal["critical", "high", "medium", "low"]


class Finding(BaseModel):
    """一条漏洞/质量问题记录。"""

    id: str                                   # 编号，如 "F1"
    category: Category                        # 分类：安全/性能/可读性/最佳实践
    severity: Severity                        # 严重度
    file_path: str                            # 相对路径
    line_start: int                           # 问题起始行
    line_end: int                             # 问题结束行
    title: str                                # 一句话标题
    description: str                          # 问题是什么、为什么有害
    evidence: str                             # 从源文件照抄的证据代码（Aggregator 会校验它真实存在）
    suggestion: str                           # 修复建议
    confidence: float = Field(ge=0, le=1)     # 置信度 0~1，用于过滤低质量报告

    # ---- LSP 诊断来源（如 "pyright"）----
    lsp_source: str | None = None
    # ---- 引用位置（如 ["file.py:42"]）----
    references: list[str] | None = None

    # ---- 证据救援标记（聚合器模糊匹配成功时置 True）----
    evidence_corrected: bool = False  # 证据被救援修正过（原始证据有微小抄写误差）

    # ---- 二级审查的裁决结果（终审仲裁员填写，没跑二级审查就是 None）----
    # 不删除被驳回的条目而是挂标记：驳回理由是最好的学习材料——
    # 它能回答"初审模型在什么上栽了跟头"
    second_verdict: str | None = None   # confirmed / rejected / uncertain
    second_reason: str | None = None    # 裁决理由（驳回原因 / 严重度修正说明）


class FindingList(BaseModel):
    """Reviewer 单次审查的输出容器。

    为什么包一层：让模型输出 {"findings": [...]} 比直接输出 [...] 更稳定，
    小模型处理"顶层是对象"的 JSON 明显比"顶层是数组"更可靠。
    """

    findings: list[Finding]
