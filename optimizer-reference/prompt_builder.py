"""prompt_builder.py —— 把漏洞日志"翻译"成修复任务书。

为什么修复提示词值得一个独立模块：
审查和修复是两个不同的 agent 干的活，中间这份"任务书"就是它们的交接文档。
交接文档的质量直接决定修复质量——给修改器的指令如果含糊
（"把安全问题修一下"），它就会自由发挥、顺手重构无关代码；
如果精确（哪个文件、哪几行、什么问题、建议怎么改、不许动别的），
它就老老实实干针线活。

两种生成模式（config 里 prompt_mode 可选）：
    template  模板拼装：零模型调用、零 token、结果完全可预测（默认、推荐）
    llm       让本地 14B 读漏洞清单+代码，用自己的话写一份任务书。
              这就是你说的"14B 也有写提示词的能力"——它改代码不可靠，
              但"把结构化信息整理成清晰指令"是它的舒适区。
"""

import json
from dataclasses import dataclass
from pathlib import Path

from cra.llm.prompts import load_prompt, profile_of
from cra.schemas.finding import Finding

# "关思考"等厂商方言已收进 config.yaml 各 profile 的 extra_body，
# agent 代码零厂商知识——换模型改配置，不改代码

# 严重度 -> 排序权重：数字小的排前面。sort(key=...) 会拿这个值比大小
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass
class FixTask:
    """一个文件的修复任务：漏洞清单 + 任务书全文 + 任务书落盘路径。

    为什么按文件组织任务而不是按漏洞：修改器改代码的最小单位是文件，
    同一个文件的几个问题必须一次性交给它一起修——
    否则它第一次重写文件修了 F1，第二次重写又把 F1 的修复冲掉了。
    """

    file_path: str
    findings: list[Finding]
    prompt_text: str
    prompt_file: str = ""        # build_tasks 落盘后回填


def group_by_file(findings: list[Finding]) -> dict[str, list[Finding]]:
    """按文件分组，组内按严重度排序（最要命的排最前面）。"""
    groups: dict[str, list[Finding]] = {}
    for f in findings:
        # setdefault：第一次遇到某文件时先建空列表，再 append
        groups.setdefault(f.file_path, []).append(f)
    for fs in groups.values():
        # key 是个 lambda：告诉 sort 拿什么比大小
        fs.sort(key=lambda f: SEVERITY_ORDER[f.severity])
    return groups


def render_task_template(file_path: str, findings: list[Finding], code: str,
                         feedback: dict | None = None,
                         keep: list[str] | None = None) -> str:
    """template 模式：机械拼装，不调用任何模型。

    好处：零 token、零延迟、输出 100% 可预测、可单元测试。
    代价：任务书写得"死"，不会指出问题之间的冲突（那是 llm 模式的价值）。

    迭代修复的两个增量输入：
        feedback  {finding_id: 复查判失败的理由}——"错题本"，
                  告诉修改器上一轮为什么被判不及格，别再交同样的答案
        keep      同文件里已 verified 的问题标题——提醒修改器
                  修新问题时别把人家已经修好的地方改回去（防回归）
    """
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
        # 防回归警示：修改器重写整个文件时，最容易顺手破坏已修好的部分
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
        # += 一次追加多行，比一行行 append 紧凑
        lines += [
            f"### {f.id} [{f.severity}] {f.title}",
            f"- 分类：{f.category}",
            f"- 位置：第 {f.line_start}-{f.line_end} 行",
            f"- 问题：{f.description}",
            f"- 证据：\n```python\n{f.evidence}\n```",
            f"- 建议修法：{f.suggestion}",
        ]
        if feedback and f.id in feedback:
            # 错题本：复查理由是修改器最需要的反馈——比"再试一次"有用得多
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
    """llm 模式：让本地 14B 把漏洞清单"翻译"成自然语言任务书。

    输出是自由文本（不走 chat_structured）：任务书本来就是给人/下游
    agent 读的散文，不需要 JSON 契约——该松的地方松，该紧的地方紧
    （审查输出必须是 JSON，任务书不必）。
    """
    # 按 client 的 profile 找模型专版提示词（没有专版回退通用版）
    system = load_prompt("optimizer", profile_of(client))
    # 迭代反馈直接塞进漏洞字典里发给模型：让它撰写任务书时
    # 就把"上一轮为什么失败"揉进修法指令，比模板拼贴更自然
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
        temperature=0.3,            # 整理指令可以有一点点发挥空间
        max_tokens=2048,
    )


def build_tasks(
    findings: list[Finding],
    copy_root: str | Path,
    run_dir: str | Path,
    state=None,
    client=None,
    mode: str = "template",
    feedback: dict | None = None,
    keep: dict | None = None,
    subdir: str = "",
) -> list[FixTask]:
    """主入口：分组 -> 逐文件生成任务书 -> 落盘 -> 更新记忆。

    state 传进来时，被处理的漏洞状态从 pending 推进到 prompted——
    打开 opt_state.json 就能看到"提示词已生成、等待修改器接手"。

    迭代修复相关的三个参数：
        feedback  {finding_id: 复查失败理由}，渲染进任务书当"错题本"
        keep      {file_path: [已修好问题的标题]}，渲染成"防回归"警示
        subdir    任务书写到 prompts/<subdir>/ 下——迭代时每轮一个子目录，
                  历史任务书不被覆盖，事后能复盘"每一轮让它修了什么"
    """
    if mode == "llm" and client is None:
        raise ValueError("llm 模式必须传入 client（本地 14B 靠它说话）")

    copy_root = Path(copy_root)
    # Path 拼空字符串是恒等操作：subdir="" 时就是 prompts/ 本身
    prompts_out = Path(run_dir) / "prompts" / subdir
    prompts_out.mkdir(parents=True, exist_ok=True)

    tasks: list[FixTask] = []
    for file_path, fs in group_by_file(findings).items():
        # 从副本里读代码（不是原项目！）——修改器看到的就是它要改的那份
        code = (copy_root / file_path).read_text(encoding="utf-8", errors="replace")
        keep_list = (keep or {}).get(file_path)

        if mode == "llm":
            text = render_task_llm(client, file_path, fs, code,
                                   feedback=feedback, keep=keep_list)
        else:
            text = render_task_template(file_path, fs, code,
                                        feedback=feedback, keep=keep_list)

        # 文件路径里的 / 不能出现在文件名里，换成 __ 拍平：
        # a/b/c.py -> a__b__c.py.task.md
        safe = file_path.replace("/", "__")
        p = prompts_out / f"{safe}.task.md"
        p.write_text(text, encoding="utf-8")

        if state is not None:
            for f in fs:
                state.set_finding_status(f.id, "prompted")

        tasks.append(FixTask(file_path=file_path, findings=fs,
                             prompt_text=text, prompt_file=str(p)))
    return tasks
