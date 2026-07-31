# LangGraph 版零基础讲解

> **读者假设**：你几乎不会 Python，但知道"程序是一行行执行的"这个程度。
> 本文会从最基础的语法讲起，带你读懂 `lra/` 包里的每一行代码。

---

## 第一章：这个项目是干什么的

一句话：**让 AI 自动审查代码，找出安全漏洞和问题。**

流程像工厂流水线：

```
扫描文件 → 切成小块 → 每块让AI审 → 汇总去重 → （可选）二审 → 生成报告
```

这个项目有两个版本：
- **手写版**（`E:\code-review-agent`）：作者亲手写了"流水线的调度逻辑"
- **框架版**（本项目）：用 LangGraph 框架替代调度逻辑，业务代码一行不改

---

## 第二章：Python 基础语法速成

### 2.1 变量和赋值

```python
x = 10          # 把数字 10 放进"盒子" x
name = "hello"  # 把文字 hello 放进盒子 name（文字要用引号括起来）
```

### 2.2 函数（function）

函数 = 一段可以反复使用的代码块，起个名字，需要时"调用"它。

```python
def add(a, b):      # def = 定义函数，add 是名字，a/b 是"输入参数"
    return a + b    # return = 把结果交回给调用者

result = add(3, 5)  # 调用函数，result 变成 8
```

### 2.3 类（class）和方法

类 = 一组相关函数的"收纳盒"。类里的函数叫"方法"。

```python
class Calculator:           # 定义一个类
    def __init__(self):     # 特殊方法：创建对象时自动执行（初始化）
        self.total = 0      # self.xxx = 这个对象"自己的"属性

    def add(self, n):       # 普通方法：第一个参数永远是 self（代表"我自己"）
        self.total += n     # 在自己的 total 上加 n

calc = Calculator()         # 创建一个 Calculator 对象
calc.add(5)                 # 调用它的 add 方法
print(calc.total)           # 输出 5
```

### 2.4 字典（dict）

字典 = 键值对的集合，像一本小词典：用"键"查"值"。

```python
person = {"name": "小明", "age": 20}   # 用花括号，键:值 用冒号分隔
print(person["name"])                   # 输出：小明（用方括号按键取值）
person["age"] = 21                      # 修改值
```

### 2.5 列表（list）

列表 = 一排有序的元素，用方括号。

```python
fruits = ["苹果", "香蕉", "橘子"]
print(fruits[0])        # 输出：苹果（下标从 0 开始！）
fruits.append("葡萄")   # 在末尾加一个
```

### 2.6 列表推导式（一行写循环）

```python
# 普通写法：
squares = []
for x in range(5):      # range(5) = 0,1,2,3,4
    squares.append(x * x)

# 列表推导式（一行搞定同样的事）：
squares = [x * x for x in range(5)]   # 读作："对每个x，算x*x，收集成列表"
```

### 2.7 import（导入别人的代码）

```python
import json                     # 导入整个模块
from pathlib import Path        # 从 pathlib 模块里只导入 Path 这个类
from cra.agents.aggregator import aggregate as cra_aggregate  # 导入并起别名
```

### 2.8 f-string（格式化字符串）

```python
name = "小明"
age = 20
print(f"{name}今年{age}岁")   # 输出：小明今年20岁
# f"..." 里的 {变量} 会被替换成变量的值
```

### 2.9 try/except（错误处理）

```python
try:
    result = 10 / 0         # 这行会报错（除数不能为0）
except Exception as e:      # 如果上面报错了，跳到这里
    print(f"出错了：{e}")    # 程序不会崩溃，而是执行这里
```

### 2.10 with 语句（资源管理）

```python
with open("file.txt") as f:     # 打开文件，赋给 f
    content = f.read()          # 在 with 块里使用文件
# 出了 with 块，文件自动关闭（即使中间报错也会关）
```

### 2.11 类型标注（Type Hints）

Python 3.5+ 支持"给变量贴标签说明它是什么类型"，**不影响运行**，只帮助阅读：

```python
def greet(name: str) -> str:    # name 应该是字符串，返回值也是字符串
    return f"你好，{name}"

x: int = 10                     # 声明 x 是整数
items: list[dict] = []          # items 是"字典组成的列表"
```

---

## 第三章：项目文件结构

```
E:\code-review-agent-langgraph\
├── lra/                    ← 核心代码（就 5 个文件）
│   ├── __init__.py         ← 路径引导（让 Python 找到原项目的代码）
│   ├── state.py            ← 定义"工作台"（共享状态）
│   ├── nodes.py            ← 6 个节点函数（干活的工人）
│   ├── graph.py            ← 组装流水线（画施工图纸）
│   └── __main__.py         ← CLI 入口（你在命令行敲的命令从这里开始）
├── tests/                  ← 测试
├── targets/smoke/          ← 测试用的靶子代码
└── runs/                   ← 运行产物
```

