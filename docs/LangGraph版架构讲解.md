# LangGraph 版架构讲解 —— 手写状态机换成真框架，发生了什么

> 面向初学者的对照教学文档。前置读物：`E:\code-review-agent\docs\架构讲解.md`
> （原版全景）。本文只讲**编排层**的变化：原版的 `Pipeline` 手写状态机
> （docstring 自称"亲手实现的迷你 LangGraph"）被换成真正的 LangGraph，
> 业务逻辑一行没重写。
>
> 原版编排层：`cra/orchestrator/pipeline.py`（174 行）
> 新版编排层：`lra/` 包（`__init__.py` / `state.py` / `nodes.py` /
> `graph.py` / `__main__.py`，合计约 280 行，其中近一半是教学注释）
> 新版依赖：langgraph 1.2.9 + langgraph-checkpoint-sqlite 3.1.0
> （装在原项目的 `.venv` 里）

---

## 0. 一句话总览

**换司机不换车**：扫描、切块、初审、聚合、终审、报告这些"车"（cra 包里的
确定性模块）原样复用、一行不抄；换掉的只是"司机"——决定**谁先干、谁后干、
谁并行、断了怎么续**的那层调度逻辑。

原版司机是你亲手写的：`asyncio.Semaphore` 管并发、`asyncio.gather` 等汇合、
`findings_raw.jsonl` 记手工账本、`if second_client` 决定走不走终审。
新版司机是 LangGraph：节点与边管顺序、`Send` 管并行扇出、`SqliteSaver`
管账本、条件边管分支。

为什么能这么换？因为原版守住了一条设计哲学——**能确定做的事，绝不交给
概率模型**。业务逻辑全在纯函数里（输入 dict、输出 dict，不碰全局状态、
不依赖调度方式），调度层自然成了可以整个拔掉的"薄壳"。这次重写就是
对这条哲学的一次实战检验：它通过了。

---

## 1. 核心映射表：手写机制 → LangGraph 机制

这是全文的纲。每一行后面都有专节展开。

| # | 原版手写（pipeline.py） | LangGraph 机制（lra/） | 本质变化 |
|---|---|---|---|
| 1 | `STAGES` 列表 + `_run()` 里从上到下的代码段（`:26`） | `StateGraph` 的节点（`add_node`）与边（`add_edge`）（`graph.py:93-117`） | 流程从"代码的执行顺序"变成"显式声明的图结构" |
| 2 | `Semaphore(concurrency)` + `asyncio.gather` + `asyncio.to_thread`（`:97-125`） | `Send` 扇出（`graph.py:67`）+ config 的 `max_concurrency`（`__main__.py:74`） | 并行调度连同事务隔离一起交给框架；同步函数直接当节点，不需要 to_thread 桥接 |
| 3 | `findings_raw.jsonl` + `done_chunks` 手工账本（`:81-95`） | `SqliteSaver` checkpointer + `thread_id`（`__main__.py:85-102`） | 断点续跑从"自己记账自己读"变成"框架每个节点后自动存档" |
| 4 | `if self.second_client is not None:`（`:143`） | 条件边 `add_conditional_edges`（`graph.py:112-115`） | 分支从"藏在函数体里的 if"变成"图上看得见的拓扑" |
| 5 | `RunState.stage_start/stage_done` 手动计时 + `bus.emit` 事件（`:51-167`） | 框架持久化每个节点后的完整 State；进度改 print（可接回 EventBus） | 状态记录被框架接管；Web 推送层不在本次重写范围 |
| 6 | 共享状态 = `_run()` 的局部变量 + 产物文件 | `ReviewState` TypedDict + reducer（`state.py`） | 状态从"隐式的局部变量"变成"显式声明的工作台" |

---

## 2. 全景图：同一条流水线，两张图纸

```
原版（手写状态机）                     新版（LangGraph）
─────────────────────────             ─────────────────────────
_run() 函数体内顺序执行：               StateGraph 图纸：

SCAN ──► CHUNK ──► REVIEW             START ──► scan ──► chunk
                    │                                │
        ┌ Semaphore(3) 闸门                fan_out: 每块发一张 Send 工单
        ├ worker 1  (to_thread)                    ├─ Send ──► review_chunk ┐
        ├ worker 2  (to_thread)                    ├─ Send ──► review_chunk ┤ max_concurrency
        └ worker N  (to_thread)                    └─ Send ──► review_chunk ┘ 限制并行数
                    │ 边审边存 findings_raw.jsonl          │ 每节点后 checkpoint 自动落盘
                    ▼ await gather                         ▼ 框架等所有分支到齐
                AGGREGATE                            aggregate
                    │ if second_client                     │ 条件边 route_after_aggregate
              ┌─────┴─────┐                          ┌─────┴─────┐
              ▼           ▼                          ▼           ▼
        SECOND_REVIEW   （跳过）               second_review   （直达）
              └─────┬─────┘                          └─────┬─────┘
                    ▼                                      ▼
                  REPORT ──► END                        report ──► END
```

