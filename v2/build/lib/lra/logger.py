"""Structured node logging — one JSON line per event, human- and machine-readable."""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path


class NodeLogger:
    """Per-node logger. Emits start / done / skip / fail events as JSON lines.

    Timing uses time.monotonic(), immune to wall-clock adjustments.
    """

    def __init__(self, run_dir: str | Path, node: str):
        self.run_dir = str(run_dir)
        self.node = node
        self._start_ts: float | None = None
        self._py_logger = logging.getLogger(f"lra.{node}")

    def _emit(self, event: str, message: str, data: dict | None = None) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "node": self.node,
            "event": event,
            "message": message,
            "data": data or {},
        }
        print(json.dumps(record, ensure_ascii=False), flush=True)
        self._py_logger.info("%s | %s | %s", event, message,
                             json.dumps(data or {}, ensure_ascii=False))

    def start(self, **data) -> None:
        self._start_ts = time.monotonic()
        self._emit("start", f"[{self.node}] 开始", data)

    def done(self, **data) -> None:
        elapsed = (time.monotonic() - self._start_ts) if self._start_ts else 0.0
        data["elapsed_sec"] = round(elapsed, 3)
        self._emit("done", f"[{self.node}] 完成 ({elapsed:.1f}s)", data)

    def skip(self, message: str, **data) -> None:
        self._emit("skip", message, data)

    def fail(self, message: str, **data) -> None:
        self._emit("fail", message, data)
