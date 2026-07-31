"""fixer.py —— 修改器：拿着任务书，在副本上动刀。

Phase 3 的分工哲学（你说"14B 顶多满足审查功能"的落地）：
    本地 14B  -> 审查 + 写任务书（轻活，它干得动）
    修改器    -> 真正动代码（重活，交给更强的模型或专业编程 agent）

两个后端，config.yaml 里 fixer.backend 切换：
    api       直接调云端大模型（如 deepseek-v4-pro）。
              协议：任务书 -> 模型返回修复后的完整文件 -> 解析 -> 语法校验 -> 写回。
    opencode  派单给本机的 opencode CLI（专业编程 agent，会自己读文件、编辑）。
              我们只"派单 + 验收"，修改过程它自治。

无论哪个后端，两道保命闸永远生效：
    1. 全程只写副本目录（copier 的铁律）
    2. api 后端写回前必须通过 Python 语法校验——模型返回一坨跑不起来的东西时，
       宁肯这条漏洞记 failed，也不把副本改坏
"""

import re
import shlex
import subprocess
from pathlib import Path

from cra.optimizer.prompt_builder import FixTask
from cra.optimizer.splice import parse_parts, splice

# 从回复里抠代码块的正则：``` 或 ```py / ```python 开头、``` 收尾。
# re.DOTALL 让 "." 能匹配换行符——代码块必然跨行，没这个标志永远匹配不到。
# (?:py|python)? 是非捕获分组：匹配语言标签但不把它算进捕获结果。
# 协议口径：任务书要求模型输出 ```python，但模型常随手写 ```py 或 ```Python，
# 这三种都应视为"同一张协议的合法写法"。(?i:...) 只做局部大小写不敏感，
# 不影响其余部分。注意【不要】放宽成任意语言标签（如 ```json）——
# extract_code_block 取最长块，任意标签可能把 json 配置块当代码提取出来。
CODE_BLOCK_RE = re.compile(r"```(?i:(?:py|python))?\s*\n(.*?)```", re.DOTALL)

# 整文件重写 vs 外科模式的切换线（真实项目量出来的）：
# 让推理模型全文重写 812 行的文件，它写到约 100 行（约 1000 token 输出）
# 就擅自收工（finish_reason=stop，不是 token 撞顶）——输出越长越不可靠。
# 可靠区大约在 2-3k token 输出以内，按 1 行 ≈ 8-10 token 换算就是 300 行。
# 所以小文件走整文件重写（简单），大文件走外科模式（模型出零件，AST 缝合）。
MAX_REWRITE_LINES = 300


def extract_code_block(text: str) -> str | None:
    """从模型回复里提取代码。

    模型不一定老实：可能先解释两句、可能给多个代码块（示例片段+完整文件）。
    策略：所有块里取最长的——完整文件永远比示例片段长。
    一个块都没有就返回 None，让调用方走 failed 流程。
    """
    blocks = CODE_BLOCK_RE.findall(text)
    if not blocks:
        return None
    # max(列表, key=len)：按字符串长度取最大值
    return max(blocks, key=len).strip() + "\n"


def python_compiles(code: str, filename: str = "<fix>") -> bool:
    """语法校验：compile() 只编译不执行。

    这是"代码跑不跑得起来"的第一道筛子：零成本、零风险，
    却能拦住"模型写着写着断了半个函数"这类最常见的翻车。
    注意它只能查语法，查不了逻辑错误——那是 Verifier 的职责。
    """
    try:
        compile(code, filename, "exec")
        return True
    except SyntaxError:
        return False


