"""fixer.py — 修改器：拿着任务书在副本上动刀。

两个后端（make_fixer 工厂按 backend 切换）：
    api       调 LLM 整文件重写：提取 ```python 代码块 + compile() 语法闸门
    opencode  派单给本机 opencode CLI（subprocess，超时/非零退出 = 失败）

compile 语法闸门只对 .py 生效，其他语言跳过。
"""

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

CODE_BLOCK_RE = re.compile(r"```(?i:(?:py|python))\s*\n(.*?)```", re.DOTALL)
ANY_BLOCK_RE = re.compile(r"```[a-zA-Z0-9_+.-]*\s*\n(.*?)```", re.DOTALL)


@dataclass
class FixTask:
    """一个文件的修复任务：文件 + 关联 findings + 任务书全文（+ 落盘路径）。"""

    file_path: str
    findings: list
    prompt_text: str
    prompt_file: str = ""


def extract_code_block(text: str) -> str | None:
    """从模型回复提取代码块：优先 ```python，退化到任意语言围栏，取最长块。"""
    blocks = CODE_BLOCK_RE.findall(text) or ANY_BLOCK_RE.findall(text)
    if not blocks:
        return None
    return max(blocks, key=len).strip() + "\n"


def python_compiles(code: str, filename: str = "<fix>") -> bool:
    """语法校验：compile() 只编译不执行。"""
    try:
        compile(code, filename, "exec")
        return True
    except SyntaxError:
        return False


def _lang_of(file_path: str) -> str:
    return file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""


class ApiFixer:
    """api 后端：云端 LLM 整文件重写。"""

    backend = "api"
    SYSTEM = (
        "你是资深工程师。用户会给你一份修复任务书（含问题清单和源文件全文）。"
        "严格按任务书修复，然后输出修复后的完整文件，放在一个 ```python 代码块里"
        "（非 Python 文件用对应语言的围栏）。"
        "禁止输出任何解释、禁止修改与任务书无关的代码、禁止更改公共接口。"
    )

    def __init__(self, client, copy_root, state=None, model: str = ""):
        self.client = client
        self.copy_root = Path(copy_root)
        self.state = state
        cfg_model = getattr(getattr(client, "config", None), "model", "")
        self.model = model or cfg_model or ""

    def apply(self, task: FixTask) -> bool:
        """修一个文件；写回副本成功返回 True，失败返回 False（并标记 failed）。"""
        target = self.copy_root / task.file_path
        if not target.is_file():
            return self._fail(task, f"副本里没有这个文件：{task.file_path}")
        try:
            reply = self.client.chat(
                [{"role": "system", "content": self.SYSTEM},
                 {"role": "user", "content": task.prompt_text}],
                temperature=0.1,
                max_tokens=8192,
            )
        except Exception as e:
            return self._fail(task, f"API 调用失败：{type(e).__name__}: {e}")

        code = extract_code_block(reply)
        if code is None:
            return self._fail(task, "回复里没有代码块")
        if not code.strip():
            return self._fail(task, "生成的代码为空")

        # compile 语法闸门只对 .py 生效，其他语言跳过
        if _lang_of(task.file_path) == "py" and not python_compiles(code, task.file_path):
            return self._fail(task, "生成的代码未通过语法校验")

        target.write_text(code, encoding="utf-8")
        self._mark(task, "fixed")
        return True

    def _fail(self, task: FixTask, note: str) -> bool:
        self._mark(task, "failed", note)
        return False

    def _mark(self, task: FixTask, status: str, note: str = "") -> None:
        if self.state is not None:
            for f in task.findings:
                self.state.set_finding_status(f.id, status, note)


class OpencodeFixer:
    """opencode 后端：把任务书派给本机 opencode CLI（subprocess.run）。"""

    backend = "opencode"

    def __init__(self, copy_root, cmd: str = "opencode run",
                 timeout: int = 600, state=None, model: str = ""):
        self.copy_root = Path(copy_root)
        self.cmd = cmd
        self.timeout = timeout
        self.state = state
        self.model = model

    def apply(self, task: FixTask) -> bool:
        abs_file = (self.copy_root / task.file_path).resolve().as_posix()
        dispatch = (
            f"【非交互修复任务】严格按任务书修复 {abs_file}。"
            f"这个文件的绝对路径已经给你了，就在当前目录下，不要去其他地方找；"
            f"不要修改当前目录以外的任何文件，不要新建文件，完成后不要输出解释。"
        )
        argv = shlex.split(self.cmd) + [dispatch]
        if task.prompt_file:
            argv += ["-f", str(Path(task.prompt_file).resolve())]
        try:
            proc = subprocess.run(
                argv,
                cwd=str(self.copy_root),
                timeout=self.timeout,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            return self._fail(task, f"找不到命令 {argv[0]!r}：opencode 未安装或不在 PATH")
        except subprocess.TimeoutExpired:
            return self._fail(task, f"超过 {self.timeout}s 未完成")

        if proc.returncode != 0:
            return self._fail(task, f"退出码 {proc.returncode}：{(proc.stderr or '')[-500:]}")
        self._mark(task, "fixed")
        return True

    def _fail(self, task: FixTask, note: str) -> bool:
        self._mark(task, "failed", note)
        return False

    def _mark(self, task: FixTask, status: str, note: str = "") -> None:
        if self.state is not None:
            for f in task.findings:
                self.state.set_finding_status(f.id, status, note)


def make_fixer(backend: str, copy_root, state=None,
               client=None, cmd: str = "opencode run",
               timeout: int = 600, model: str = ""):
    """工厂函数：按 backend 造出对应修改器。"""
    if backend == "api":
        if client is None:
            raise ValueError("api 后端必须传入 client")
        return ApiFixer(client, copy_root, state, model=model)
    if backend == "opencode":
        return OpencodeFixer(copy_root, cmd=cmd, timeout=timeout,
                             state=state, model=model)
    raise ValueError(f"未知后端 {backend!r}：只能是 api / opencode")
