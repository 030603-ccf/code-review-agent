"""second_reviewer.py —— 二级审查员：漏斗的下半段。

漏斗架构（你拍板的方案，对比实验 runs/compare_m3 是它的依据）：
    初审（本地 14B）  召回优先：可疑即报，宁可错杀不可漏放——
                      误报没关系，后面有人筛；漏报就永远丢了
    二级审查（云端）  精确优先：对初审的每条发现做终审仲裁

为什么强模型只复核不初审：
14B 的失败模式是幻觉、形式匹配、定级虚高——捞上来的东西要过筛；
而强模型全量初审的成本是 14B 的几十倍。只让它复核初审的产出，
一次请求裁决一个文件的全部条目，成本只有全量重审的零头。

三级裁决（挂在 Finding.second_verdict 上，条目永不删除）：
    confirmed   问题成立（可顺手修正初审的严重度）
    rejected    不成立（附驳回理由——驳回是最好的学习材料）
    uncertain   存疑（材料不足/拿不准/模型漏判）——留给人判，
                驳回是重判，宁缺毋滥的原则在这里反过来用
"""

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from cra.llm.prompts import load_prompt, profile_of
from cra.llm.structured import chat_structured
from cra.schemas.finding import Finding
from cra.schemas.verdict import SecondReviewResult

if TYPE_CHECKING:
    from cra.analysis.lsp_client import LSPClient

logger = logging.getLogger(__name__)

# 每条初审发现带前后多少行上下文去复核。
# 复核是判定题不是通读题：模型只需看清"问题点位"，不需要全文件
WINDOW_LINES = 12

# 合法裁决集合：模型输出三态之外的词时按 uncertain 处理（防自由发挥）
VERDICTS = {"confirmed", "rejected", "uncertain"}


def _window(lines: list[str], f: Finding) -> str:
    """给一条初审发现截取前后 WINDOW_LINES 行的源码（带真实行号）。

    range(lo, hi+1) 生成行号，i-1 才是 lines 的下标（行号从 1 起）。
    """
    lo = max(1, f.line_start - WINDOW_LINES)
    hi = min(len(lines), f.line_end + WINDOW_LINES)
    body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(lo, hi + 1))
    return f"### {f.id} 的上下文（第 {lo}-{hi} 行）\n{body}"


def _check_file(client, relpath: str, src_lines: list[str],
                items: list[Finding]) -> SecondReviewResult:
    """复核一个文件的全部初审发现：一次请求，逐条裁决。

    按文件打包（和 Verifier 同一个省 token 原则）：
    一个文件有 1 条是 1 次请求，有 10 条也是 1 次请求。
    """
    system = load_prompt("second_reviewer", profile_of(client))
    # 只挑仲裁需要的字段：suggestion（怎么修）与"成立与否"无关，少发少干扰
    brief = [
        {
            "finding_id": f.id,
            "category": f.category,
            "severity": f.severity,
            "lines": f"{f.line_start}-{f.line_end}",
            "title": f.title,
            "description": f.description,
            "evidence": f.evidence,
            "confidence": f.confidence,
        }
        for f in items
    ]
    windows = "\n\n".join(_window(src_lines, f) for f in items)
    user = (
        f"文件：{relpath}\n\n"
        f"【初审发现清单】\n{json.dumps(brief, ensure_ascii=False, indent=2)}\n\n"
        f"【相关源码】\n{windows}"
    )
    return chat_structured(
        client,
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        SecondReviewResult,
        temperature=0.1,     # 仲裁要稳定，不要发挥
        max_tokens=2048,     # 每条裁决几十 token，2048 对一个文件的量绰绰有余
    )


