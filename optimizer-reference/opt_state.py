"""opt_state.json —— 优化阶段的"情景记忆"（回答你 19:30 提的那个问题）。

和 RunState 的分工：
    RunState  记"审查流水线跑到哪了"（阶段、文件进度、token）
    OptState  记"每条漏洞的修复命运"和"每个文件的哈希快照"

一条 Finding 的生命周期（状态机）：

    pending   待处理
      -> prompted   已生成修复提示词（PromptBuilder 写过 prompt 文件）
      -> fixed      修改器声称改完了（注意：只是"声称"，还没验证）
      -> verified   复查确认修好了
      -> remaining  复查发现老问题还在
      -> failed     修改器报错或复查发现改出新问题

有了这个 JSON，任何时候都能回答三个问题：
1. 哪些漏洞修了、哪些没修、哪些改砸了？  -> findings 字段
2. 哪些文件被动过、Verifier 要复查谁？    -> files 里的哈希对比
3. 这次修复用的哪个后端、哪个模型？       -> fixer 字段

为什么状态流转要用"显式状态机"而不是随便写字符串：
状态名拼错（比如 "verifed"）如果静默写进 JSON，后面的统计会漏掉它，
这种 bug 极难发现。所以定义合法状态集合，写入前先校验。
"""

import json
from pathlib import Path

# 合法状态集合。用 set 而不是 list：我们只关心"在不在里面"，O(1) 查找
FINDING_STATUS = {"pending", "prompted", "fixed", "verified", "remaining", "failed"}


class OptState:
    """优化记忆的内存形态；self.data 就是那个 JSON 的字典形态。"""

    def __init__(self, target_root: str, copy_root: str):
        self.data: dict = {
            "target_root": target_root,   # 原项目路径（只读，绝不写）
            "copy_root": copy_root,       # 副本路径（所有修改发生在这里）
            "fixer": {},                  # {"backend": "api"|"opencode", "model": ...}
            "findings": {},               # finding_id -> {status, file, severity, title, note?}
            "files": {},                  # relpath -> {hash_before, hash_after}
        }

    # ---------- 漏洞状态 ----------

    def register_findings(self, findings: list) -> None:
        """流水线开始时装载审查结果，全部置为 pending。

        findings 里是 Finding（pydantic 对象），用 .属性 取值；
        存进 JSON 时只挑修复流程需要的字段，完整信息留在 findings.json 里，
        不在两处复制完整数据（单一事实来源原则）。
        """
        for f in findings:
            self.data["findings"][f.id] = {
                "status": "pending",
                "file": f.file_path,
                "severity": f.severity,
                "title": f.title,
            }

    def set_finding_status(self, finding_id: str, status: str, note: str = "") -> None:
        """推进一条漏洞的状态；note 记录"为什么"（比如失败原因）。"""
        if status not in FINDING_STATUS:
            # 主动报错 > 静默写错：bug 要在离错误最近的地方炸出来
            raise ValueError(f"非法状态 {status!r}，只能是 {sorted(FINDING_STATUS)}")
        if finding_id not in self.data["findings"]:
            raise KeyError(f"未知的 finding id：{finding_id!r}，先 register_findings")
        rec = self.data["findings"][finding_id]
        rec["status"] = status
        if note:
            rec["note"] = note

    def findings_by_status(self, status: str) -> list[str]:
        """捞出某个状态的所有 finding id，如 findings_by_status("pending")。"""
        return [fid for fid, rec in self.data["findings"].items()
                if rec["status"] == status]

    # ---------- 文件哈希快照 ----------

    def record_hashes(self, tag: str, hashes: dict[str, str]) -> None:
        """记录一轮哈希。tag 取 "hash_before" / "hash_after"。

        同一个文件会被记两次（修复前、修复后），setdefault 建壳、
        下标赋值填字段，两次调用合并成一条记录：
            "a.py": {"hash_before": "...", "hash_after": "..."}
        """
        for rel, h in hashes.items():
            self.data["files"].setdefault(rel, {})[tag] = h

    # ---------- 落盘 / 读盘 ----------

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "OptState":
        """从 JSON 恢复——中断续跑、Verifier 作为独立命令运行都靠它。

        cls.__new__(cls) 绕过 __init__ 直接造一个"空壳"实例：
        __init__ 的职责是生成一份全新的空骨架，而 load 马上要用文件里的
        数据整个替换 self.data，空骨架造出来也是浪费，所以跳过它。
        这是 classmethod 做"第二种构造方式"的又一个实例（对比 LLMClient.from_config）。
        """
        obj = cls.__new__(cls)
        obj.data = json.loads(Path(path).read_text(encoding="utf-8"))
        return obj
