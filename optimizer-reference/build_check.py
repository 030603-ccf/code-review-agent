"""build_check.py —— 构建验证层：修完能不能编译，确定性代码说了算。

复查员（verifier）判的是"问题还在不在"——它不管"代码能不能编译"。
模型修完代码，语法对了（compile() 闸门通过），但 import 写错了、
类型不匹配、lint 规则违反了——这些 compile() 抓不到，
需要真正的构建/lint 工具来判。

设计原则（和 verifier 的 compile() 闸门、STRUCT 阶段一脉相承）：
    能确定做的事，绝不交给概率模型。
    ruff check 报的错是 100% 确定的事实——零 token、零幻觉、永不漏判。

在迭代闭环中的位置：
    修复 → 复查（verifier：问题还在不在）→ 构建验证（本模块：能不能编译）
    构建不过 = 修改器改出了新 bug，和"文件被删/语法错误"同级——
    标记为 failed，不迭代（failed 不盲目重试，留给人）。

配置（config.yaml 的 build_check 节）：
    build_check:
      enabled: true
      timeout: 30
      commands:
        py: "ruff check"

    键是文件扩展名（不带点），值是命令字符串。
    命令在副本目录里跑（subprocess cwd=copy_root），
    退出码 0 = 通过，非 0 = 有错误。
    命令不存在（如没装 ruff）= 静默跳过（保险缺席不等于功能该失败）。
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


# 默认配置：只开 Python 的 ruff check（项目 dev 依赖里已经有 ruff）。
# 键是文件扩展名（不带点），值是命令字符串。
# 其他语言需要用户自己配：
#   js: "npx tsc --noEmit"     （需要 tsconfig.json）
#   cs: "dotnet build --nologo"  （需要 .csproj）
DEFAULT_COMMANDS: dict[str, str] = {
    "py": "ruff check",
}
DEFAULT_TIMEOUT = 30


@dataclass
class BuildCheckResult:
    """一次构建验证的结果。

    passed:     所有命令都退出码 0（或没有命令可跑）
    exit_code:  最后一个非零退出码（全通过时 0）
    command:    最后跑的那条命令（给报告/日志用）
    output:     工具的原始输出（报错详情，人看的）
    skipped:    被跳过的命令（工具没装）
    """
    passed: bool = True
    exit_code: int = 0
    command: str = ""
    output: str = ""
    skipped: list[str] = field(default_factory=list)


def run_build_check(copy_root: str | Path,
                    commands: dict[str, str] | None = None,
                    timeout: int = DEFAULT_TIMEOUT) -> BuildCheckResult:
    """在副本目录里跑构建/lint 命令。

    参数：
        copy_root: 副本目录（命令的 cwd）
        commands:  {扩展名: 命令} 映射。None = 用 DEFAULT_COMMANDS
        timeout:   单条命令的超时秒数

    返回 BuildCheckResult。任何一条命令非零退出 = passed=False。
    命令不存在（FileNotFoundError）= 跳过，不算失败。

    为什么在副本里跑而不是原项目：
    副本铁律——所有验证只读副本，原项目零接触。
    构建工具（ruff/tsc/dotnet）只读不写，但 cwd 在副本里
    保证它看到的文件就是修改后的版本。
    """
    copy_root = Path(copy_root)
    if commands is None:
        commands = DEFAULT_COMMANDS

    result = BuildCheckResult()
    outputs: list[str] = []

    for ext, cmd in commands.items():
        # 副本里有这种扩展名的文件才跑——没有 .py 文件就不跑 ruff
        if not _has_files_with_ext(copy_root, ext):
            continue

        try:
            # Windows 的 CreateProcess 不用 env["PATH"] 找可执行文件——
            # 必须先用 shutil.which 解析出完整路径。
            # 搜索范围：当前 PATH + venv 的 Scripts/bin 目录。
            scripts_dir = str(Path(sys.executable).parent)
            search_path = scripts_dir + os.pathsep + os.environ.get("PATH", "")
            parts = cmd.split()
            exe = shutil.which(parts[0], path=search_path)
            if exe is None:
                result.skipped.append(cmd)
                continue
            parts[0] = exe  # 替换为完整路径

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
            # ruff 的错误在 stdout，tsc 的错误在 stderr——两个都收
            out = (proc.stdout or "") + (proc.stderr or "")
            outputs.append(f"$ {cmd}\n{out.strip()}")
            if proc.returncode != 0:
                result.passed = False
        except FileNotFoundError:
            # 命令不存在（没装 ruff / npx / dotnet）：
            # 静默跳过——保险缺席不等于功能该失败（和 copier.py git init 同一哲学）
            result.skipped.append(cmd)
        except subprocess.TimeoutExpired:
            outputs.append(f"$ {cmd}\n（超时 {timeout}s，已终止）")
            result.passed = False
            result.command = cmd
            result.exit_code = -1

    result.output = "\n\n".join(outputs)
    return result


def _has_files_with_ext(root: Path, ext: str) -> bool:
    """root 下（递归）有没有 .ext 文件。

    跳过 .git / __pycache__ / node_modules 等噪声目录——
    和 copier.py 的 IGNORE_DIRS 同一套口径。
    """
    ignore = {".git", "__pycache__", "node_modules", ".venv", "venv"}
    for p in root.rglob(f"*.{ext}"):
        # 路径中任何一级在忽略名单里就跳过
        if ignore & set(p.relative_to(root).parts[:-1]):
            continue
        return True
    return False