def second_review(findings: list[Finding], root: str | Path, client,
                  bus=None, save_path: str | Path | None = None,
                  lsp_client: "LSPClient | None" = None
                  ) -> list[Finding]:
    """二级审查主入口：逐文件复核，裁决挂回每条 Finding。

    返回的还是原列表（就地修改）：confirmed/rejected/uncertain 都保留，
    一条不删——被驳回的条目带着驳回理由，是初审模型最好的学习材料。

    save_path：增量落盘路径。每复核完一个文件立刻全量写一次——
    中途被杀，已复核的裁决都在盘上（全量实战被超时掐断、
    已复核 3 个文件的裁决全丢的教训换来的）。

    lsp_client：可选的 LSP 客户端；不为 None 时对每条 Finding 的证据位置
    调用 find_references()，将引用信息附加到 Finding.references 字段。
    """
    root = Path(root)
    # 断点续跑：已有终局裁决（confirmed/rejected）的条目跳过——
    # 不重复花 token，也绝不推翻已落盘的仲裁。
    # uncertain 会被重新复核：它可能是上次请求失败造成的，值得第二次机会
    todo = [f for f in findings
            if f.second_verdict not in ("confirmed", "rejected")]
    skipped = len(findings) - len(todo)
    if skipped and bus:
        bus.emit("second_review", "SecondReviewer",
                 f"断点续跑：跳过 {skipped} 条已有终局裁决")
    by_file: dict[str, list[Finding]] = {}
    for f in todo:
        by_file.setdefault(f.file_path, []).append(f)

    # 增量落盘闭包：把"当前完整事实"写盘。
    # 写的是整个 findings（含跳过和未复核的）——文件始终是完整事实，
    # 续跑时靠 second_verdict 区分"已终局/待复核"。
    # 定义成闭包是因为成功/失败两条路径都要用它，避免复制两份写盘代码
    def _persist() -> None:
        if save_path:
            Path(save_path).write_text(
                json.dumps([f.model_dump() for f in findings],
                           ensure_ascii=False, indent=2),
                encoding="utf-8")

    stats = {"confirmed": 0, "rejected": 0, "uncertain": 0}
    for relpath, items in by_file.items():
        src = root / relpath
        if not src.exists():
            # 聚合器已校验过文件存在，这是双保险（比如复核前文件被移动）
            continue
        src_lines = src.read_text(
            encoding="utf-8", errors="replace").splitlines()

        # 故意宽捕获：一个文件复核失败不拖垮整个 run，
        # 该文件全部落 uncertain——复核失败的后果绝不能是"误判成立/不成立"
        try:
            result = _check_file(client, relpath, src_lines, items)
        except Exception as e:
            for f in items:
                f.second_verdict = "uncertain"
                f.second_reason = f"复核请求失败：{type(e).__name__}: {e}"
                stats["uncertain"] += 1
            _persist()   # 失败也落盘：uncertain 是"请求失败"的实锤记录
            continue

        verdict_by_id = {v.finding_id: v for v in result.verdicts}
        for f in items:
            v = verdict_by_id.get(f.id)
            if v is None:
                # 模型漏判：落 uncertain 而不是保持原样——
                # "没被复核过"必须在报告里可见，不能伪装成已复核
                f.second_verdict = "uncertain"
                f.second_reason = "复核输出缺少该条裁决"
                stats["uncertain"] += 1
                continue
            # 模型自由发挥了三态之外的词：按 uncertain 收编，理由留痕
            if v.verdict not in VERDICTS:
                f.second_verdict = "uncertain"
                f.second_reason = f"裁决用词非法（{v.verdict!r}）：{v.reason}"
                stats["uncertain"] += 1
                continue
            f.second_verdict = v.verdict
            f.second_reason = v.reason
            stats[v.verdict] += 1
            # confirmed 且仲裁员顺手修正了定级：采信修正，理由里留痕
            if v.verdict == "confirmed" and v.severity \
                    and v.severity != f.severity:
                f.second_reason = f"严重度 {f.severity}→{v.severity}：{v.reason}"
                f.severity = v.severity

        if bus:
            bus.emit("second_review", "SecondReviewer",
                     f"复核 {relpath}：{len(items)} 条")

        # LSP 引用查找（可选）：对每条 Finding 的证据位置查找引用
        if lsp_client is not None:
            for f in items:
                try:
                    abs_path = str(root / f.file_path)
                    refs = lsp_client.find_references(
                        abs_path, f.line_start, 0)
                    if refs:
                        ref_strs: list[str] = []
                        for r in refs:
                            r_uri = r.get("uri", "")
                            r_range = r.get("range", {})
                            r_line = r_range.get("start", {}).get("line", 0) + 1
                            # 从 URI 提取相对路径（尽力而为）
                            r_path = r_uri.split("/")[-1] if "/" in r_uri else r_uri
                            ref_strs.append(f"{r_path}:{r_line}")
                        f.references = ref_strs
                except Exception as e:
                    logger.debug("LSP 引用查找失败 [%s:%d]: %s",
                                 f.file_path, f.line_start, e)

        # 每复核完一个文件立刻落盘——中途被杀，已复核的裁决都在盘上
        _persist()
    if bus:
        bus.emit("second_review", "SecondReviewer",
                 f"仲裁完成：✅ {stats['confirmed']} · ❌ {stats['rejected']} · "
                 f"❓ {stats['uncertain']}")
    return findings
