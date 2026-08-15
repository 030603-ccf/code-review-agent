"""opt_state.py — 优化阶段的情景记忆：每条 finding 的修复状态 + 备注。

finding 状态机：
    pending  刚装载（待修）
    fixed    修改器声称已修好（写回副本）
    verified 复查确认问题已消除
    remaining 复查发现问题还在（下一轮再修）
    failed   改砸了 / 语法不过 / API 或 opencode 失败（不再重试）
"""

import json
from pathlib import Path

FINDING_STATUS = {"pending", "fixed", "verified", "remaining", "failed"}


class OptState:
    """内存形态就是 opt_state.json 的字典形态；save/load 负责往返。"""

    def __init__(self, target_root, copy_root):
        self.data: dict = {
            "target_root": str(target_root),
            "copy_root": str(copy_root),
            "findings": {},
        }

    def register_findings(self, findings: list) -> None:
        """装载审查结果，全部置为 pending。"""
        for f in findings:
            self.data["findings"][f.id] = {
                "status": "pending",
                "file": f.file_path,
                "severity": getattr(f, "severity", ""),
                "title": getattr(f, "title", ""),
            }

    def set_finding_status(self, finding_id: str, status: str, note: str = "") -> None:
        """推进一条 finding 的状态，可附带备注。"""
        if status not in FINDING_STATUS:
            raise ValueError(f"非法状态 {status!r}，只能是 {sorted(FINDING_STATUS)}")
        if finding_id not in self.data["findings"]:
            raise KeyError(f"未知 finding id：{finding_id!r}，先 register_findings")
        rec = self.data["findings"][finding_id]
        rec["status"] = status
        if note:
            rec["note"] = note

    def findings_by_status(self, status: str) -> list[str]:
        """捞出某个状态的所有 finding id。"""
        return [fid for fid, rec in self.data["findings"].items()
                if rec["status"] == status]

    def save(self, path) -> None:
        Path(path).write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path) -> "OptState":
        """从 JSON 恢复。"""
        obj = cls.__new__(cls)
        obj.data = json.loads(Path(path).read_text(encoding="utf-8"))
        return obj