结构一模一样——这正是"薄壳替换"的含义：**变的是机制的提供者，
不变的是流程的形状。**

---

## 3. 新概念的七个"第一讲"

每个 LangGraph 概念在代码里第一次出现的位置都有一段讲解注释，
这里汇总成索引（详见各文件）：

| 概念 | 一句话解释 | 首次出现 |
|---|---|---|
| **State（状态）** | 整张图共享的"工作台"，节点从上面拿材料、把成果放回去 | `state.py` docstring |
| **TypedDict** | "带键名说明书的字典"，声明工作台上有哪些格子 | `state.py:50` |
| **reducer** | 格子的**合并规则**：并行分支同时交作业时怎么收（覆盖 or 拼接） | `state.py:76` |
| **StateGraph** | 施工图纸：`add_node` 画工位，`add_edge` 画传送带 | `graph.py:91` |
| **Send** | 动态扇出的"临时工单"：运行到那一刻才知道派几个并行工人 | `graph.py:67` |
| **条件边** | 路由函数返回字符串，框架按映射表决定下一站 | `graph.py:80-88` |
| **checkpoint / thread_id** | 框架的账本（每个节点后存全量 State）与户头名 | `__main__.py:96-124` |

---

## 4. graph.py 逐段走读

这是重写的主战场，逐段对照。

### 第 1 段：fan_out 发牌员（56-68）

```python
def fan_out(state: ReviewState) -> list:
    work = state.get("work", [])
    if not work:
        return ["aggregate"]
    return [Send("review_chunk", {"entry": w["entry"], "chunk": w["chunk"]})
            for w in work]
```

**它替掉了什么**：原版 `pipeline.py:97-125` 的一整套——`Semaphore` 闸门、
`worker()` 协程、`asyncio.to_thread` 桥接、`asyncio.gather` 汇合。

**为什么这样设计**：

- `Send("review_chunk", 包裹)` 是"临时工单"：框架为每个包裹**各派一个
  review_chunk 实例**，包裹 dict 就是该实例看到的 state。切块切出几块
  就派几个——**运行到那一刻才知道**，所以叫动态扇出。
- 列表推导式 `[Send(...) for w in work]` 等价于：

  ```python
  sends = []
  for w in work:
      sends.append(Send("review_chunk", {"entry": w["entry"], "chunk": w["chunk"]}))
  return sends
  ```
- 零块分支（空项目 / 全部文件语法错误）返回 `["aggregate"]`——字符串是
  普通跳转。没有这张"保底牌"，图会在 chunk 之后悬空，报告都生不出来。
  这是手写时代不会遇到的边角：框架的灵活性带来新边界情况。

**并行度去哪了**：不在图里，在**运行配置**里——
`config = {"max_concurrency": 3}`（`__main__.py:74`）。原版那把
`asyncio.Semaphore(concurrency)` 的闸门，现在由框架替你守。
图只声明"这些活**可以**并行"，闸门决定"**同时**放行几个"——
结构与资源分离，这比手写版干净。

**to_thread 去哪了**：原版需要它，是因为事件循环里不能跑同步的
`review_chunk`（会堵死其他协程）。LangGraph 的节点调度器自己管
执行方式，同步函数直接注册成节点即可——桥接代码消失了。

### 第 2 段：条件边（80-88）

```python
def route_after_aggregate(state: ReviewState) -> str:
    return "second_review" if second_client is not None else "report"
```

**它替掉了什么**：`pipeline.py:143` 的 `if self.second_client is not None:`。

注意一个细节：`second_client` 是**闭包变量**（`build_graph` 的入参，
路由函数定义在 `build_graph` 内部，天然能读到它）。走不走终审在
**编译图纸时**就决定了，路由函数只是把这个决定翻译成图的语言。
`三态解析`（没传回退 config / 显式 none 禁用）依然复用原版的
`_resolve_second_name`——连同它挡过的真实 bug 一起继承。

### 第 3 段：画图（91-118）

