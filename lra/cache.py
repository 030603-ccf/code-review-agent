"""Cross-run findings cache keyed by (file, sha1, chunk line range, context).

scan already computes a per-file sha1; this cache reuses it so that unchanged
files skip the LLM entirely on subsequent runs. The `context` dimension is a
fingerprint of every input that affects the reviewer prompt but is not covered
by the file sha1 (issue hint, project rules, mistake notebook) — changing any
of them produces a different key, so the cache never silently serves stale
findings. The cache is a single JSON file shared across thread_ids (it lives in
runs/), guarded by a lock because review_chunk nodes run in parallel.
"""

import json
import os
import threading
import time
import uuid
from pathlib import Path

# 审查 prompt/schema 版本：改 reviewer prompt 或 Finding schema 时 +1，
# 让旧缓存整体失效（否则改完规则，旧 findings 会误导下游）。
CACHE_VERSION = "3"

# 节流落盘间隔（秒）：16 并发下每个 chunk 完成都全量 json.dumps 整个 dict 落盘是
# O(N²) 写放大。改为「内存更新总是立即（加锁），落盘节流：距上次落盘超过该间隔才
# 落盘，否则只置 dirty」，run 结束调用 flush() 兜底。
SAVE_INTERVAL_SEC = 2.0


class FindingCache:
    def __init__(self, path: str | Path | None,
                 save_interval: float = SAVE_INTERVAL_SEC):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._data: dict = {}
        self._dirty = False
        self._last_save = 0.0
        self._save_interval = save_interval
        if self.path and self.path.is_file():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def enabled(self) -> bool:
        return self.path is not None

    @staticmethod
    def _key(relpath: str, sha1: str, line_start: int, line_end: int,
             model: str = "", context: str = "") -> str:
        return (f"{CACHE_VERSION}\x00{model}\x00{relpath}\x00{sha1}"
                f"\x00{line_start}\x00{line_end}\x00{context}")

    def get(self, relpath: str, sha1: str,
            line_start: int, line_end: int, model: str = "",
            context: str = "") -> list[dict] | None:
        if not self.path:
            return None
        key = self._key(relpath, sha1, line_start, line_end, model, context)
        with self._lock:
            return self._data.get(key)

    def put(self, relpath: str, sha1: str,
            line_start: int, line_end: int, findings: list[dict],
            model: str = "", context: str = "") -> None:
        if not self.path:
            return
        key = self._key(relpath, sha1, line_start, line_end, model, context)
        with self._lock:
            self._data[key] = findings
            self._maybe_save_locked()

    def _maybe_save_locked(self) -> None:
        """节流落盘：距上次落盘超过间隔才写盘，否则只置 dirty 等 flush()。"""
        if time.monotonic() - self._last_save >= self._save_interval:
            self._save_locked()
        else:
            self._dirty = True

    def flush(self) -> None:
        """兜底落盘：run 结束/进程退出前调用，把未落盘的 dirty 数据写下去。"""
        if not self.path:
            return
        with self._lock:
            if self._dirty:
                self._save_locked()

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 固定 .tmp 名会让两个并发 run 的 replace 互相竞争（FileNotFoundError
        # 被误判为保存失败）。tmp 名带上 PID + 随机后缀，每个进程/每次落盘
        # 各自写自己的临时文件，replace 是原子的最后一步。
        tmp = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False),
                       encoding="utf-8")
        try:
            tmp.replace(self.path)
        except OSError:
            # tmp 消失 = 并发 run 已抢先 replace 完成；视为已写，静默清理残片。
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        self._dirty = False
        self._last_save = time.monotonic()
