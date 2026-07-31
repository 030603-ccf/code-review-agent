"""verifier.py —— 复查员 v2：定向复查。

v1（开放式重审）的教训，你在首跑报告里亲眼见过：
让同一个洁癖模型把修复后的代码重新挑一遍刺，它把教科书级安全写法
（ast.literal_eval、except FileNotFoundError）当成新问题报出来，
行号窗口匹配就把旧账误判成"没修好"——5 条全修对的代码被判 3 条"仍存在"。

v2 改成定向复查：拿着每条旧漏洞的原始记录（位置/证据/描述），
逐条问模型"这个问题在新代码里还存在吗"。
问题越具体，模型越没有自由发挥的余地——和 PromptBuilder 是同一个道理。

token 账也更漂亮：
    每个修改过的文件只发一次请求（装着该文件的全部旧漏洞），
    没动的文件依然零消耗（哈希对比挡在前面）。

确定性安全网一张不少（这些判断不经过模型，代码做 100% 可靠）：
    文件被删除 / 改出语法错误 / 声称修了但哈希没变

v2.1（diff 对质）：只发修复后代码时，14B 的洁癖会复发——
看到 except/import 的"形式"还在就判"没修好"，看不到语义上已加守卫
（真实事故：opencode 把 7 条漏洞全修对，14B 全判"仍存在"）。
解药是把材料从"修复后代码"换成"修复前后 diff"：
任务从开放式"再审查一遍"降级为封闭式"对账"——旧形态（- 行）还在不在？
材料更短、判断更死，恰好扬长（模式匹配）避短（语义推理）。
"""

import difflib
import json
from pathlib import Path

from cra.llm.prompts import load_prompt, profile_of
from cra.llm.structured import chat_structured
from cra.optimizer.copier import diff_hashes, hash_tree
from cra.schemas.finding import Finding
from cra.schemas.verdict import FileCheckResult

# "关思考"等厂商方言已收进 config.yaml 各 profile 的 extra_body，
# agent 代码零厂商知识——换模型改配置，不改代码

# 整文件对质的行数上限。本地 14B 的上下文只有 8192 token，
# 塞 800 行的文件全文（约 7k token）加上漏洞清单就会爆。
# 超过上限就改用"窗口对质"：每条漏洞只带它前后若干行上下文——
# 反正要判定的是"那几条旧账"，不是通读全文件。
VERIFY_FULL_FILE_LINES = 400
# diff 对质的行数上限：修改器改得太猛（接近重写全文件）时，
# diff 比文件本身还长，"只发变化处"就没意义了——退回窗口对质
DIFF_MAX_LINES = 250
# 窗口半宽：真实运行量出来的预算——8192 上下文里，系统提示词约 400、
# 漏洞清单约 1200、输出预留 1024，留给代码材料约 5.5k token ≈ 550 行。
# 7 条漏洞 × (2×20+1) 行 ≈ 287 行，绰绰有余；40 行就会顶爆（教训在此）
WINDOW_LINES = 20


def _diff_text(old: str, new: str, relpath: str) -> str:
    """生成 unified diff 文本；没变化或 diff 太大时返回空串。

    difflib.unified_diff 是标准库的逐行对比器：输入两个文本的行序列，
    输出带 @@ 定位标记、- 旧行、+ 新行的 diff 行序列。
    keepends=True 让每行保留换行符，"".join 之后就是能直接读的 diff 文本。
    n=3：每处改动带 3 行上下文——够模型定位问题位置，又不浪费材料。

    返回空串的两种情况，调用方都退回"全文/窗口"材料：
        old == new   内容一样，没有 diff 可发（防御性分支，正常走不到）
        diff 太大    修改器接近重写全文件，diff 行数超 DIFF_MAX_LINES
    """
    if old == new:
        return ""
    text = "".join(difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"修复前 {relpath}",   # diff 头部的文件名标记，给模型看的
        tofile=f"修复后 {relpath}",
        n=3,
    ))
    return text if len(text.splitlines()) <= DIFF_MAX_LINES else ""


def _windowed(lines: list[str], originals: list[Finding]) -> str:
    """给每条旧漏洞截取前后 WINDOW_LINES 行的上下文片段（带真实行号）。

    漏洞扎堆时窗口会重叠——先合并再发，同一行代码不让模型读两遍：
    13 条漏洞各自 81 行 ≈ 1053 行，合并相邻后往往只剩几段。
    """
    n = len(lines)
    spans = sorted(
        (max(1, f.line_start - WINDOW_LINES), min(n, f.line_end + WINDOW_LINES))
        for f in originals
    )
    # 区间合并：下一个窗口的起点 <= 当前终点的下一行，就并成一段
    merged: list[list[int]] = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])

    windows = []
    for lo, hi in merged:
        # 标题列出这个窗口覆盖的漏洞 id，方便模型对号入座
        ids = [f.id for f in originals
               if f.line_start <= hi and f.line_end >= lo]
        # range(lo, hi+1) 生成行号，i-1 才是 lines 的下标（行号从 1 起）
        body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(lo, hi + 1))
        windows.append(f"### {', '.join(ids)} 的上下文（第 {lo}-{hi} 行）\n{body}")
    return "\n\n".join(windows)