```python
g = StateGraph(ReviewState)
g.add_node("scan", nodes.scan)
...
g.add_edge(START, "scan")
g.add_edge("scan", "chunk")
g.add_conditional_edges("chunk", fan_out, ["review_chunk", "aggregate"])
g.add_edge("review_chunk", "aggregate")
g.add_conditional_edges("aggregate", route_after_aggregate,
                        {"second_review": "second_review", "report": "report"})
g.add_edge("second_review", "report")
g.add_edge("report", END)
```

六个节点名与原版 `STAGES` 列表一一对应——**名字没丢，角色没丢**。

两处需要体会的框架语义：

1. **汇合**：`g.add_edge("review_chunk", "aggregate")` 写在扇出之后，
   框架保证**所有**并行分支都到齐才触发 aggregate——这就是
   `await asyncio.gather(...)` 的图版本，但你一个 `await` 都不用写。
   map-reduce 的 map（fan_out + Send）和 reduce（reducer 汇总 +
   aggregate 算总账）在图上各就各位。
2. **第三个参数是声明不是逻辑**：`add_conditional_edges("chunk", fan_out,
   ["review_chunk", "aggregate"])` 里的清单告诉框架"路由函数可能返回
   哪些目的地"，用于画图和校验。真正的分派逻辑全在 `fan_out` 里。

---

## 5. state.py：工作台与收纳盒（对应映射表 #6）

原版没有显式的 State 类：`pm`、`work`、`findings` 是 `_run()` 的局部变量，
靠函数从上到下的执行顺序传递。LangGraph 要求显式声明：

```python
class ReviewState(TypedDict, total=False):
    root: str
    run_dir: str
    project_map: dict
    work: list[dict]
    entry: dict          # Send 工单包裹的两格
    chunk: dict
    findings: Annotated[list[dict], operator.add]   # ← 唯一的 reducer
    aggregated: list[dict]
    report_done: bool
```

三个初学要点：

- **`total=False`**：说明书里列的键都不是必填的。图刚启动时工作台上
  只有 `root`/`run_dir` 两格有东西，其余格子沿途补齐。
- **reducer 是并行世界的规矩**：普通格子后写的覆盖先写的；但
  review_chunk 是并行扇出的，多个分支几乎同时交回 `{"findings": [...]}`，
  "覆盖"会让发现全丢。`Annotated[list[dict], operator.add]` 的意思是
  "这个格子用列表拼接合并"——工作台中央放个**收纳盒**，每人回来把
  便签放进去，而不是清空盒子换成自己的。
- **为什么 `aggregated` 没有 reducer**：聚合是对全量发现"算总账"
  （证据校验 + 去重 + 重排 id），结果就该整体替换，不是拼接。
  **reducer 不是越多越好，是按格子的语义配。**

原版并行写账本的技巧（`pipeline.py:107-109` 的注释："async 单线程
没有 await 就不会被切走，多个 worker 不会交错写"）很巧妙，但它是
**你要懂事件循环才能写对的代码**。reducer 把这条规矩变成了声明。

---

## 6. nodes.py：六个薄封装（对应映射表 #1 的节点侧）

每个节点 = 从 State 取材料 → 调 cra 的函数 → 返回 dict。
以 review_chunk 为例（`nodes.py:85-108`）：

```python
def review_chunk(self, payload: dict) -> dict:
    chunk = payload["chunk"]
    tag = f"{chunk['file']}:{chunk['line_start']}-{chunk['line_end']}"
    try:
        fs = cra_review_chunk(self.client, payload["entry"], chunk)
    except Exception as e:
        print(f"[review] {tag} 失败：{type(e).__name__}: {e}")
        return {"findings": []}
    return {"findings": [f.model_dump(mode="json") for f in fs]}
```

两处刻意保留的手写痕迹：

1. **try/except 没有消失**。框架对节点异常默认中断整张图，而业务要求
   "单块失败不拖垮整个 run"——这是**业务级容错决策**，框架不知道你的
   业务允许哪块失败，这道防线依然要自己留。
2. **client 不进 State**。checkpoint 要把 State 序列化进 SQLite，
   LLMClient（带连接、带计数器）序列化不了。所以让 `Nodes` 实例用
   `self.client` 持有它（依赖注入），State 里只留纯数据——
   这就是为什么 `findings` 里放的是 `model_dump()` 的 dict 而不是
   Finding 对象。

---

## 7. __main__.py：账本与户头（对应映射表 #3）

原版断点续跑的机制（`pipeline.py:81-95`）：每块审完追加一行进
`findings_raw.jsonl`；重跑时读回账本，块身份证 `"文件:起-止"` 在账上
就跳过。手工，但有效。

