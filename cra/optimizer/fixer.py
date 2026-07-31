"""fixer.py —— 修改器：拿着任务书，在副本上动刀。

两个后端，config.yaml 里 fixer.backend 切换：
    api       直接调云端大模型整文件重写
    opencode  派单给本机的 opencode CLI（专业编程 agent）
"""

import re
import shlex
import subprocess
from pathlib import Path

from cra.optimizer.prompt_builder import FixTask
from cra.optimizer.splice import parse_parts, splice

CODE_BLOCK_RE = re.compile(r"```(?i:(?:py|python))?\s*\n(.*?)```", re.DOTALL)

MAX_REWRITE_LINES = 300


def extract_code_block(text: str) -> str | None:
    """从模型回复里提取代码（取最长块）。"""
    blocks = CODE_BLOCK_RE.findall(text)
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


class ApiFixer:
    """api 后端：云端大模型整文件重写 / 外科缝合。"""

    SYSTEM = (
        "你是资深 Python 工程师。用户会给你一份修复任务书（含问题清单和源文件全文）。"
        "严格按任务书修复，然后输出修复后的完整文件，放在一个 ```python 代码块里。"
        "禁止输出任何解释、禁止修改与任务书无关的代码、禁止更改公共接口。"
    )

    SYSTEM_SURGICAL = (
        "你是资深 Python 工程师。用户会给你一份修复任务书（含问题清单和源文件全文）。"
        "严格按任务书修复，但【不要输出完整文件】。"
        "只输出需要修改的顶层函数/类的【完整新版本】，每个放在一个 ```python 代码块里；"
        "如果需要新增 import，单独用一个代码块、只写 import 行。"
        "禁止修改函数名和类名，禁止输出模块级调用（print、赋值等），"
        "禁止输出任何解释，未涉及修改的代码一个字都不要输出。"
    )

    def __init__(self, client, copy_root, state=None):
        self.client = client
        self.copy_root = Path(copy_root)
        self.state = state

    def apply(self, task: FixTask) -> bool:
        """修一个文件。写回成功返回 True；失败返回 False。"""
        target = self.copy_root / task.file_path
        original = target.read_text(encoding="utf-8", errors="replace")
        surgical = len(original.splitlines()) > MAX_REWRITE_LINES
        try:
            reply = self.client.chat(
                [
                    {"role": "system",
                     "content": self.SYSTEM_SURGICAL if surgical else self.SYSTEM},
                    {"role": "user", "content": task.prompt_text},
                ],
                temperature=0.1,
                max_tokens=8192,
            )
        except Exception as e:
            return self._fail(task, f"API 调用失败：{type(e).__name__}: {e}")

        if surgical:
            code = self._surgical(task, original, reply)
            if code is None:
                return False
        else:
            code = extract_code_block(reply)
            if code is None:
                hint = ("回复疑似被 max_tokens 截断"
                        if "```" in reply else "回复里没有代码块")
                self._log_reply(task, reply)
                return self._fail(task, hint)

        if not code.strip():
            self._log_reply(task, reply)
            return self._fail(task, "生成的代码是空的")
        if not python_compiles(code, task.file_path):
            self._log_reply(task, reply)
            return self._fail(task, "生成的代码未通过语法校验")

        target.write_text(code, encoding="utf-8")
        self._mark(task, "fixed")
        return True

    def _surgical(self, task: FixTask, original: str, reply: str) -> str | None:
        """外科模式：从回复里抠零件 -> AST 缝合。"""
        blocks = CODE_BLOCK_RE.findall(reply)
        if not blocks:
            self._log_reply(task, reply)
            self._fail(task, "外科模式：回复里没有代码块")
            return None
        try:
            parts: list = []
            for b in blocks:
                parts.extend(parse_parts(b.strip()))
            if not parts:
                raise ValueError("代码块里没有可用的函数/类/import 零件")
            return splice(original, parts)
        except (SyntaxError, ValueError, KeyError) as e:
            self._log_reply(task, reply)
            self._fail(task, f"外科缝合失败：{e}")
            return None

    def _fail(self, task: FixTask, note: str) -> bool:
        self._mark(task, "failed", note)
        return False

    def _log_reply(self, task: FixTask, reply: str) -> None:
        if task.prompt_file:
            p = Path(task.prompt_file.replace(".task.md", ".reply.md"))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(reply, encoding="utf-8")

    def _mark(self, task: FixTask, status: str, note: str = "") -> None:
        if self.state is not None:
            for f in task.findings:
                self.state.set_finding_status(f.id, status, note)


class OpencodeFixer:
    """opencode 后端：把任务书派给本机安装的 opencode CLI。"""

    def __init__(self, copy_root, cmd: str = "opencode run",
                 timeout: int = 600, state=None):
        self.copy_root = Path(copy_root)
        self.cmd = cmd
        self.timeout = timeout
        self.state = state

    def apply(self, task: FixTask) -> bool:
        abs_file = (self.copy_root / task.file_path).resolve().as_posix()
        dispatch = (f"【非交互修复任务】修复任务书见附件。"
                    f"严格按任务书修复 {abs_file}。"
                    f"这个文件的绝对路径已经给你了，就在当前目录下，不要去其他地方找；"
                    f"不要修改当前目录以外的任何文件，不要新建文件，"
                    f"完成后不要输出解释。")
        prompt_ref = Path(task.prompt_file).as_posix()
        argv = shlex.split(self.cmd) + [dispatch, "-f", prompt_ref]
        try:
            proc = subprocess.run(
                argv,
                cwd=self.copy_root,
                timeout=self.timeout,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            return self._fail(task, f"找不到命令 {argv[0]!r}：opencode 未安装或不在 PATH")
        except subprocess.TimeoutExpired:
            return self._fail(task, f"超过 {self.timeout}s 未完成")

        if proc.returncode != 0:
            return self._fail(task, f"退出码 {proc.returncode}：{proc.stderr[-500:]}")
        self._mark(task, "fixed")
        return True

    def _fail(self, task: FixTask, note: str) -> bool:
        if self.state is not None:
            for f in task.findings:
                self.state.set_finding_status(f.id, "failed", note)
        return False

    def _mark(self, task: FixTask, status: str) -> None:
        if self.state is not None:
            for f in task.findings:
                self.state.set_finding_status(f.id, status)


def make_fixer(backend: str, copy_root, state=None,
               client=None, cmd: str = "opencode run", timeout: int = 600):
    """工厂函数：按配置造出对应后端。"""
    if backend == "api":
        if client is None:
            raise ValueError("api 后端必须传入 client")
        return ApiFixer(client, copy_root, state)
    if backend == "opencode":
        return OpencodeFixer(copy_root, cmd=cmd, timeout=timeout, state=state)
    raise ValueError(f"未知后端 {backend!r}：只能是 api / opencode")