---

## 第四章：逐文件讲解

### 4.1 `__init__.py` —— 让 Python 找到"借来的代码"

```python
import os           # os 模块：和操作系统交互（读环境变量等）
import sys          # sys 模块：Python 解释器自身的功能
from pathlib import Path   # Path：处理文件路径的工具类

def _resolve_cra_lib() -> Path:
    """找到原项目的目录位置"""
    override = os.environ.get("CRA_LIB_PATH")  # 读环境变量
    if override:                                # 如果设了环境变量
        return Path(override)                   # 就用环境变量指定的路径
    return Path(r"E:\code-review-agent")        # 否则用默认路径
    # r"..." 是"原始字符串"：反斜杠 \ 不会被当作转义符

CRA_LIB = _resolve_cra_lib()   # 调用函数，把结果存起来

# sys.path 是 Python 的"搜索路径清单"：
# import 一个包时，Python 按这个清单逐个目录去找
if str(CRA_LIB) not in sys.path:       # 如果原项目路径还没在清单里
    sys.path.insert(0, str(CRA_LIB))   # 插到最前面（优先搜索）
```

**为什么需要这个文件**：本项目的业务逻辑（扫描/审查/聚合等）全在原项目 `E:\code-review-agent` 里。这个文件的作用就是告诉 Python："去那里找代码"。

---

### 4.2 `state.py` —— 定义"工作台"

**核心比喻**：LangGraph 把一次运行想象成"一群人围着一张工作台干活"。这张工作台就是 State。

```python
import operator                          # 提供加法等运算符
from typing import Annotated, TypedDict  # 类型标注工具

class ReviewState(TypedDict, total=False):
    # TypedDict = "带说明书的字典"：本质还是普通 dict，
    # 但标注了"应该有哪些键、值是什么类型"
    # total=False = 这些键都不是必填的（图刚启动时只有两格有东西）

    root: str        # 待审查项目的路径
    run_dir: str     # 产物存放目录

    project_map: dict    # 项目索引（扫描后填写）
    work: list[dict]     # 待审清单（切块后填写）

    entry: dict      # 单个审查任务的文件信息
    chunk: dict      # 单个审查任务的代码块

    # 关键！这行定义了"合并规则"：
    findings: Annotated[list[dict], operator.add]
    # Annotated[类型, 元数据] 的意思是：
    #   类型 = list[dict]（字典组成的列表）
    #   元数据 = operator.add（合并时用"加法"= 列表拼接）
    #
    # 为什么需要这个：多个审查员同时交回结果，
    # 如果规则是"覆盖"，后交的会把先交的盖掉！
    # 用"加法"就是：每人把自己的便签放进收纳盒，而不是清空盒子。

    aggregated: list[dict]   # 质检后的发现（没有reducer = 整体替换）
    report_done: bool        # 报告完成标记
```

---

### 4.3 `nodes.py` —— 6 个干活的工人

每个节点函数只做三件事：**取材料 → 干活 → 交成果**

```python
class Nodes:
    """持有 LLM 客户端的节点集合"""

    def __init__(self, client, second_client=None):
        # __init__ = 创建对象时自动执行
        # self.client = 把传进来的 client 存为"我的属性"
        self.client = client              # 初审模型
        self.second_client = second_client  # 终审模型（可以是 None = 没有）
```

**节点 1：scan（扫描）**
```python
    def scan(self, state: ReviewState) -> dict:
        # state["root"] = 从工作台取"项目路径"
        pm = scan_project(state["root"])   # 调用原项目的扫描函数
        save_project_map(pm, Path(state["run_dir"]) / "project_map.json")
        # Path(...) / "文件名" = 拼接路径，如 runs/xxx/project_map.json
        print(f"[scan] {brief(pm)}")       # 打印进度
        return {"project_map": pm}         # 把成果放回工作台
```

**节点 3：review_chunk（审查一块代码）—— 最关键的节点**
```python
    def review_chunk(self, payload: dict) -> dict:
        # payload 不是完整的 State，是 Send 发来的"工单包裹"
        chunk = payload["chunk"]           # 取出要审的代码块
        tag = f"{chunk['file']}:{chunk['line_start']}-{chunk['line_end']}"
        # 上面这行生成一个标签，如 "app.py:1-50"，用于打印

        try:
            # 调用原项目的审查函数（真正调 LLM 的地方）
            fs = cra_review_chunk(self.client, payload["entry"], chunk)
        except Exception as e:
            # 如果出错了（比如模型返回了垃圾）：
            print(f"[review] {tag} 失败：{type(e).__name__}: {e}")
            return {"findings": []}   # 返回空列表，不拖垮其他块！

        # model_dump(mode="json") = 把对象转成普通字典（方便存盘）
        return {"findings": [f.model_dump(mode="json") for f in fs]}
        # 列表推导式：对 fs 里的每个 f，调用 f.model_dump()，收集成列表
```

