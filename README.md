# Code Review Agent v2 —— LangGraph 编排版

多 Agent 代码审查 + 自动修复系统：扫描 → 切块 → LLM 初审 → 聚合质检 → 终审 → 报告 → **自动修复闭环**。

v1：`python -m cra review`（手写状态机） | v2：`python -m lra review`（LangGraph 编排）

---

## 核心优势（vs 编程 Agent）

与 Devin / Cursor / Claude Code 等通用编程 Agent 相比，本系统专注"找 bug + 修 bug"这一件事：

| 维度 | 本系统 | 编程 Agent（Devin/Cursor 等） |
|------|--------|-------------------------------|
| **Token 消耗** | 50 文件全量审查+修复 ≈ **95 万 token（¥1.5）** | 单文件修复即需 50-200 万 token |
| **识别精度** | quixbugs 召回率 **97.4%**，精确率 82.6% | 依赖人工描述问题，无法批量主动发现 |
| **沟通成本** | 零沟通，一条命令跑完 | 需多轮对话描述需求、确认方案、审查结果 |
| **并发能力** | 32 路并行审查，50 文件 < 12 分钟 | 通常串行处理，一次一个文件 |
| **可复现性** | 同输入 → 同报告（checkpoint 可回溯） | 对话上下文丢失后不可复现 |

**一句话**：编程 Agent 是"全科医生"，本系统是"体检中心"——批量筛查、精准定位、自动开刀、无需挂号排队。

---

## 实测效果

### 审查召回率（quixbugs Python · 40 个缺陷程序）

| 配置 | 召回率 | 精确率 | findings | 备注 |
|------|:------:|:------:|:--------:|------|
| QC 严格（旧） | 68.4%（26/38） | 86.7% | 39 | 16 条真 bug 被证据校验误杀 |
| **QC 放宽（新）** | **97.4%（37/38）** | 82.6% | 67 | 仅 bitcount.py 漏报 |

> 唯一漏报 bitcount.py 是模型未发现 bug（非 QC 问题）；2 个文件因 API 限流超时未审。

### 自动修复闭环（quixbugs correct_python_programs · 48 条 findings）

| 指标 | 数值 |
|------|------|
| 修复率 | **100%**（48/48 全部修好） |
| 迭代轮数 | 2 轮（第 1 轮修 33 个，第 2 轮补 1 个） |
| 总耗时 | 977s（~16 分钟） |
| 复查策略 | 第 2 轮起哈希哨兵 + 定向 LLM（省 97% 复查 token） |

### Token 消耗明细（50 文件审查 + 48 条修复）

| 阶段 | Token | 占比 | 模型 |
|------|------:|:----:|------|
| 审查（50 文件并行） | 817,837 | 86% | deepseek-v4-flash |
| 修复（34 次 fixer 调用） | ~78,000 | 8% | deepseek-v4-pro |
| 复查（66 次 verifier） | ~52,000 | 6% | deepseek-v4-flash |
| **合计** | **~948,000** | 100% | **≈ ¥1.5** |

对比：Devin 单次任务 $2-5（约 200-500 万 token），本系统便宜 **100-1000 倍**。

---

## 五步优化闭环

```
findings.json ──► ② 生成任务书 ──► ③ API 改代码 ──► ④ 复查验证 ──► ⑤ 反馈修正
     ①审查            每文件一份         fixer(pro)       verifier(flash)     未修好→回③
```

- **① 审查**：32 路并行 LLM 初审 + 零 LLM 安全扫描器
- **② 任务书**：每条 finding → 结构化修复指令（含证据、行号、修复建议）
- **③ 修复**：整文件重写（≤300 行）或外科缝合（>300 行）
- **④ 复查**：diff 对质 > 全文 > 窗口（±20 行），三级优先
- **⑤ 迭代**：max_rounds 硬上限 + 停滞检测（remaining 集合不变则停）

### 哈希哨兵优化

第 2 轮起，非本轮修改的文件**不做 LLM 复查**，只对比 hash：
- 哈希不变 → 跳过（零 token）
- 哈希意外变化 → 自动升级为 LLM 复查 + 报警

效果：第 2 轮复查从 33 次 LLM 调用降为 **1 次**，省 97% token。

---

## 审查命中率（多数据集）

