"""lra/logger.py —— NodeLogger：结构化日志与节点计时。

为什么有它：
    原版用 print + EventBus 记进度；这个重写版聚焦编排层，进度直接 print。
    这里把 print 升级成**结构化的 JSON 行**——每行一个事件，机器能解析、
    人能读懂，将来想接回 EventBus（写 events.jsonl）时只需把 _emit 的
    输出目标换一下。

输出格式（控制台一行一个 JSON）：
    {"ts": "2026-07-31T18:00:00+08:00", "node": "scan", "event": "done",
     "message": "[scan] 完成 (0.1s)", "data": {"files": 23}}

字段约定：
    ts        ISO 8601 时间戳
    node      节点名（scan / chunk / review_chunk / aggregate / second_review / report）
    event     事件类型（start / done / skip / fail）
    message   人类可读描述
    data      结构化附数据（文件数、token 数、耗时等）

计时用什么：
    time.monotonic()——不受系统时间调整影响（改系统时间不会把耗时算歪），
    比 time.time() 更适合做"跑了多久"的测量。
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path


class NodeLogger:
    """每个节点实例化一个，持有 run_dir（产物目录）和 node 名。

    用法：
        logger = NodeLogger(state["run_dir"], "scan")
        logger.start()                       # 打点 + 输出 start 事件
        ... 干活 ...
        logger.done(files=23)                # 输出 done 事件（含 elapsed_sec）
    """

    def __init__(self, run_dir: str | Path, node: str):
        self.run_dir = str(run_dir)
        self.node = node
        self._start_ts: float | None = None
        # 标准库 logger 也走一份——将来加 FileHandler 写 events.jsonl 时
        # 在 lra 包初始化处统一配 handler 即可，节点代码不用动
        self._py_logger = logging.getLogger(f"lra.{node}")

    def _emit(self, event: str, message: str, data: dict | None = None) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "node": self.node,
            "event": event,
            "message": message,
            "data": data or {},
        }
        # 控制台：紧凑 JSON 行（flush=True 保证进程被杀时最后一行不丢）
        print(json.dumps(record, ensure_ascii=False), flush=True)
        # 标准库 logging 同步走一份（多路输出的挂载点）
        self._py_logger.info(
            "%s | %s | %s", event, message,
            json.dumps(data or {}, ensure_ascii=False),
        )

    def start(self, **data) -> None:
        """打点计时并输出 start 事件。之后的 done() 自动算 elapsed_sec。"""
        self._start_ts = time.monotonic()
        self._emit("start", f"[{self.node}] 开始", data)

    def done(self, **data) -> None:
        """输出 done 事件，自动带上从 start() 到现在的耗时。"""
        elapsed = (time.monotonic() - self._start_ts) if self._start_ts else 0.0
        data["elapsed_sec"] = round(elapsed, 3)
        self._emit("done", f"[{self.node}] 完成 ({elapsed:.1f}s)", data)

    def skip(self, message: str, **data) -> None:
        """输出 skip 事件（跳过某文件/某块时用）。"""
        self._emit("skip", message, data)

    def fail(self, message: str, **data) -> None:
        """输出 fail 事件（节点内部失败但不想中断全图时用）。"""
        self._emit("fail", message, data)
