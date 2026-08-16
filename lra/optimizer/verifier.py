"""verifier.py — 复查：对修复后的副本文件重审，判断原 finding 是否还在。

两种模式（loop 的 verify_mode 切换，二选一，不过度设计）：
    llm    用审查 client 逐文件 LLM 对质，逐条判定 still_exists（默认）
    build  跑确定性 lint/构建命令（如 ruff check），通过即视为本轮修复通过

LLM 模式下还带一个零 token 的 Python 语法闸门：副本里的 .py 无法 compile()
直接判 failed，不浪费模型调用。
"""

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from lra.llm.structured import chat_structured

VERIFY_SYSTEM = (
    "你是代码复查员。给你一份文件的原始问题清单和修复后的文件全文。\n"
    "逐条判定每个问题在修复后的代码里是否仍然存在，只依据给出的代码。\n"
    "输出严格 JSON（不要任何解释或 markdown）：\n"
    '{"verdicts": [{"finding_id": "F1", "still_exists": true, "reason": "简短理由"}]}'
)

# 语法错误类 finding 的标题特征：build/lint 只能确定性验证「语法类」问题已消除。
_SYNTAX_TITLE_MARKERS = ("语法", "syntax", "解析失败")


def _build_can_verify(f) -> bool:
    """build/lint 只能确定性验证「语法/风格类」问题已消除；语义类不能。

    security/correctness 的 SQL 注入、逻辑 bug 等 ruff/lint 根本查不出来，
    「没 lint 报错」不等于「修好了」，据此标 verified 就是假复查。只有语法错误类
    （标题含 语法/syntax/解析失败，含 _parse_error_findings 生成的「语法解析失败」
    correctness finding）和 best_practice 等 lint 可覆盖的类才能由 build 通过确认。
    """
    title = (getattr(f, "title", "") or "").lower()
    if any(m in title for m in _SYNTAX_TITLE_MARKERS):
        return True
    return getattr(f, "category", "") not in {"security", "correctness"}


class FindingVerdict(BaseModel):
    finding_id: str
    still_exists: bool
    reason: str = ""


class VerificationResult(BaseModel):
    verdicts: list[FindingVerdict]


@dataclass
class BuildCheckResult:
    passed: bool = True
    command: str = ""
    output: str = ""
    skipped: str = ""


