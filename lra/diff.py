"""lra/diff.py —— 增量审查：只审 git diff 变更的文件。

为什么用它：
    日常审查最贵的不是扫描，是 LLM 调用。项目大起来之后，
    "每次都把全部文件审一遍"是浪费——上一次审过没改的代码，
    这次大概率还是没问题的。

    增量模式从 git 里读出变更文件清单，chunk 节点只对它们切块。

    --incremental  只审 <base_ref> 之后的变更 + 未提交的本地修改
    非 git 仓库 / git 不可用  -> 返回空列表（调用方自然退化为全量审查）

用 subprocess 而不是 gitpython：
    依赖最小化——git 命令一个子进程调用就能拿结果，
    不需要为它引入一个包（gitpython 是 cra 的 optional 依赖）。

两类变更要**合并**（缺一不可）：
    1. 已提交的：git diff --name-only <base_ref> HEAD
       —— base_ref 之前到最近一次提交之间，别人/自己提交的改动
    2. 未提交的：git diff --name-only
       —— 工作区/暂存区里还没 commit 的改动
    只算第 1 类会把"刚改还没提交"的代码漏掉；只算第 2 类会漏掉
    提交过的历史改动。合并取并集才是"相对 base_ref 到底动了什么"。
"""

import subprocess
from pathlib import Path


def changed_files(repo_root: str | Path,
                  base_ref: str = "HEAD~1") -> list[str]:
    """获取 base_ref 之后 + 未提交的变更文件（相对路径清单，已去重排序）。

    两类变更各自尝试获取，失败（非 git 仓库 / 没有 git）就跳过该步；
    两步都失败 -> 返回 []，调用方退化为全量审查。

    返回的相对路径与 project_map 里 entry["relpath"] 同格式（正斜杠）。
    """
    paths: set[str] = set()

    # ---- 第 1 类：base_ref..HEAD 的已提交变更 ----
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only", base_ref, "HEAD"],
            cwd=str(repo_root), text=True,
            stderr=subprocess.DEVNULL, encoding="utf-8",
        )
        paths.update(_parse_lines(out))
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass    # base_ref 不存在 / 没有 git：这一步放弃，不是致命伤

    # ---- 第 2 类：工作区 + 暂存区的未提交变更 ----
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only"],
            cwd=str(repo_root), text=True,
            stderr=subprocess.DEVNULL, encoding="utf-8",
        )
        paths.update(_parse_lines(out))
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    return sorted(paths)


def _parse_lines(out: str) -> list[str]:
    """git 输出的行 -> 相对路径清单（统一正斜杠，去空行）。"""
    return [line.strip().replace("\\", "/")
            for line in out.splitlines() if line.strip()]
