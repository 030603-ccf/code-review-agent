"""opt_state.py —— 优化阶段的情景记忆。

一条 Finding 的生命周期（状态机）：
    pending -> prompted -> fixed -> verified
                                 -> remaining（复查发现还在）
                                 -> failed（改砸了）
"""

import json
from pathlib import Path

FINDING_STATUS = {"pending", "prompted", "fixed", "verified", "remaining", "failed"}


class OptState:
    """优化记忆的内存形态；self.data 就是 opt_state.json 的字典形态。"""

    def __init__(self, target_root: str, copy_root: str):
        self.data: dict = {
            "target_root": target_root,
            "copy_root": copy_root,
            "fixer": {},
            "findings": {},
            "files": {},
        }

    def register_findings(self, findings: list) -> None:
        """装载审查结果，全部置为 pending。"""
        for f in findings:
            self.data["findings"][f.id] = {
                "status": "pending",
                "file": f.file_path,
                "severity": f.severity,
                "title": f.title,
            }

    def set_finding_status(self, finding_id: str, status: str, note: str = "") -> None:
        """推进一条漏洞的状态。"""
        if status not in FINDING_STATUS:
            raise ValueError(f"非法状态 {status!r}，只能是 {sorted(FINDING_STATUS)}")
        if finding_id not in self.data["findings"]:
            raise KeyError(f"未知的 finding id：{finding_id!r}，先 register_findings")
        rec = self.data["findings"][finding_id]
        rec["status"] = status
        if note:
            rec["note"] = note

    def findings_by_status(self, status: str) -> list[str]:
        """捞出某个状态的所有 finding id。"""
        return [fid for fid, rec in self.data["findings"].items()
                if rec["status"] == status]

    def record_hashes(self, tag: str, hashes: dict[str, str]) -> None:
        """记录一轮哈希。tag 取 "hash_before" / "hash_after"。"""
        for rel, h in hashes.items():
            self.data["files"].setdefault(rel, {})[tag] = h

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
