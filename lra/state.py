"""ReviewState — the shared workbench of the graph.

Reducers:
  - findings:      operator.add (parallel reviewers append)
  - failed_blocks: custom — dedupe by (file, line range), drop "resolved"
                   entries so a retried block is never re-run.
  - retry_round:   overwrite (parallel retry nodes all write round+1, so last
                   write wins; overwrite also lets --retry-failed reset to 0)
"""

import operator
from typing import Annotated, TypedDict


def _retry_round_reducer(a: int, b: int) -> int:
    """合并 retry_round：覆盖语义（last-write-wins）。

    并行 retry_failed 节点写的是同一个 ``round+1``（fan_out_failed 给每个节点
    传同一个 round），所以覆盖与 max 在正常跑批里等价；但覆盖额外允许
    ``--retry-failed`` 倒带时显式写回更小的 0 来重置轮次——旧 ``max`` reducer
    会把它卡在 1，倒带永远无法重触发 retry_failed。
    """
    return b


def _block_key(item: dict) -> tuple:
    entry = item.get("entry", {})
    chunk = item.get("chunk", {})
    return (entry.get("relpath", ""),
            chunk.get("line_start", 0), chunk.get("line_end", 0))


def _merge_failed(a: list[dict], b: list[dict]) -> list[dict]:
    """Merge two failed-block ledgers. An item with resolved=True removes its
    identity; otherwise entries are deduplicated by identity."""
    merged: dict[tuple, dict] = {}
    for item in a + b:
        key = _block_key(item)
        if item.get("resolved"):
            merged.pop(key, None)
        else:
            merged[key] = item
    return list(merged.values())


class ReviewState(TypedDict, total=False):
    # CLI-provided
    root: str
    run_dir: str
    diff_files: list[str] | None
    # True 表示本次 run 只审 diff 变更文件。它独立于 diff_files 存在：
    # strict 增量模式下 diff_files 可以为 []（零变更 = 零文件审查），
    # 若只看 diff_files 会把空列表误当成"未提供、全量审查"。
    incremental: bool
    # CLI 选择的实际审查模式：full / incremental / full_fallback。
    # 写入 state 让断点续跑时 summary.json 仍能读到首次 run 的模式。
    review_mode: str
    issue_hint: str
    second_client_enabled: bool

    # scan
    project_map: dict
    # 错题本文本（scan 读取后供 review_chunk 注入）
    mistakes_text: str

    # chunk
    work: list[dict]

    # per-chunk payloads (Send fan-out)
    entry: dict
    chunk: dict

    # parallel review accumulation
    findings: Annotated[list[dict], operator.add]
    failed_blocks: Annotated[list[dict], _merge_failed]
    # 结构化输出永久失败（重试无意义）的块：不进 failed_blocks 账本（那账本
    # 只登记可重试的瞬时失败），但要进 summary.json，让 MCP 等调用方能区分
    # "LLM 永久失败"与"LLM 瞬时失败耗尽"。
    llm_errors: Annotated[list[dict], operator.add]
    retry_round: Annotated[int, _retry_round_reducer]

    # aggregate / second_review / report
    aggregated: list[dict]
    report_done: bool
