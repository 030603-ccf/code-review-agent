"""graph.py —— 用 LangGraph 组装审查流水线（map-reduce 结构）。

这个文件是本次重写的"主战场"：原版 pipeline.py 里**手写**的四种机制，
在这里全部换成框架机制。对照表（详见 docs/LangGraph版架构讲解.md）：

    原版手写                              LangGraph 机制
    ────────────────────────────────      ──────────────────────────────
    STAGES 列表 + 顺序执行的代码段    ->    StateGraph 的节点（node）与边（edge）
    Semaphore + gather + to_thread    ->    Send 扇出 + config 的 max_concurrency
    findings_raw.jsonl + done_chunks  ->    SqliteSaver checkpointer + thread_id
    if second_client is not None      ->    条件边（conditional edge）

==================== LangGraph 概念第四讲：StateGraph（状态图）================

StateGraph 就是"施工图纸"：你用 add_node 声明有哪些工位（节点），
用 add_edge 声明工位之间的传送带（边）。图纸画好后 compile 成
一台能跑的机器（CompiledStateGraph），invoke 一次 = 开工一轮。

START / END 是两个特殊路标：START 是入口大门，END 是收工出口。

==================== LangGraph 概念第五讲：Send（动态扇出）==================

普通边是"固定的传送带"：A 跑完必走 B。但切块之后要审几个块，
**运行到那一刻才知道**——3 个块派 3 个审查员，300 个块派 300 个。
Send 就是"临时工单"：边函数返回一批 Send("节点名", 包裹)，
框架就为每个包裹**各派一个该节点的实例**去干活，
包裹里的 dict 就是该实例看到的 state（只含它要审的那一块）。

这就是 map-reduce 的 map：fan_out 发牌 -> 多个 review_chunk 并行 ->
所有人的 findings 经 reducer 汇总 -> aggregate 做 reduce。

==================== LangGraph 概念第六讲：条件边 ==========================

add_conditional_edges("aggregate", 路由函数, 映射表)：
aggregate 跑完后调用路由函数，它返回一个字符串，
框架按映射表决定下一站。原版那行 `if self.second_client is not None:`
就是一条条件边——只是当时它写死在代码里，现在它是图上看得见的结构。

==================== LangGraph 概念第七讲：checkpoint 与 thread_id =========

checkpointer（这里用 SqliteSaver）会在**每个节点跑完后**，
把整张工作台（State）连同"接下来该走哪条边"一起写进 SQLite。
thread_id 是这次运行在账本上的**户头名**：
同一 thread_id 再次 invoke(None)，框架从上次 checkpoint 接着跑——
这就是断点续跑。原版靠 findings_raw.jsonl + done_chunks 手工记账
实现同一目标；现在账本是框架的，户头名就是 thread_id。
"""

from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

from lra.nodes import Nodes
from lra.state import ReviewState


def make_route_after_aggregate(second_client):
    """条件边路由函数的工厂：配了终审模型走 second_review，否则直达报告。

    对应原版 pipeline.py 第 143 行：
        if self.second_client is not None:
    区别：原版的"if"藏在函数体里，看一眼代码才知道有分支；
    画成条件边后，分支是图的拓扑结构，打印图就能看见。

    为什么是工厂函数而不是直接闭包定义：
        闭包函数无法单独测试（它活在 build_graph 的作用域里）；
        提取成工厂后，单元测试可以直接验证两种路由结果。
    """
    def route(state: ReviewState) -> str:
        return "second_review" if second_client is not None else "report"
    return route


def fan_out(state: ReviewState) -> list:
    """map 阶段的发牌员：chunk 跑完后，为每个块发一张 Send 工单。

    返回值有两种元素，框架都认：
      Send("review_chunk", {...})  派一个并行审查员，包裹里是那一块
      "aggregate"（字符串）        普通跳转——零块时（空项目/全部语法错误）
                                   没有牌可发，直接走去聚合，别让图断在半空

    包裹里除了 entry/chunk 还带上 run_dir：并行分支在节点里也能写
    结构化日志（logger 需要产物目录）。
    另外捎带 issue_hint（用户问题线索，可能为空串）：review_chunk 节点
    靠它把线索注入到该块的审查提示词里（见 nodes.py 的 review_chunk）。
    """
    work = state.get("work", [])
    if not work:
        return ["aggregate"]
    return [Send("review_chunk",
                 {"entry": w["entry"], "chunk": w["chunk"],
                  "run_dir": state.get("run_dir", ""),
                  "issue_hint": state.get("issue_hint", "")})
            for w in work]


def build_graph(client, second_client=None):
    """画施工图纸（未编译）。client 们在此"缝"进节点，不进 State。

    返回的是 StateGraph 图纸而不是编译后的机器——
    因为 checkpointer 要用 with 管理生命周期（见 __main__.py），
    编译动作留给调用方在 with 块里做。
    """
    nodes = Nodes(client, second_client)

    route_after_aggregate = make_route_after_aggregate(second_client)

    g = StateGraph(ReviewState)

    # ---- 六个工位（节点名 = 原版 STAGES 里的名字，一一对应）----
    g.add_node("scan", nodes.scan)
    g.add_node("chunk", nodes.chunk)
    g.add_node("review_chunk", nodes.review_chunk)
    g.add_node("aggregate", nodes.aggregate)
    g.add_node("second_review", nodes.second_review)
    g.add_node("report", nodes.report)

    # ---- 传送带（边）----
    g.add_edge(START, "scan")          # 开工先扫描
    g.add_edge("scan", "chunk")        # 扫描完切块

    # chunk 之后不是固定边，是"发牌"：每块派一个 review_chunk
    # 第三个参数是可达节点清单（给画图/校验用的声明）
    g.add_conditional_edges("chunk", fan_out, ["review_chunk", "aggregate"])
    # 每个审查员干完都走向 aggregate；框架会等**所有**并行分支到齐
    # 才真正触发 aggregate（图的拓扑汇合 = 原版的 await gather）
    g.add_edge("review_chunk", "aggregate")

    # aggregate 之后是条件边：走不走终审，编译时就由 second_client 决定
    g.add_conditional_edges(
        "aggregate", route_after_aggregate,
        {"second_review": "second_review", "report": "report"},
    )
    g.add_edge("second_review", "report")
    g.add_edge("report", END)          # 报告落盘，收工
    return g
