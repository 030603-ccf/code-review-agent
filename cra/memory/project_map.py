"""project_map 的存取与查询 —— RunMemory 的"语义记忆"层。

project_map.json 是整个系统的索引中枢：
- Scanner 生成它
- Reviewer/Orchestrator 查它（哪个文件有哪些符号、在第几行）
- Phase 3 里 Optimizer 修改后会增量更新它（只重扫 hash 变化的文件）
"""

import json
from collections import Counter
from pathlib import Path


def save_project_map(pm: dict, path: str | Path) -> None:
    """索引落盘。ensure_ascii=False 保留中文，indent=2 方便人读。"""
    Path(path).write_text(
        json.dumps(pm, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_project_map(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def find_file(pm: dict, relpath: str) -> dict | None:
    """按相对路径查文件条目，找不到返回 None。"""
    for f in pm["files"]:
        if f["relpath"] == relpath:
            return f
    return None


def brief(pm: dict) -> str:
    """一句话摘要，用于 CLI 进度展示。"""
    total_symbols = sum(len(f["symbols"]) for f in pm["files"])
    total_lines = sum(f["line_count"] for f in pm["files"])
    # 按扩展名统计文件数：多语言项目一眼看清构成（Counter 就是"计数字典"）
    ext_counts = Counter(Path(f["relpath"]).suffix or "(无扩展名)"
                         for f in pm["files"])
    exts = ", ".join(f"{ext}×{n}" for ext, n in sorted(ext_counts.items()))
    return (f"{pm['file_count']} 个文件（{exts}）/ "
            f"{total_lines} 行 / {total_symbols} 个符号")
