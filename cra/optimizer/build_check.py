"""build_check.py —— 构建验证层：修完能不能编译，确定性代码说了算。

复查员判"问题还在不在"，构建验证判"能不能编译"。
ruff check / tsc / dotnet build 报的错是 100% 确定的事实——零 token、零幻觉。
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_COMMANDS: dict[str, str] = {
    "py": "ruff check",
}
DEFAULT_TIMEOUT = 30


@dataclass
class BuildCheckResult:
    passed: bool = True
    exit_code: int = 0
    command: str = ""
    output: str = ""
    skipped: list[str] = field(default_factory=list)


def run_build_check(copy_root,
                    commands: dict[str, str] | None = None,
                    timeout: int = DEFAULT_TIMEOUT) -> BuildCheckResult:
    """在副本目录里跑构建/lint 命令。"""
    copy_root = Path(copy_root)
    if commands is None:
        commands = DEFAULT_COMMANDS

    result = BuildCheckResult()
    outputs: list[str] = []

    for ext, cmd in commands.items():
        if not _has_files_with_ext(copy_root, ext):
            continue

        try:
            scripts_dir = str(Path(sys.executable).parent)
            search_path = scripts_dir + os.pathsep + os.environ.get("PATH", "")
            parts = cmd.split()
            exe = shutil.which(parts[0], path=search_path)
            if exe is None:
                result.skipped.append(cmd)
                continue
            parts[0] = exe

            proc = subprocess.run(
                parts,
                cwd=str(copy_root),
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
            result.command = cmd
            result.exit_code = proc.returncode
            out = (proc.stdout or "") + (proc.stderr or "")
            outputs.append(f"$ {cmd}\n{out.strip()}")
            if proc.returncode != 0:
                result.passed = False
        except FileNotFoundError:
            result.skipped.append(cmd)
        except subprocess.TimeoutExpired:
            outputs.append(f"$ {cmd}\n（超时 {timeout}s，已终止）")
            result.passed = False
            result.command = cmd
            result.exit_code = -1

    result.output = "\n\n".join(outputs)
    return result


def _has_files_with_ext(root: Path, ext: str) -> bool:
    """root 下（递归）有没有 .ext 文件。"""
    ignore = {".git", "__pycache__", "node_modules", ".venv", "venv"}
    for p in root.rglob(f"*.{ext}"):
        if ignore & set(p.relative_to(root).parts[:-1]):
            continue
        return True
    return False