新版的对应物：

```python
with SqliteSaver.from_conn_string(str(ckpt_path)) as saver:
    graph = builder.compile(checkpointer=saver)
    snap = graph.get_state(config)
    if snap.values:
        result = graph.invoke(None, config)     # 从 checkpoint 接着跑
    else:
        result = graph.invoke({"root": ..., "run_dir": ...}, config)
```

- **SqliteSaver** 在每个节点跑完后，把整张工作台 + "接下来该走哪条边"
  写进 `runs/<thread_id>/checkpoints.sqlite`。粒度从原版的"每块一行"
  变成了"每节点一份全量快照"。
- **thread_id 是户头名**：run 目录直接以它命名（`runs/<thread_id>`），
  续跑 = **原样再敲一遍同一条命令**。原版的 `--resume <目录>` 参数
  整个消失了。
- `invoke(None)` 的三态语义（实测验证过）：户头没账 → `EmptyInputError`，
  所以全新开工必须给初始 state；户头有账且没跑完 → 续跑；
  户头有账且已跑完 → 直接交还旧结果，**零新增请求**（测试
  `test_resume_via_same_thread_id` 钉死了这条：二次运行后
  `client.total_requests` 不涨）。
- `with` 管理 SqliteSaver 生命周期：进块开连接，出块关连接。
  编译和 invoke 都得在块内——出了块，机器够不到账本。

产物文件名与原版完全一致：`project_map.json` / `findings.json` /
`report.md`，外加框架的 `checkpoints.sqlite`。

---

## 8. 消失的代码 与 留下的代码

### 消失了（框架接管）

| 消失的代码 | 原版位置 | 去向 |
|---|---|---|
| `STAGES` 列表 + `_run()` 的顺序驱动 | `pipeline.py:26,49-167` | 节点与边的显式图 |
| `asyncio.Semaphore` 闸门 | `:97` | `max_concurrency` 配置 |
| `asyncio.gather` 汇合 | `:123-125` | 图的拓扑汇合 |
| `asyncio.to_thread` 桥接 | `:105` | 框架自己调度同步节点 |
| `findings_raw.jsonl` + `done_chunks` 账本 | `:30,81-95,110-114` | SqliteSaver + thread_id |
| `--resume` 参数与目录复用逻辑 | `__main__.py:68-80` | 同 thread_id 再跑一遍 |
| `RunState` 手动计时 / `stage_start/done` | 贯穿 `_run()` | 框架持久化 + print |

粗算：原版 174 行的 `_run()`，核心调度机制约占 90 行，**全部消失**；
剩下的 80 行业务调用，原样搬进了 nodes.py 的六个薄封装。

### 留下了（一行没改，直接 import）

- `cra.analysis.ast_scan.scan_project` / `chunking.chunk_file`（零 LLM 的地基）
- `cra.agents.reviewer.review_chunk`（初审 agent：人设 + 材料 + schema）
- `cra.agents.aggregator.aggregate`（证据校验三规则）
- `cra.agents.second_reviewer.second_review`（终审三态裁决）
- `cra.report.markdown.render_report`
- `cra.llm.client.LLMClient` / `structured.py` / `prompts.py`（LLM 底座三件套）
- `cra.__main__._resolve_second_name`（三态解析，连同它修过的 bug 一起继承）
- 全部提示词文件（`prompts/`）、`config.yaml`（连配置都不复制一份）

**留下的比消失的多得多**——这就是"能确定做的事绝不交给概率模型"
的复利：框架只管调度，业务逻辑与框架零耦合，换框架像换轮胎。

---

## 9. 诚实的取舍讨论

框架不是免费的午餐。四条代价，如实记录：

1. **教学上，先手写后框架是对的顺序。** 原版的 `Semaphore`、手工账本、
   `to_thread` 是理解"并行、持久化、异步桥接"这三个概念的必经之路。
   如果一开始就上 LangGraph，你会知道 Send 怎么用，但不知道它替你
   挡住了什么——**框架隐藏机制，而机制恰恰是会漏水的部分**。
   这个项目先手写再换框架，等于同一套机制学了两遍：一遍看原理，
   一遍看工业界的标准答案。
2. **新增依赖链。** langgraph 1.2.9 会带进 langchain-core（1.4.9）、
   langgraph-checkpoint 等一串包。原版后端只用 FastAPI/pydantic/openai
   这类通才库；现在编排层绑在了一个特定框架的版本语义上——
   框架升级改 API（LangGraph 0.x→1.x 就改过 imports），你要跟着迁移。
