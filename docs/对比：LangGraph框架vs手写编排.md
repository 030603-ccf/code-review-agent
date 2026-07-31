# 对比：LangGraph 框架 vs 手写编排

> 本文基于两个**功能完全相同**的项目进行对比：
> - 手写版：`E:\code-review-agent`（编排层 = `cra/orchestrator/pipeline.py`，174 行）
> - 框架版：`E:\code-review-agent-langgraph`（编排层 = `lra/` 包，约 280 行含教学注释）
>
> 两者共享同一套业务逻辑（扫描/切块/审查/聚合/终审/报告），产物格式一致。

---

## 一、测试结果对比

| 测试项 | 手写版 (cra) | 框架版 (lra) |
|--------|-------------|-------------|
| 单测数量 | 220 个 | 3 个 |
| 测试通过 | ✅ 220 passed | ✅ 3 passed |
| 覆盖范围 | 全链路（LLM客户端/扫描/切块/聚合/报告/服务器/优化器/eval） | 编排层端到端（全图流转/条件边/断点续跑） |
| 真实冒烟 | MemoirAI 23文件/95块/299831 tokens | smoke 2文件/2块/5438 tokens |
| 断点续跑 | ✅ findings_raw.jsonl 手工账本 | ✅ SqliteSaver + thread_id |
| 并行审查 | ✅ Semaphore(3) + gather | ✅ Send + max_concurrency=3 |
| 条件分支 | ✅ if second_client | ✅ 条件边 |
| 容错（单块失败） | ✅ try/except 手挡 | ✅ try/except 手挡（框架不替你做） |

**结论**：功能完全等价。框架版测试少是因为它只重写了编排层，业务逻辑的 220 个测试仍在原版项目里跑。

---

## 二、代码量对比

| 模块 | 手写版 | 框架版 | 说明 |
|------|--------|--------|------|
| 编排核心 | pipeline.py 174行 | graph.py 119行 + state.py 86行 + nodes.py 169行 | 框架版行数多，但近一半是教学注释 |
| 并发控制 | ~30行（Semaphore+gather+to_thread） | 1行（config里写max_concurrency） | 框架接管 |
| 断点续跑 | ~25行（jsonl读写+done_chunks） | ~15行（SqliteSaver+get_state） | 框架接管 |
| 条件分支 | 1行 if | 5行（路由函数+add_conditional_edges） | 框架反而更啰嗦 |
| 状态传递 | 局部变量，隐式 | TypedDict 显式声明 | 框架要求 |
| 业务逻辑 | 同一份 | 同一份（import复用） | 零重复 |

---

## 三、核心机制映射

| 功能 | 手写版怎么做 | 框架版怎么做 | 谁更好 |
|------|-------------|-------------|--------|
| **流程定义** | `_run()`函数从上到下执行 | StateGraph 节点+边，显式声明 | 框架：可视化、可打印图结构 |
| **并行扇出** | asyncio.Semaphore + gather + to_thread | Send + max_concurrency | 框架：不用懂事件循环 |
| **结果汇总** | 单线程无await不交错写的技巧 | reducer（operator.add） | 框架：声明式，不用理解事件循环 |
| **断点续跑** | findings_raw.jsonl 每块追加一行 | SqliteSaver 每节点全量快照 | 各有千秋（见下） |
| **条件分支** | if 语句 | 条件边 + 路由函数 | 手写更简洁 |
| **进度追踪** | EventBus + RunState（支持Web SSE） | print（未接回EventBus） | 手写更完整 |
| **错误调试** | 打开jsonl直接看 | graph.get_state() 翻SQLite | 手写更直观 |

---

## 四、断点续跑的粒度差异

这是两个版本最本质的区别：

```
手写版（按"块"记账）：
  审完第1块 → 写一行到 findings_raw.jsonl
  审完第2块 → 写一行到 findings_raw.jsonl
  ...进程被杀...
  重跑 → 读账本，跳过已审的块，从第N+1块继续
  ✅ 粒度细：只丢失"正在审的那一块"

框架版（按"节点"快照）：
  scan完成 → 存一次全量State到SQLite
  chunk完成 → 存一次全量State到SQLite
  所有review_chunk完成 → 存一次
  ...进程被杀...
  重跑 → 从最近的checkpoint恢复
  ⚠️ 粒度粗：可能丢失"整个review阶段"的进度
  （但框架内部有超步记录，实际丢失比想象中少）
```

---

## 五、什么时候该用框架，什么时候该手写

### 适合用 LangGraph 的场景：
- 团队有多人协作，需要**可视化流程图**（图结构比代码块更容易讨论）
- 需要**开箱即用的断点续跑**，不想自己设计账本格式
- 流程中有**复杂的分支/循环/人工介入节点**（框架的条件边/循环边比手写if更清晰）
- 需要**框架级的可观测性**（LangSmith追踪、checkpoint回放）
- 项目初期快速原型，流程可能频繁变动

### 适合手写的场景：
- 流程**简单线性**（扫描→切块→审查→聚合→报告），手写100行就够
- 需要**极致控制**（自定义并发策略、细粒度断点、自定义事件推送）
- 不想引入**额外依赖链**（langgraph带进langchain-core等一串包）
- 教学目的：理解并发/持久化/异步桥接的**底层原理**
- 对**性能敏感**：框架的每节点全量序列化有开销

### 本项目的结论：
> 这个项目先手写再换框架，等于同一套机制学了两遍：一遍看原理，一遍看工业界标准答案。
> **先手写后框架是正确的学习顺序**——不理解Semaphore的人用不好max_concurrency。

---

## 六、依赖对比

| 手写版依赖 | 框架版额外依赖 |
|-----------|--------------|
| openai SDK | langgraph 1.2.9 |
| pydantic | langgraph-checkpoint-sqlite 3.1.0 |
| FastAPI + uvicorn | langchain-core（被langgraph拉入） |
| PyYAML | |
| httpx | |

框架版多了约 5 个间接依赖。版本升级时（LangGraph 0.x→1.x 改过import路径），需要跟着迁移。

---

## 七、一句话总结

> **手写版是"自己造车"，框架版是"买了辆底盘，把发动机（业务逻辑）装上去"。**
> 造车的过程让你理解发动机为什么这样设计；买底盘让你更快上路。
> 两者不矛盾——先造后买，才是工程师的成长路径。
