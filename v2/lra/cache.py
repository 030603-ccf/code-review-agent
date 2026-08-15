"""Cross-run findings cache keyed by (file, sha1, chunk line range).

scan already computes a per-file sha1; this cache reuses it so that unchanged
files skip the LLM entirely on subsequent runs. The cache is a single JSON file
shared across thread_ids (it lives in runs/), guarded by a lock because
review_chunk nodes run in parallel.
"""

import json
import threading
from pathlib import Path

# 审查 prompt/schema 版本：改 reviewer prompt 或 Finding schema 时 +1，
# 让旧缓存整体失效（否则改完规则，旧 findings 会误导下游）。
CACHE_VERSION = "3"


class FindingCache:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._data: dict = {}
        if self.path and self.path.is_file():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def enabled(self) -> bool:
        return self.path is not None

    @staticmethod
    def _key(relpath: str, sha1: str, line_start: int, line_end: int,
             model: str = "") -> str:
        return (f"{CACHE_VERSION}\x00{model}\x00{relpath}\x00{sha1}"
                f"\x00{line_start}\x00{line_end}")

    def get(self, relpath: str, sha1: str,
            line_start: int, line_end: int, model: str = "") -> list[dict] | None:
        if not self.path:
            return None
        key = self._key(relpath, sha1, line_start, line_end, model)
        with self._lock:
            return self._data.get(key)

    def put(self, relpath: str, sha1: str,
            line_start: int, line_end: int, findings: list[dict],
            model: str = "") -> None:
        if not self.path:
            return
        key = self._key(relpath, sha1, line_start, line_end, model)
        with self._lock:
            self._data[key] = findings
            self._save_locked()

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.path)