class ApiFixer:
    """api 后端：云端大模型整文件重写。

    为什么选"整文件重写"而不是"diff 补丁"：
    让模型输出精确到行的补丁（unified diff），格式错一点就应用失败；
    整文件重写只有一种格式要求（放进代码块），简单的东西才可靠。
    代价是 token 多一点——文件不大时这是划算的买卖。
    """

    # 修改器的"人设"：和任务书（user 消息）分工，system 管输出格式和纪律
    SYSTEM = (
        "你是资深 Python 工程师。用户会给你一份修复任务书（含问题清单和源文件全文）。"
        "严格按任务书修复，然后输出修复后的完整文件，放在一个 ```python 代码块里。"
        "禁止输出任何解释、禁止修改与任务书无关的代码、禁止更改公共接口。"
    )

    # 外科模式（大文件）的人设：零件协议——只输出要改的，不许输出全文
    SYSTEM_SURGICAL = (
        "你是资深 Python 工程师。用户会给你一份修复任务书（含问题清单和源文件全文）。"
        "严格按任务书修复，但【不要输出完整文件】。"
        "只输出需要修改的顶层函数/类的【完整新版本】，每个放在一个 ```python 代码块里；"
        "如果需要新增 import，单独用一个代码块、只写 import 行。"
        "禁止修改函数名和类名，禁止输出模块级调用（print、赋值等），"
        "禁止输出任何解释，未涉及修改的代码一个字都不要输出。"
    )

    def __init__(self, client, copy_root: str | Path, state=None):
        self.client = client
        self.copy_root = Path(copy_root)
        self.state = state

    def apply(self, task: FixTask) -> bool:
        """修一个文件。写回成功返回 True；任何一步不达标记 failed 返回 False。"""
        target = self.copy_root / task.file_path
        original = target.read_text(encoding="utf-8", errors="replace")
        # 模式选择：小文件整文件重写（简单可靠）；
        # 大文件外科模式——模型只输出要改的函数/类，缝合交给 AST
        surgical = len(original.splitlines()) > MAX_REWRITE_LINES
        try:
            reply = self.client.chat(
                [
                    {"role": "system",
                     "content": self.SYSTEM_SURGICAL if surgical else self.SYSTEM},
                    {"role": "user", "content": task.prompt_text},
                ],
                temperature=0.1,     # 改代码要比审查更"死板"，越少发挥越好
                max_tokens=8192,     # 整文件重写时输出可能很长
            )
        except Exception as e:
            # 故意宽捕获：一个文件修失败不该拖垮整条流水线，
            # 记下来继续修下一个——编排层的韧性原则在这里同样适用
            return self._fail(task, f"API 调用失败：{type(e).__name__}: {e}")

        if surgical:
            # _surgical 失败时已在内部记 failed + 留档，这里只管收 None
            code = self._surgical(task, original, reply)
            if code is None:
                return False
        else:
            code = extract_code_block(reply)
            if code is None:
                # 区分两种"没有代码块"：有围栏开头 = 写到一半被截断；
                # 完全没有围栏 = 模型没按约定输出
                hint = ("回复疑似被 max_tokens 截断（文件过大或输出上限不足）"
                        if "```" in reply else "回复里没有代码块")
                self._log_reply(task, reply)
                return self._fail(task, hint)

        if not code.strip():
            # 空模块能通过 compile() 的语法校验（Python 里空文件合法），
            # 但"把文件重写成空白"绝不可能是合法修复——必须单独拦
            self._log_reply(task, reply)
            return self._fail(task, "生成的代码是空的")
        if not python_compiles(code, task.file_path):
            self._log_reply(task, reply)
            return self._fail(task, "生成的代码未通过语法校验")

        target.write_text(code, encoding="utf-8")
        self._mark(task, "fixed")
        return True

    def _surgical(self, task: FixTask, original: str, reply: str) -> str | None:
        """外科模式：从回复里抠零件 -> AST 缝合。失败记 failed 并留档。"""
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
            # 三种典型翻车：零件本身语法错 / 零件里有模块级调用 /
            # 零件名字在原文件找不到（模型幻觉或擅自改名）
            self._log_reply(task, reply)
            self._fail(task, f"外科缝合失败：{e}")
            return None

    def _fail(self, task: FixTask, note: str) -> bool:
        self._mark(task, "failed", note)
        return False

    def _log_reply(self, task: FixTask, reply: str) -> None:
        """失败时把模型的原始回复存到任务书旁边（.reply.md）。

        "没有代码块""语法错误"这种失败，不看原始输出永远猜不出原因：
        是截断了？模型在解释？还是把代码写成了散文？留档才能复盘。
        """
        if task.prompt_file:
            p = Path(task.prompt_file.replace(".task.md", ".reply.md"))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(reply, encoding="utf-8")

    def _mark(self, task: FixTask, status: str, note: str = "") -> None:
        # 同一文件的所有漏洞同生共死：文件没改成，它们一起 failed
        if self.state is not None:
            for f in task.findings:
                self.state.set_finding_status(f.id, status, note)


