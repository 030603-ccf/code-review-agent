"""FixVerdict —— 定向复查的"判定契约"。

和 Finding 的分工：
    Finding     审查阶段的输出："这里有个问题"（开放式发现）
    FixVerdict  复查阶段的输出："上次说的那个问题，还在不在"（封闭式判定）

为什么判定也要 schema：开放式重审翻车（把教科书级安全写法当新问题）
本质就是"让模型自由发挥"。契约把输出锁成三个字段——
在不在、为什么——模型没有发挥的余地。
"""

from pydantic import BaseModel

from cra.schemas.finding import Severity   # 严重度枚举复用，全项目一个口径


class FixVerdict(BaseModel):
    """对一条旧漏洞的判定。"""

    finding_id: str      # 回显旧漏洞的编号，靠它把判定对号入座
    still_exists: bool   # True = 问题还在；False = 已修好
    reason: str          # 一句话理由，写进报告给人看


class NewIssue(BaseModel):
    """修复过程中引入的"新问题"（只进报告，不参与旧账判定）。

    旧账要逐条对质，新账只需提醒——它的作用是让人知道
    "这次修改可能带进了别的东西"，具体由人复核 diff 确认。
    """

    title: str
    severity: Severity
    line_start: int
    line_end: int
    description: str


class FileCheckResult(BaseModel):
    """一个文件的复查总结果：旧漏洞逐条判定 + 新问题清单。

    包一层容器（而不是直接返回数组）的原因和 FindingList 一样：
    小模型处理"顶层是对象"的 JSON 明显更稳定。
    """

    verdicts: list[FixVerdict]
    new_issues: list[NewIssue] = []   # 默认空列表：没发现新问题时模型可以不填


class SecondVerdict(BaseModel):
    """二级审查对一条初审发现的仲裁（对比 FixVerdict 的分工见模块 docstring）。

    verdict 三态而不是二值：
        confirmed   问题成立（驳回必须谨慎，但确认也不能放水）
        rejected    不成立（初审的幻觉/洁癖/形式匹配，在这里被拦下）
        uncertain   存疑（窗口材料不足或拿不准）——留给人判，
                    模型漏判某条时也落这档：驳回是重判，宁缺毋滥
    """

    finding_id: str
    verdict: str                 # confirmed / rejected / uncertain
    severity: Severity | None = None   # confirmed 时可顺手修正初审的定级
    reason: str                  # 裁决理由：驳回说"为什么不成立"，确认说"依据是什么"


class SecondReviewResult(BaseModel):
    """一个文件的二级审查总结果（顶层是对象，同 FindingList 的理由）。"""

    verdicts: list[SecondVerdict]
