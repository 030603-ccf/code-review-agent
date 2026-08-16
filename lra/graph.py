"""Graph assembly — map-reduce pipeline.

    START -> scan -> chunk -> (fan out) review_chunk* -> aggregate
            -> (failed blocks? retry_failed* ->) aggregate
            -> second_review (optional) | report -> END
"""

from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

from lra.nodes import Nodes, MAX_RETRY_ROUNDS
from lra.state import ReviewState


def fan_out(state: ReviewState) -> list:
    """After chunk: send one review_chunk per work item, or go straight to
    aggregate when there is nothing to review."""
    work = state.get("work", [])
    if not work:
        return ["aggregate"]
    return [Send("review_chunk",
                 {"entry": w["entry"], "chunk": w["chunk"],
                  "run_dir": state.get("run_dir", ""),
                  "issue_hint": state.get("issue_hint", ""),
                  "mistakes_text": state.get("mistakes_text", "")})
            for w in work]


def fan_out_failed(state: ReviewState) -> list:
    """After aggregate: retry failed blocks while rounds remain, then route to
    second_review (if enabled) or report."""
    failed = state.get("failed_blocks", [])
    round_no = state.get("retry_round", 0)
    if failed and round_no < MAX_RETRY_ROUNDS:
        return [Send("retry_failed",
                     {"entry": b["entry"], "chunk": b["chunk"],
                      "run_dir": state.get("run_dir", ""),
                      "mistakes_text": state.get("mistakes_text", ""),
                      "round": round_no})
                for b in failed]
    if state.get("second_client_enabled", False):
        return ["second_review"]
    return ["report"]


def build_graph(client, second_client=None, cache=None, context="", lsp_cfg=None):
    nodes = Nodes(client, second_client, cache, context, lsp_cfg)
    g = StateGraph(ReviewState)

    g.add_node("scan", nodes.scan)
    g.add_node("chunk", nodes.chunk)
    g.add_node("review_chunk", nodes.review_chunk)
    g.add_node("retry_failed", nodes.retry_failed)
    g.add_node("aggregate", nodes.aggregate)
    g.add_node("second_review", nodes.second_review)
    g.add_node("report", nodes.report)

    g.add_edge(START, "scan")
    g.add_edge("scan", "chunk")
    g.add_conditional_edges("chunk", fan_out, ["review_chunk", "aggregate"])
    g.add_edge("review_chunk", "aggregate")
    g.add_conditional_edges("aggregate", fan_out_failed,
                            ["retry_failed", "second_review", "report"])
    g.add_edge("retry_failed", "aggregate")
    g.add_edge("second_review", "report")
    g.add_edge("report", END)
    return g