class OpencodeFixer:
    """opencode 后端：把任务书派给本机安装的 opencode CLI。

    注意传给 opencode 的是任务书的【文件路径】而不是全文：
    1. Windows 命令行有长度上限（约 32k 字符），长任务书传参数会炸
    2. opencode 本身就是编程 agent，"先读这个文件再照做"是它的本能
    """

    def __init__(self, copy_root: str | Path, cmd: str = "opencode run",
                 timeout: int = 600, state=None):
        self.copy_root = Path(copy_root)
        self.cmd = cmd
        self.timeout = timeout
        self.state = state

    def apply(self, task: FixTask) -> bool:
        # shlex.split 把 cmd 拆成参数列表（posix 规则：反斜杠会被当转义符，
        # 所以 config 里的路径一律用正斜杠）
        #
        # argv 顺序是教训换来的：-f/--file 是"数组型"参数，会贪婪吞掉
        # 后面所有词——dispatch 放它后面会被当成文件名（报 File not found）。
        # 所以：消息在前，-f 文件在最后（它后面没有别的词，没得吞）。
        # 路径给绝对路径（resolve 消掉任何歧义）：
        # 越界事件的另一教训——"当前目录下的 X"这种相对说法，
        # agent 没找到时会自己去"推理"文件在哪，一推理就推理到副本外面。
        # 绝对路径 + "不要找"双保险：坐标钉死，不给推理留空间。
        abs_file = (self.copy_root / task.file_path).resolve().as_posix()
        dispatch = (f"【非交互修复任务】修复任务书见附件。"
                    f"严格按任务书修复 {abs_file}。"
                    f"这个文件的绝对路径已经给你了，就在当前目录下，不要去其他地方找；"
                    f"不要修改当前目录以外的任何文件，不要新建文件，"
                    f"完成后不要输出解释。")
        prompt_ref = Path(task.prompt_file).as_posix()   # 路径统一正斜杠
        argv = shlex.split(self.cmd) + [dispatch, "-f", prompt_ref]
        try:
            proc = subprocess.run(
                argv,
                cwd=self.copy_root,          # 工作目录=副本根：它能改的只有副本
                timeout=self.timeout,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            return self._fail(task, f"找不到命令 {argv[0]!r}：opencode 未安装或不在 PATH")
        except subprocess.TimeoutExpired:
            return self._fail(task, f"超过 {self.timeout}s 未完成")

        if proc.returncode != 0:
            # stderr 只留最后 500 字符：够定位问题，又不会把状态文件灌爆
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


def make_fixer(backend: str, copy_root: str | Path, state=None,
               client=None, cmd: str = "opencode run", timeout: int = 600):
    """工厂函数：按配置造出对应后端。

    和 LLMClient.from_config 一个思路：调用方（流水线/CLI）不关心
    后端怎么构造，只认"给我一个能 apply(task) 的东西"。
    """
    if backend == "api":
        if client is None:
            raise ValueError("api 后端必须传入 client（云端模型靠它说话）")
        return ApiFixer(client, copy_root, state)
    if backend == "opencode":
        return OpencodeFixer(copy_root, cmd=cmd, timeout=timeout, state=state)
    raise ValueError(f"未知后端 {backend!r}：只能是 api / opencode")