def run_build_check(copy_root, command: str = "ruff check",
                    timeout: int = 60) -> BuildCheckResult:
    """在副本目录跑一条 lint/构建命令（确定性闸门）。命令不存在则 skipped。"""
    copy_root = Path(copy_root)
    result = BuildCheckResult(command=command)
    parts = command.split()
    if not parts:
        result.skipped = "空命令"
        return result
    search_path = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    exe = shutil.which(parts[0], path=search_path)
    if exe is None:
        result.skipped = f"{parts[0]} 不在 PATH，跳过"
        return result
    parts[0] = exe
    try:
        proc = subprocess.run(
            parts, cwd=str(copy_root), capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        result.passed = False
        result.output = f"（超时 {timeout}s，已终止）"
        return result
    result.output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    result.passed = proc.returncode == 0
    return result


def _group_by_file(findings, round_files=None) -> dict[str, list]:
    groups: dict[str, list] = {}
    for f in findings:
        if round_files is not None and f.file_path not in round_files:
            continue
        groups.setdefault(f.file_path, []).append(f)
    return groups


def _check_file(client, file_path: str, new_content: str, originals) -> VerificationResult:
    """对一个文件做 LLM 对质：全部旧问题 + 新代码，逐条判定。"""
    brief = [
        {"finding_id": f.id, "severity": getattr(f, "severity", ""),
         "lines": f"{f.line_start}-{f.line_end}", "title": f.title,
         "description": f.description, "evidence": getattr(f, "evidence", "")}
        for f in originals
    ]
    lang = file_path.rsplit(".", 1)[-1] if "." in file_path else ""
    user = (
        f"文件：{file_path}\n\n"
        f"【原始问题清单】\n{json.dumps(brief, ensure_ascii=False, indent=2)}\n\n"
        f"【修复后的文件全文】\n```{lang}\n{new_content}\n```"
    )
    return chat_structured(
        client,
        [{"role": "system", "content": VERIFY_SYSTEM},
         {"role": "user", "content": user}],
        VerificationResult,
        temperature=0.1,
        max_tokens=2048,
    )


def verify_fixes(run_dir, copy_root, client, state, findings,
                 round_files: set[str] | None = None,
                 mode: str = "llm",
                 build_cmd: str = "ruff check",
                 build_timeout: int = 60) -> dict:
    """复查主入口：更新 opt_state、落盘 verification.md，返回汇总 dict。

    round_files：本轮修改器实际动过的文件集合；None 表示检查全部 finding 文件。
    LLM 模式只重审非 verified 的 finding（已确认修好的不重复花 token）。
    """
    run_dir = Path(run_dir)
    copy_root = Path(copy_root)
    run_dir.mkdir(parents=True, exist_ok=True)
    summary: dict = {"verified": [], "remaining": [], "failed": [], "skipped": []}
    report = ["# 修复验证报告", ""]

    if mode == "build":
        bc = run_build_check(copy_root, command=build_cmd, timeout=build_timeout)
        summary["build_check"] = {
            "passed": bc.passed, "command": bc.command,
            "output": bc.output, "skipped": bc.skipped,
        }
        files = sorted(round_files) if round_files else \
            sorted({f.file_path for f in findings})
        report.append(f"- 模式：确定性 lint（{build_cmd}）→ "
                      f"{'通过 ✅' if bc.passed else '未通过 ❌'}")
        report.append("- ⚠️ build 只能验证语法/风格类问题；security/correctness 等语义"
                      "问题不会被标 verified，需 llm 复查。")
        if bc.skipped:
            report.append(f"- ⚠️ {bc.skipped}")
        for fp in files:
            for f in [x for x in findings if x.file_path == fp]:
                if not bc.passed:
                    state.set_finding_status(f.id, "remaining",
                                             bc.output or "lint 未通过")
                    summary["remaining"].append(f.id)
                    report.append(f"- ❌ {f.id} [{f.severity}] {f.title}：lint 未通过")
                elif _build_can_verify(f):
                    state.set_finding_status(f.id, "verified",
                                             "lint 通过（build 可验证此类）")
                    summary["verified"].append(f.id)
                    report.append(f"- ✅ {f.id} [{f.severity}] {f.title}：lint 通过")
                else:
                    state.set_finding_status(
                        f.id, "remaining",
                        "build 无法验证此类问题，需 llm 复查")
                    summary["remaining"].append(f.id)
                    report.append(f"- ❓ {f.id} [{f.severity}] {f.title}："
                                  "build 无法验证此类问题，需 llm 复查")
    else:
        for file_path, fs in sorted(_group_by_file(findings, round_files).items()):
            # 只重审还没确认修好的 finding
            fs = [f for f in fs
                  if state.data["findings"].get(f.id, {}).get("status") != "verified"]
            if not fs:
                continue
            target = copy_root / file_path
            if not target.is_file():
                for f in fs:
                    state.set_finding_status(f.id, "failed", "副本中文件不存在")
                    summary["failed"].append(f.id)
                report.append(f"- ⛔ {file_path}：副本中文件不存在")
                continue

            new_content = target.read_text(encoding="utf-8", errors="replace")
            # Python 语法闸门（确定性、零 token）
            if file_path.endswith(".py"):
                try:
                    compile(new_content, file_path, "exec")
                except SyntaxError as e:
                    for f in fs:
                        state.set_finding_status(f.id, "failed", f"修复后语法错误：{e}")
                        summary["failed"].append(f.id)
                    report.append(f"- ⛔ {file_path}：修复后语法错误（{e}）")
                    continue

            try:
                result = _check_file(client, file_path, new_content, fs)
            except Exception as e:
                for f in fs:
                    state.set_finding_status(
                        f.id, "remaining",
                        f"复查异常，保守按未修好处理：{type(e).__name__}: {e}")
                    summary["remaining"].append(f.id)
                report.append(f"- ❓ {file_path}：复查异常（{type(e).__name__}）")
                continue

            verdict_by_id = {v.finding_id: v for v in result.verdicts}
            for f in fs:
                v = verdict_by_id.get(f.id)
                if v is None:
                    state.set_finding_status(f.id, "remaining", "复查未判定，保守按未修好")
                    summary["remaining"].append(f.id)
                    report.append(f"- ❌ {f.id} [{f.severity}] {f.title}：复查未判定")
                elif v.still_exists:
                    state.set_finding_status(f.id, "remaining", v.reason)
                    summary["remaining"].append(f.id)
                    report.append(f"- ❌ {f.id} [{f.severity}] {f.title}：仍存在（{v.reason}）")
                else:
                    state.set_finding_status(f.id, "verified", v.reason)
                    summary["verified"].append(f.id)
                    report.append(f"- ✅ {f.id} [{f.severity}] {f.title}：已修好（{v.reason}）")

    state.save(run_dir / "opt_state.json")
    (run_dir / "verification.md").write_text("\n".join(report), encoding="utf-8")
    return summary