---

### 4.4 `graph.py` —— 画施工图纸

```python
from langgraph.graph import StateGraph, START, END  # 框架的核心组件
from langgraph.types import Send                      # 动态扇出的"工单"

def fan_out(state: ReviewState) -> list:
    """发牌员：切块完成后，为每个块发一张工单"""
    work = state.get("work", [])    # .get(键, 默认值)：键不存在时返回默认值
    if not work:                    # 如果没有待审的块（空项目）
        return ["aggregate"]        # 直接跳到聚合（字符串 = 普通跳转）
    # 列表推导式：为每个块创建一个 Send 工单
    return [Send("review_chunk", {"entry": w["entry"], "chunk": w["chunk"]})
            for w in work]
    # Send("节点名", 包裹) = 告诉框架："派一个工人去执行这个节点，
    #                         把这个包裹当作他看到的工作台"

def build_graph(client, second_client=None):
    """组装流水线"""
    nodes = Nodes(client, second_client)  # 创建节点集合

    # 路由函数：决定聚合后走哪条路
    def route_after_aggregate(state: ReviewState) -> str:
        # 如果配了终审模型 → 走 "second_review"
        # 否则 → 走 "report"
        return "second_review" if second_client is not None else "report"
        # 上面是 Python 的"三元表达式"：
        # 值A if 条件 else 值B → 条件为真取值A，否则取值B

    g = StateGraph(ReviewState)   # 创建图纸，声明工作台类型

    # 添加 6 个工位（节点）
    g.add_node("scan", nodes.scan)
    g.add_node("chunk", nodes.chunk)
    g.add_node("review_chunk", nodes.review_chunk)
    g.add_node("aggregate", nodes.aggregate)
    g.add_node("second_review", nodes.second_review)
    g.add_node("report", nodes.report)

    # 连接传送带（边）
    g.add_edge(START, "scan")       # 入口 → 扫描
    g.add_edge("scan", "chunk")     # 扫描 → 切块

    # 切块后不是固定路线，是"发牌"（动态扇出）
    g.add_conditional_edges("chunk", fan_out, ["review_chunk", "aggregate"])
    # 第三个参数 ["review_chunk", "aggregate"] 是声明"可能去哪"

    # 所有审查员干完 → 汇合到聚合
    g.add_edge("review_chunk", "aggregate")

    # 聚合后条件分支：走不走终审
    g.add_conditional_edges(
        "aggregate", route_after_aggregate,
        {"second_review": "second_review", "report": "report"},
    )
    g.add_edge("second_review", "report")  # 终审 → 报告
    g.add_edge("report", END)              # 报告 → 结束

    return g   # 返回图纸（还没编译成能跑的机器）
```

**流水线图示**：
```
START → scan → chunk → [发牌: 每块一个review_chunk] → aggregate
                                                         ↓
                                              有终审？──是──→ second_review
                                                │                    ↓
                                                否               report → END
                                                ↓
                                            report → END
```

---

### 4.5 `__main__.py` —— 命令行入口

当你在终端输入 `python -m lra review targets/smoke` 时：

```python
import argparse       # 解析命令行参数的标准库
from datetime import datetime
from pathlib import Path
import yaml           # 读 YAML 配置文件
from langgraph.checkpoint.sqlite import SqliteSaver  # SQLite 存档器

def cmd_review(args):
    root = Path(args.path).resolve()   # 把相对路径变成绝对路径
    if not root.is_dir():              # 如果不是目录
        print(f"错误：{root} 不是目录")
        return 1                       # 返回 1 = 告诉系统"出错了"

    # 创建模型客户端（读 config.yaml 里的配置）
    client = LLMClient.from_config(config_path, profile=args.profile)

    # thread_id = 这次运行的"户头名"
    thread_id = args.thread_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    # or 的妙用：如果 args.thread_id 是 None/空，就用当前时间
    # strftime = 把时间格式化成字符串，如 "20260720_215431"

    run_dir = Path("runs") / thread_id   # 产物目录
    run_dir.mkdir(parents=True, exist_ok=True)
    # parents=True：父目录不存在就一起创建
    # exist_ok=True：目录已存在也不报错

    # 框架配置
    config = {
        "configurable": {"thread_id": thread_id},  # 户头名
        "max_concurrency": args.concurrency         # 最多同时几个审查员
    }

    # with 语句管理 SQLite 连接的生命周期
    with SqliteSaver.from_conn_string(str(ckpt_path)) as saver:
        graph = builder.compile(checkpointer=saver)
        # compile = 把图纸变成能跑的机器
        # checkpointer = 告诉机器"每步完成后存档到这个SQLite"

        snap = graph.get_state(config)   # 翻账本：这个户头有没有记录？
        if snap.values:                  # 有记录！
            # invoke(None) = "不给新输入，从上次存档接着跑"
            result = graph.invoke(None, config)
        else:                            # 没记录，全新开工
            result = graph.invoke(
                {"root": str(root), "run_dir": str(run_dir)}, config)
            # 把初始材料放上工作台

    # 打印结果
    findings = result.get("aggregated", [])
    print(f"完成：{len(findings)} 个问题")
    return 0   # 返回 0 = 告诉系统"一切正常"
```