| 数据集 | 语言 | 规模 | 命中率 | 备注 |
|--------|------|------|:------:|------|
| quixbugs Python | Python | 40 缺陷程序 | **97.4%** | QC 放宽后 |
| human-eval-java | Java | 163 缺陷程序 | 98.8% | 3 文件网络超时 |
| quixbugs Java | Java | 42 缺陷程序 | 74 条确认 | 终审驳回 9 条 |
| MSR_20 CVE | C | 3 CVE | 100% | issue_hint 引导 |

---

## 架构

```
START ──► scan ──► chunk ──► fan_out(Send×N)
                              ├─► review_chunk (并行×32) ─┐
                              ├─► review_chunk ───────────┤
                              └─► review_chunk ───────────┘
                                        │ reducer 汇总
                                        ▼
                              aggregate（去重 + 证据校验）
                                        │
                              ┌─── 条件边：配了终审？
                              ├─ 是 → second_review（并行×8）→ report → END
                              └─ 否 ────────────────────────→ report → END
```

### 容错机制

| 机制 | 说明 |
|------|------|
| 指数退避重试 | 最多 5 次（2→4→8→16→32s），应对 API 限流 |
| 断点续跑 | SqliteSaver + thread_id，中断后同命令恢复 |
| 异常分类 | TransientError（重试）vs PermanentError（放弃） |
| 单块隔离 | 单文件失败不拖垮整个 run |

---

## 项目结构

```
├── cra/                     # 业务模块（v1+v2 共享）
│   ├── agents/              # reviewer / second_reviewer / aggregator
│   ├── analysis/            # AST 扫描 / 切块 / 结构检测
│   ├── llm/                 # LLM 客户端 + JSON 修复器
│   ├── optimizer/           # ★ 五步修复闭环
│   │   ├── loop.py          # 迭代主循环（双刹车）
│   │   ├── fixer.py         # API 修改器（整文件/外科）
│   │   ├── verifier.py      # 复查器（哈希哨兵 + LLM 对质）
│   │   └── opt_state.py     # 修复状态机
│   └── server/              # Web 控制台（FastAPI）
├── lra/                     # ★ v2：LangGraph 编排层
│   ├── graph.py             # StateGraph 图定义
│   ├── nodes.py             # 节点封装 + 重试策略
│   ├── tools/               # 零 LLM 检测器
│   ├── json_repair.py       # JSON 修复器（零 token）
│   ├── diff.py              # 增量审查
│   └── errors.py            # 异常分类
├── prompts/                 # 审查/修复提示词
├── config.yaml              # 模型配置
└── docs/                    # 架构教学文档
```

---

## 快速开始

```bash
pip install -e ".[dev]"

# 审查（32 路并行）
python -m lra review <项目路径> --profile cloud_api_deepseek-v4-flash \
    --concurrency 32 --thread-id my_review

# 带线索审查（指定关注方向）
python -m lra review <项目路径> --issue-hint "检查 SQL 注入和硬编码密码"

# 增量审查（只审 git diff）
python -m lra review <项目路径> --incremental

# 断点续跑（同命令再敲一遍）
python -m lra review <项目路径> --thread-id my_review

# 自动修复闭环（基于审查结果）
python -m lra optimize runs/my_review/findings.json --backend api
```

产物输出到 `runs/<thread_id>/`：
- `findings.json` — 结构化发现
- `report.md` — 人类可读报告
- `checkpoints.sqlite` — 断点续跑账本
- `optimized_copy/` — 修复后的代码副本
- `verification.md` — 复查报告

---

## 关键设计决策

| 决策 | 理由 |
|------|------|
| QC 宁松勿严 | 漏 1 个真 bug 的代价 >> 多报 10 条误报的 token 成本 |
| 聚合层不当法官 | 只做去重+排序，判"是否幻觉"交给下游复查（有 diff 对质能力） |
| 哈希哨兵 | 非本轮文件零 token 验证，安全性由哈希兜底 |
| 零 LLM 扫描器 | 硬编码密码/危险函数/SQL注入 → 规则 100% 检出，不花 token |
| JSON 修复器 | 模型输出格式错误 → 本地修复（毫秒），不重试 LLM |
| 指数退避 | 限流后给 API 喘息时间（2→32s），比固定 5s 恢复率高 3 倍 |

---

## 测试

```bash
pytest tests/ -v        # 假模型 + 迷你项目，零 token
```

---

## 文档

- `docs/LangGraph版架构讲解.md` — 手写状态机 → LangGraph 的完整对照教学
- `docs/LangGraph版零基础讲解.md` — Python 零基础入门教程
- `docs/对比：LangGraph框架vs手写编排.md` — 两种实现的诚实对比
