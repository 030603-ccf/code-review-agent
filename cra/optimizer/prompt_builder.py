"""prompt_builder.py —— 把漏洞日志"翻译"成修复任务书。

两种生成模式（config 里 prompt_mode 可选）：
    template  模板拼装：零模型调用、零 token、结果完全可预测（默认）
    llm       让本地模型读漏洞清单+代码，用自己的话写一份任务书
"""

import json
from dataclasses import dataclass
from pathlib import Path

from cra.llm.prompts import load_prompt, profile_of
from cra.schemas.finding import Finding

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass
class FixTask:
    """一个文件的修复任务：漏洞清单 + 任务书全文 + 任务书落盘路径。"""

    file_path: str
    findings: list[Finding]
    prompt_text: str
    prompt_file: str = ""


def group_by_file(findings: list[Finding]) -> dict[str, list[Finding]]:
    """按文件分组，组内按严重度排序。"""
    groups: dict[str, list[Finding]] = {}
    for f in findings:
        groups.setdefault(f.file_path, []).append(f)
    for fs in groups.values():
        fs.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
    return groups


def render_task_template(file_path: str, findings: list[Finding], code: str,
                         feedback: dict | None = None,
                         keep: list[str] | None = None) -> str:
    """template 模式：机械拼装，不调用任何模型。"""
    lines = [
        f"# 修复任务：{file_path}",
        "",
        "请修复下面列出的问题。规则：",
        "1. 只修复列出的问题，禁止重构、改名、格式化无关代码",
        "2. 保持公共接口不变（函数名、参数、返回值），除非该接口本身就是问题",
        "3. 修完代码必须能正常运行，不许引入新的 import 错误或语法错误",
        "",
    ]
    if keep:
        lines += [
            "## ⚠️ 本文件已修好的问题（请勿破坏这些修法）",
            "",
            *[f"- {t}" for t in keep],
            "",
        ]
    lines += [
        f"## 问题清单（共 {len(findings)} 条，已按严重度排序）",
        "",
    ]
    for f in findings:
        lines += [
            f"### {f.id} [{f.severity}] {f.title}",
            f"- 分类：{f.category}",
            f"- 位置：第 {f.line_start}-{f.line_end} 行",
            f"- 问题：{f.description}",
            f"- 证据：\n```python\n{f.evidence}\n```",
            f"- 建议修法：{f.suggestion}",
        ]
        if feedback and f.id in feedback:
            lines += [
                f"- ⚠️ 上一轮修复被复查判为失败：{feedback[f.id]}",
                f"  请分析失败原因，换用真正有效的修法，不要交同样的答案",
            ]
        lines.append("")
    lines += [
        "## 当前文件全文",
        "",
        f"```python\n{code}\n```",
    ]
    return "\n".join(lines)


def render_task_llm(client, file_path: str, findings: list[Finding], code: str,
                    feedback: dict | None = None,
                    keep: list[str] | None = None) -> str:
    """llm 模式：让模型把漏洞清单"翻译"成自然语言任务书。"""
    system = load_prompt("optimizer", profile_of(client))
    items = []
    for f in findings:
        d = f.model_dump()
        if feedback and f.id in feedback:
            d["上一轮修复失败原因"] = feedback[f.id]
        items.append(d)
    findings_json = json.dumps(items, ensure_ascii=False, indent=2)
    user = (
        f"文件路径：{file_path}\n\n"
        f"【漏洞清单】\n{findings_json}\n\n"
        f"【当前文件全文】\n```python\n{code}\n```"
    )
    if keep:
        user += ("\n\n【已修好的问题，要求修改器请勿破坏】\n"
                 + "\n".join(f"- {t}" for t in keep))
    return client.chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
        max_tokens=2048,
    )


def build_tasks(
    findings: list[Finding],
    copy_root,
    run_dir,
    state=None,
    client=None,
    mode: str = "template",
    feedback: dict | None = None,
    keep: dict | None = None,
    subdir: str = "",
) -> list[FixTask]:
    """主入口：分组 -> 逐文件生成任务书 -> 落盘 -> 更新记忆。"""
    if mode == "llm" and client is None:
        raise ValueError("llm 模式必须传入 client")

    copy_root = Path(copy_root)
    prompts_out = Path(run_dir) / "prompts" / subdir
    prompts_out.mkdir(parents=True, exist_ok=True)

    tasks: list[FixTask] = []
    for file_path, fs in group_by_file(findings).items():
        code = (copy_root / file_path).read_text(encoding="utf-8", errors="replace")
        keep_list = (keep or {}).get(file_path)

        if mode == "llm":
            text = render_task_llm(client, file_path, fs, code,
                                   feedback=feedback, keep=keep_list)
        else:
            text = render_task_template(file_path, fs, code,
                                        feedback=feedback, keep=keep_list)

        safe = file_path.replace("/", "__").replace("\\", "__")
        p = prompts_out / f"{safe}.task.md"
        p.write_text(text, encoding="utf-8")

        if state is not None:
            for f in fs:
                state.set_finding_status(f.id, "prompted")

        tasks.append(FixTask(file_path=file_path, findings=fs,
                             prompt_text=text, prompt_file=str(p)))
    return tasks