def _check_file(client, relpath: str, new_content: str,
                originals: list[Finding], diff_text: str = "") -> FileCheckResult:
    """对质一个文件：把它的全部旧漏洞和改动材料一起发给模型，逐条要判定。

    一个文件只发一次请求——旧漏洞有 1 条是 1 次，有 10 条也是 1 次。
    这就是"按文件打包"比"按漏洞逐条问"省 token 的地方。

    diff_text 是修复前后的 unified diff（_diff_text 生成）：
    传了就走"diff 对质"，空串则退回"全文/窗口"材料。
    """
    # 按 client 的 profile 找模型专版提示词（没有专版回退通用版）
    system = load_prompt("verifier", profile_of(client))
    # 只挑判定需要的字段发给模型： suggestion（怎么修）已经无关，
    # 复查只关心"问题还在不在"，少发字段就是少干扰
    brief = [
        {
            "finding_id": f.id,
            "severity": f.severity,
            "category": f.category,
            "lines": f"{f.line_start}-{f.line_end}",
            "title": f.title,
            "description": f.description,
            "evidence": f.evidence,
        }
        for f in originals
    ]
    # 材料三级优先：diff 对质 > 全文 > 窗口。
    # diff 是洁癖噪声的解药（opencode 修对 7 条、14B 全误判的教训）：
    # 只发修复后代码，模型只能重新挑刺——看到 except/import 的"形式"还在
    # 就判"没修好"，看不到语义上已加守卫；给它 diff（- 旧 + 新），
    # 任务从开放式"再审查一遍"降级为封闭式"对账"，判断空间越小越可靠。
    lines = new_content.splitlines()
    # 围栏语言标记用文件扩展名（和 reviewer 同一个手法）：
    # 告诉模型"你在看什么语言"，多语言项目不再一律冒充 python
    lang = relpath.rsplit(".", 1)[-1] if "." in relpath else ""
    if diff_text:
        code_part = (
            f"【修复前后的改动（unified diff，- 开头是修复前，+ 开头是修复后）】\n"
            f"```diff\n{diff_text}```\n\n"
            f"注意：diff 之外的代码没有变化。判定依据是问题模式的旧形态（- 行）\n"
            f"是否已被新形态（+ 行）消除，而不是相关代码是否仍然存在。"
        )
    elif len(lines) <= VERIFY_FULL_FILE_LINES:
        code_part = f"【修复后的文件全文】\n```{lang}\n{new_content}\n```"
    else:
        code_part = (
            f"【修复后的相关代码片段】（文件共 {len(lines)} 行，"
            f"只取每条漏洞前后 {WINDOW_LINES} 行）\n"
            f"{_windowed(lines, originals)}\n\n"
            f"注意：如果你认为判定所需的关键代码在片段之外，"
            f"按 still_exists = true 处理（保守），并在 reason 里说明。"
        )
    user = (
        f"文件：{relpath}\n\n"
        f"【原始漏洞清单】\n{json.dumps(brief, ensure_ascii=False, indent=2)}\n\n"
        f"{code_part}"
    )
    return chat_structured(
        client,
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        FileCheckResult,
        temperature=0.1,
        # 判定 JSON 很小（每条几十 token），1024 足够；
        # 省下的额度全留给代码材料——8192 是固定总预算，这里省就是那里赚
        max_tokens=1024,
    )


