"""state.py —— ReviewState：整张图共享的"工作台"。

==================== LangGraph 概念第一讲：State（状态）====================

LangGraph 把一次运行想象成"一群人围着一张工作台干活"：
每个节点（干活的人）伸手从工作台上拿自己需要的材料，干完把成果
**放回工作台**，下一个节点接着用。这张工作台就是 State。

对应原版：pipeline.py 里没有显式的 State 类，状态散在
`pm`、`work`、`findings` 这些局部变量 + run_dir 里的产物文件里，
靠 `_run()` 一个函数从上到下的执行顺序串起来。
LangGraph 要求你**显式声明**工作台上有哪些格子——这就是下面的
ReviewState。

==================== LangGraph 概念第二讲：TypedDict ====================

TypedDict 是"带键名说明书的字典"：本体还是普通 dict，
但类型标注告诉编辑器和框架"这个 dict 应该有哪些键、值是什么类型"。
LangGraph 用它来描述 State 的形状。

`total=False` 的意思是：说明书里列的键**都不是必填的**——
图刚启动时工作台上只有 root/run_dir 两个格子有东西，
其余格子由沿途节点陆续补上。不写 total=False 的话，
类型检查器会要求你初始化时就把所有键填齐。

==================== LangGraph 概念第三讲：reducer（合并规则）============

普通格子：节点返回 {"work": [...]}，框架直接**覆盖**旧值——
这好理解，就像把新文件放到桌上、拿走旧文件。

但 review_chunk 是**并行扇出**的（同时有好几个审查员在干活），
他们几乎同时交回 {"findings": [...]}。如果规则是"覆盖"，
后交的人会把先交的人的成果盖掉——发现全丢了！

`Annotated[list[dict], operator.add]` 的意思是：
    这个格子的类型是 list[dict]，
    合并规则（reducer）是 operator.add——也就是列表拼接 `旧 + 新`。

比喻：工作台中央放一个**收纳盒**，每个审查员回来都把自己那沓
便签**放进盒子里**（拼接），而不是把盒子清空换成自己的（覆盖）。
框架保证这些"放便签"的动作不会互相打架（不用我们自己加锁——
原版靠"async 单线程没有 await 就不会被切走"的技巧保证，
现在框架替我们保证了）。
"""

import operator
from typing import Annotated, TypedDict


def _max_int(a: int, b: int) -> int:
    """取较大的整数：retry_round 的合并规则（并行分支写 round+1，取最大）。"""
    return a if a > b else b


class ReviewState(TypedDict, total=False):
    """审查流水线的工作台。每个键 = 一个格子，由对应节点填写。"""

    # ---- 启动时由 CLI 填进去的格子 ----
    root: str        # 待审查项目的绝对路径（字符串而不是 Path：checkpoint 要 JSON 序列化）
    run_dir: str     # 本次运行的产物目录
    diff_files: list[str]   # 增量模式的变更文件清单（--incremental 时由 CLI 填入，chunk 只审这些）
    issue_hint: str  # 用户输入的问题线索（用于引导 LLM 审查重点）；空串 = 未启用线索模式
    # 终审是否启用（fan_out_failed 路由用它决定补跑后走 second_review 还是 report）
    second_client_enabled: bool

    # ---- scan 节点填写 ----
    project_map: dict   # 项目索引（文件清单 + 符号表），同时落盘 project_map.json

    # ---- chunk 节点填写 ----
    # 待办清单：每项是 {"entry": 文件索引条目, "chunk": 切块结果}。
    # 对应原版 pipeline.py 里的 work: list[tuple[dict, dict]]——
    # 元组改成了 dict，因为 checkpoint 序列化时 dict 的键名自带说明书，更好读
    work: list[dict]

    # ---- Send 派发给单个 review_chunk 节点的"工单包裹" ----
    # 扇出时每个并行分支只看到自己负责的那一块（见 graph.py 的 fan_out）
    entry: dict
    chunk: dict

    # ---- review_chunk 节点们"往收纳盒里放"的格子（带 reducer！）----
    # 每个并行分支返回 {"findings": [这一块发现的几条]}，
    # 框架用 operator.add 把所有分支的列表拼成一份总清单。
    # 元素是 Finding.model_dump() 的 dict 而不是 Finding 对象本身：
    # checkpoint 要把 State 写进 SQLite，dict 能 JSON 序列化，对象不能
    findings: Annotated[list[dict], operator.add]

    # ---- 失败块的补跑账本（review_chunk 登记，retry_failed 消费）----
    # 每个元素：{"entry": 文件索引条目, "chunk": 切块结果, "error": 错误摘要}
    # reducer 是 add（只增不清）：登记后永远留在账上，
    # fan_out_failed 每轮都重试全部失败块，直到 retry_round 到上限
    failed_blocks: Annotated[list[dict], operator.add]
    # 补跑轮数（max reducer）：fan_out_failed 读它决定是否再发一轮。
    # 注意不能用普通字段——多个 retry_failed 并行分支同时写会报
    # InvalidUpdateError（LastValue 通道每步只收一个值），
    # max 聚合天然兼容并行写（所有分支写 round+1，取最大）
    retry_round: Annotated[int, _max_int]

    # ---- aggregate / second_review 节点填写 ----
    # 质检（证据校验 + 去重 + 重排 id）之后的发现清单。
    # 注意这个格子**没有** reducer——聚合是对全量发现"算总账"，
    # 结果就该整体替换旧值，而不是拼接
    aggregated: list[dict]

    # ---- report 节点填写 ----
    report_done: bool   # 报告已落盘的标记（留个旗子，方便续跑时判断进度）