---

## 第五章：关键概念图解

### 5.1 State（工作台）

```
┌─────────────────── 工作台（ReviewState）───────────────────┐
│                                                            │
│  [root]          → "E:\targets\MemoirAI"                   │
│  [run_dir]       → "runs/20260720_215431"                  │
│  [project_map]   → {文件清单、符号表...}     ← scan 填写    │
│  [work]          → [{块1}, {块2}, ...]      ← chunk 填写   │
│  [findings]      → [发现1, 发现2, ...]      ← 审查员们放入  │
│  [aggregated]    → [质检后的发现...]         ← aggregate 填 │
│  [report_done]   → True                     ← report 填    │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

### 5.2 Send（动态扇出）

```
chunk 完成后，work 里有 95 个块：

fan_out 函数返回 95 个 Send：
  Send("review_chunk", {块1的信息})  →  派工人A去审
  Send("review_chunk", {块2的信息})  →  派工人B去审
  Send("review_chunk", {块3的信息})  →  派工人C去审
  ...（max_concurrency=3 限制同时最多3个在干活）

每个工人干完后，把自己的发现放进"收纳盒"（findings 的 reducer）：
  工人A交回 {"findings": [发现1, 发现2]}    ─┐
  工人B交回 {"findings": [发现3]}           ─┼─→ 框架自动拼接成总清单
  工人C交回 {"findings": [发现4, 发现5]}    ─┘

全部到齐后 → aggregate 节点被触发（对总清单做质检）
```

### 5.3 Checkpoint（断点续跑）

```
第1次运行：
  scan 完成 → 存档！ → chunk 完成 → 存档！ → review... → 电脑突然断电！

第2次运行（同一条命令）：
  程序启动 → 翻账本（get_state）→ "哦，这个户头有记录"
  → invoke(None) → 从最近的存档点接着跑
  → 不用重新扫描、不用重新切块、不用重新审已审完的块
```

---

## 第六章：如何运行

### 前提条件
- 原项目 `E:\code-review-agent` 存在（业务逻辑在那里）
- vLLM 模型服务在运行（`http://localhost:8000`）
- 使用原项目的 Python 环境

### 运行命令

```bash
# 进入原项目目录（用它的 .venv）
cd E:\code-review-agent

# 审查一个项目（用本地 14B 模型）
.venv\Scripts\python.exe -m lra review E:\code-review-agent-langgraph\targets\smoke --profile local_vllm

# 断点续跑（用同一个 thread-id）
.venv\Scripts\python.exe -m lra review E:\code-review-agent-langgraph\targets\smoke --profile local_vllm --thread-id smoke1

# 不启用二审
.venv\Scripts\python.exe -m lra review <路径> --second-profile none

# 跑测试
.venv\Scripts\python.exe -m pytest E:\code-review-agent-langgraph\tests -v
```

---

## 第七章：常见问题

**Q：为什么框架版只有 3 个测试？**
A：因为它只重写了"调度逻辑"（谁先干谁后干），业务逻辑（怎么扫描、怎么审查、怎么聚合）全在原项目里，原项目有 220 个测试覆盖。

**Q：`self` 是什么？**
A：Python 类的方法里，`self` 永远代表"这个对象自己"。`self.client` = "我自己的 client 属性"。调用 `obj.method()` 时，Python 自动把 `obj` 作为 `self` 传进去。

**Q：`None` 是什么？**
A：表示"什么都没有"。`second_client = None` 意思是"没有终审模型"。

**Q：为什么 findings 用 dict 而不是对象？**
A：因为 checkpoint 要把 State 存进 SQLite 数据库，数据库只认 JSON（纯文本），不认 Python 对象。`model_dump()` 就是把对象转成纯字典。

**Q：`Annotated[list[dict], operator.add]` 到底在干什么？**
A：拆开看：
- `list[dict]` = 这个格子里放的是"字典组成的列表"
- `operator.add` = 当多个工人同时往这个格子放东西时，用"加法"合并
- 列表的加法 = 拼接：`[1,2] + [3,4]` = `[1,2,3,4]`
- `Annotated[类型, 规则]` = 把类型和规则打包在一起的语法糖