def verify_fixes(run_dir: str | Path, copy_root: str | Path, client,
                 state, bus=None) -> dict:
    """复查主入口。返回汇总 dict，写 verification.md，更新 opt_state。

    参数 state 是 OptState：里面必须已经有 hash_before（建副本时记录）。
    """
    run_dir = Path(run_dir)
    copy_root = Path(copy_root)

    # ---------- 第 1 步：哈希对比，算出"动了哪些文件" ----------
    after = hash_tree(copy_root)
    state.record_hashes("hash_after", after)
    before = {rel: rec["hash_before"]
              for rel, rec in state.data["files"].items()
              if "hash_before" in rec}
    diff = diff_hashes(before, after)

    # ---------- 第 2 步：装载原始漏洞清单，按文件分组 ----------
    original = [Finding(**d) for d in json.loads(
        (run_dir / "findings.json").read_text(encoding="utf-8"))]
    orig_by_file: dict[str, list[Finding]] = {}
    for f in original:
        orig_by_file.setdefault(f.file_path, []).append(f)

    summary: dict = {
        "changed": diff["changed"],
        "added": diff["added"],
        "deleted": diff["deleted"],
        "verified": [], "remaining": [], "failed": [],
        "new_findings": [],      # 修复引入的新问题（dict 形态，只进报告）
        "polluted": [],          # 原项目被越界改动的文件（看门狗，见下）
    }
    report: list[str] = [
        "# 修复验证报告（定向复查）",
        "",
        f"- 增量复查：只重读了 {len(diff['changed'])} 个内容变化的文件，"
        f"未动的 {len(before) - len(diff['changed'])} 个文件零消耗",
        f"- 高危信号：被删除文件 {len(diff['deleted'])} 个，"
        f"新增文件 {len(diff['added'])} 个",
        "",
    ]

    # ---------- 原项目完整性看门狗（零 token，纯哈希） ----------
    # opencode 越界事件的教训：修改器（尤其是编程 agent）可能跑出副本、
    # 直接改原项目——副本机制只保证"我们不主动写原项目"，
    # 挡不住别人替我们写。所以每轮复查把原项目重新哈希一遍，
    # 和修复前的快照逐文件比对：原项目应该是一个字节都没动的。
    # 哈希零 token——把铁律从"信任"变成"验证"。
    target_root = state.data.get("target_root")
    if target_root and Path(target_root).exists():
        target_now = hash_tree(target_root)
        for rel, h_before in before.items():
            h_now = target_now.get(rel)
            if h_now is None:
                summary["polluted"].append(f"{rel}（原文件被删除）")
            elif h_now != h_before:
                summary["polluted"].append(rel)
        if summary["polluted"]:
            report.append("## 🚨 原项目完整性警报：修复期间以下文件被改动！")
            report.append("")
            report.append("铁律是“所有修改只写副本”，但这些原项目文件和修复前")
            report.append("的哈希不一致——修改器越界了。请从副本或版本控制恢复：")
            report += [f"- {rel}" for rel in summary["polluted"]]
            report.append("")

    # ---------- 第 3 步：高危信号——文件被删 ----------
    for rel in diff["deleted"]:
        report.append(f"## ⛔ {rel}：文件被修改器删除！")
        for f in orig_by_file.get(rel, []):
            state.set_finding_status(f.id, "failed", "文件被修改器删除")
            summary["failed"].append(f.id)
            report.append(f"- {f.id} [{f.severity}] {f.title}：改砸了")
        report.append("")

    if diff["added"]:
        report.append("## ⚠️ 修改器新建了这些文件（请人工确认是否合理）")
        report += [f"- {rel}" for rel in diff["added"]]
        report.append("")

    # ---------- 第 4 步：逐个"修改过"的文件定向复查 ----------
    # 多语言说明：本循环的全部机制（哈希对比、diff、窗口切片、看门狗）
    # 都是纯文本操作，天然语言无关——曾经这里有个 ".py 才复查"的闸门，
    # 那是 Python 独家时代的遗迹，多语言支持后已拆除。
    for rel in diff["changed"]:
        originals = orig_by_file.get(rel, [])

        new_content = (copy_root / rel).read_text(
            encoding="utf-8", errors="replace")

        # 修复前内容 = 原项目对应文件：副本拷自原项目、修复只动副本，
        # 看门狗已确认原项目没被碰——它就是最权威的"修复前快照"。
        # 读不到（原项目路径缺失等）就保持 None，退回无 diff 材料。
        old_content = None
        if target_root:
            old_file = Path(target_root) / rel
            if old_file.is_file():
                old_content = old_file.read_text(
                    encoding="utf-8", errors="replace")
        # 有旧内容才算 diff；算出来是空串（太大/没变化）则退回全文/窗口。
        # 注意变量名用 diff_text 而不是 diff——本函数外层已有一个
        # diff = diff_hashes(...)（哈希对比结果 dict），同名会把它遮蔽掉，
        # 后面 diff["changed"] 就会对字符串下标，TypeError（测试抓到过）
        diff_text = _diff_text(old_content, new_content, rel) \
            if old_content is not None else ""

        # 语法检查交给 compile()（确定性，零 token）——但只对 Python 生效：
        # compile() 是标准库能力，其他语言没有等价物。非 Python 跳过这道闸，
        # "改没改坏"交给对质环节判断（diff 材料里新旧形态一目了然）。
        # 更严的保障是"构建验证"层的事（比如 .NET 项目跑 dotnet build、
        # 前端项目跑 tsc）——那是另一层，不是复查员的职责。
        if rel.endswith(".py"):
            try:
                compile(new_content, rel, "exec")
            except SyntaxError as e:
                for f in originals:
                    state.set_finding_status(f.id, "failed", f"修复后语法错误：{e}")
                    summary["failed"].append(f.id)
                report.append(f"## ⛔ {rel}：修复后文件无法解析（{e}），改砸了")
                continue

        if not originals:
            # 文件被改了但它本来没有漏洞记录——修改器顺手改了别的？
            report.append(f"## ❓ {rel}：内容有变化但没有原始漏洞记录，请人工 diff")
            continue

        # 对质：一个文件一次请求。这里故意宽捕获：
        # chat_structured 重试耗尽抛 StructuredOutputError；
        # 请求本身也会抛 APIError（比如窗口合并后仍超出 14B 的 8192 上下文）。
        # 无论哪种，保守处理：这个文件的旧账全部按"没修好"记，绝不判"已修好"
        try:
            result = _check_file(client, rel, new_content, originals,
                                 diff_text=diff_text)
        except Exception as e:
            for f in originals:
                state.set_finding_status(f.id, "remaining",
                                         f"复查异常，按未修好处理（保守）：{type(e).__name__}: {e}")
                summary["remaining"].append(f.id)
            report.append(f"## ❓ {rel}：复查异常（{type(e).__name__}），保守按没修好处理")
            continue

        # 把判定对号入座。模型可能漏判某条、也可能编个不存在的 id——
        # 两种都不信任：漏判的按"没修好"（保守），编造的无视。
        # 注意"无视编造 id"不需要显式过滤：下面遍历的方向是
        # "以旧账为锚"（for old in originals，反查 verdict_by_id.get(old.id)），
        # 模型多编出来的 id 不在 originals 里，根本不会被查到，
        # 天然进不了结果——曾有人想加 known_ids 白名单，其实是死代码
        verdict_by_id = {v.finding_id: v for v in result.verdicts}
        # 报告标注这次判定是看着什么材料做的，复盘误判时好追查材料问题
        if diff_text:
            mode_tag = "（diff 对质）"
        elif len(new_content.splitlines()) > VERIFY_FULL_FILE_LINES:
            mode_tag = "（大文件，窗口对质）"
        else:
            mode_tag = ""
        report.append(f"## {rel}{mode_tag}")
        for old in originals:
            v = verdict_by_id.get(old.id)
            if v is None:
                state.set_finding_status(old.id, "remaining",
                                         "复查输出缺少该条判定，按未修好处理（保守）")
                summary["remaining"].append(old.id)
                report.append(f"- {old.id} [{old.severity}] {old.title}："
                              f"❓ 复查未判定，按没修好处理")
            elif v.still_exists:
                state.set_finding_status(old.id, "remaining", v.reason)
                summary["remaining"].append(old.id)
                report.append(f"- {old.id} [{old.severity}] {old.title}："
                              f"❌ 仍存在（{v.reason}）")
            else:
                state.set_finding_status(old.id, "verified", v.reason)
                summary["verified"].append(old.id)
                report.append(f"- {old.id} [{old.severity}] {old.title}："
                              f"✅ 已修好（{v.reason}）")

        for issue in result.new_issues:
            summary["new_findings"].append(issue.model_dump())
            report.append(f"- 🆕 修复引入的新问题 [{issue.severity}] {issue.title}"
                          f"（第 {issue.line_start}-{issue.line_end} 行）："
                          f"{issue.description}")
        report.append("")

    # ---------- 第 5 步：声称修了但文件没动的，戳穿它 ----------
    untouched = set(orig_by_file) - set(diff["changed"]) - set(diff["deleted"])
    for rel in sorted(untouched):
        for f in orig_by_file[rel]:
            rec = state.data["findings"].get(f.id, {})
            if rec.get("status") == "fixed":
                state.set_finding_status(f.id, "remaining",
                                         "修改器声称修复，但文件哈希未变化")
                summary["remaining"].append(f.id)
                report.append(f"## 🤥 {rel}")
                report.append(f"- {f.id} [{f.severity}] {f.title}："
                              f"❌ 声称已修但文件根本没动")

    # ---------- 第 6 步：汇总、落盘 ----------
    report.insert(4, f"- 判定：✅ 修好 {len(summary['verified'])} · "
                     f"❌ 没修好 {len(summary['remaining'])} · "
                     f"⛔ 改砸 {len(summary['failed'])} · "
                     f"🆕 新发现 {len(summary['new_findings'])}")

    (run_dir / "verification.md").write_text("\n".join(report), encoding="utf-8")
    state.save(run_dir / "opt_state.json")
    return summary