3. **调试从"读自己的代码"变成"读框架的 checkpoint"。** 原版出问题，
   打开 `findings_raw.jsonl` 一行行看就懂了——格式是你自己设计的。
   新版出问题，要会用 `graph.get_state(config)` 翻账本，看懂框架存的
   State 快照和 `next`（待走的边）。好在账本是 SQLite，通用工具能打开。
4. **语义的细微差别要心里有数。** 原版账本按**块**记（findings_raw.jsonl
   每块一行），框架按**节点**存快照。进程被杀在"审到一半"的瞬间：
   原版已审块的发现已经在盘上；框架则回到**最近一次成功的 checkpoint**，
   该超步里跑完的分支结果由框架的任务记录兜底。两者都安全，但
   "安全"的粒度不同——另外新版的 checkpoint 含全部块文本，
   sqlite 文件比原版的 jsonl 大。这是用声明换控制时必然的让渡。

还有一条**没换的**：Web 层（FastAPI + SSE 推送）依赖 EventBus/RunState，
本次重写没有接回（进度先 print）。要接也简单——节点里加一行
`bus.emit` 即可，cra 的 EventBus 同样能原样复用。这是刻意留的练习。

---

## 10. 验证与冒烟实录

**单测**（假模型 + 迷你项目，零 token）：

```
E:\code-review-agent\.venv\Scripts\python.exe -m pytest E:\code-review-agent-langgraph\tests -q
3 passed in 0.45s
```

三个测试各钉一件事：全链路 + 裁决挂回（`test_graph_end_to_end`）、
条件边跳过终审（`test_conditional_edge_skips_second_review`）、
同 thread_id 零请求续跑（`test_resume_via_same_thread_id`）。

**原版测试未被破坏**：

```
E:\code-review-agent\tests -q
163 passed
```

**真实冒烟**（本地 vLLM，qwen3-14b）：对 2 文件的微型目标
`targets/smoke`（埋了 eval 和 SQL 注入）：

- `--profile local_vllm_coder7b` 时服务上没有 `qwen25-coder-7b` 这个模型
  （服务挂的是 qwen3-14b），404——**单块失败的容错路径真实触发**：
  两块都失败，0 条发现，报告照常生成，图没有崩。
- 换 `--profile local_vllm` 后：tools.py 的 eval 被真实抓出
  （🔴 严重，证据逐字命中），db.py 那块的模型输出 JSON 没通过校验
  （StructuredOutputError，重试 2 次后放弃）——又一次走了容错路径。
  4 次请求，5438 tokens。
- 同一 `--thread-id smoke2` 再跑一遍：`检测到 checkpoint，上次已完整
  跑完，直接读结果`，**0 次请求 0 tokens**——断点续跑的户头语义真实生效。

一次冒烟把"正常路径 + 两种失败容错 + 续跑"全覆盖了，这比全绿还有
教学价值：你亲眼看到**韧性设计在真实模型面前的样子**。

---

## 11. 总结：这次重写教你的东西

**Python / 框架技能**（新版代码里真实出现的）：

1. `sys.path.insert` 路径引导 + 环境变量覆盖（`lra/__init__.py`）
2. `TypedDict(total=False)` 声明带说明书的字典
3. `Annotated[类型, 元数据]` 给类型附注解（reducer 的挂载点）
4. 闭包 / 依赖注入：用类实例或外层函数持有不可序列化的依赖
5. `with` 上下文管理器管理资源生命周期（SqliteSaver）
6. `import ... as` 起别名避免撞名、标明来源
7. LangGraph 七件套：State / reducer / StateGraph / Send / 条件边 /
   checkpointer / thread_id

**工程思想**：

1. **薄壳替换**：业务逻辑纯函数化，调度层才能整个拔掉重换——
   架构分层不是仪式感，是未来的换胎空间
2. **声明 vs 控制**：框架用声明（图、reducer、配置）换走你的控制权，
   换来的简洁是有标价的（隐藏机制、依赖链、调试方式改变）
3. **先手写后框架**：不理解 Semaphore 的人用不好 max_concurrency——
   教学顺序本身就是架构决策
4. **容错是业务决策**：框架默认节点异常中断全图，"单块失败不拖垮全局"
   这道防线换框架后依然要自己留
5. **一致性是免费的兼容层**：产物文件名、profile 语义、提示词加载
   全部与原版一致，新编排层对存量工具链（看报告的脚本、config.yaml）
   零冲击
