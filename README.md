# Code Review Agent v2 —— LangGraph 编排版

多 Agent 代码审查 + 自动修复系统：扫描 → 切块 → LLM 初审（跨文件依赖感知）→ 聚合质检 → 终审 → 报告 → **自动修复闭环**。

v1：`python -m cra review`（手写状态机） | v2：`python -m lra review`（LangGraph 编排）

---

## 核心优势（vs 编程 Agent）

与 Devin / Cursor / Claude Code 等通用编程 Agent 相比，本系统专注"找 bug + 修 bug"这一件事：

| 维度 | 本系统 | 编程 Agent（Devin/Cursor 等） |
|------|--------|-------------------------------|
| **Token 消耗** | 50 文件全量审查+修复 ≈ **95 万 token（¥1.5）** | 单文件修复即需 50-200 万 token |
| **识别精度** | quixbugs 召回率 **100%**（40/40），多模型验证 | 依赖人工描述问题，无法批量主动发现 |
| **沟通成本** | 零沟通，一条命令跑完 | 需多轮对话描述需求、确认方案、审查结果 |
| **并发能力** | 并行扇出审查 + 断点续跑 + 失败自动补跑 | 通常串行处理，一次一个文件 |
| **可复现性** | 同输入 → 同报告（checkpoint 可回溯） | 对话上下文丢失后不可复现 |

**一句话**：编程 Agent 是"全科医生"，本系统是"体检中心"——批量筛查、精准定位、自动开刀、无需挂号排队。

---

## 实测效果

### 召回率演进（quixbugs Python · 40 个缺陷程序）

| 版本 | 召回率 | 备注 |
|------|:------:|------|
| QC 严格（初版） | 68.4% | 16 条真 bug 被证据校验误杀 |
| QC 放宽 | 97.4% | 仅 bitcount.py 漏报，2 文件限流超时 |
| **+ 跨文件依赖图 + 新重试策略** | **100%（40/40）** | 漏报与超时文件全部清零 |

### 多模型横评（quixbugs · 同一套流水线）

| 模型 | 召回率 | findings | Token | 耗时 |
|------|:------:|:--------:|------:|-----:|
| DeepSeek-v4-flash | **100%** | 60 | 859K | 884s |
| Qwen 3.7 Max | **100%** | 98 | **208K（省 76%）** | **436s（快 2x）** |

> 结论：两模型召回率打平。Qwen token 消耗仅为 DeepSeek 的 1/4、速度翻倍；代价是 findings 更多（平均每文件 2 条 vs 1.2 条），适合"宁多勿漏 + 下游复查"的组合。

### 真实项目审查

| 项目 | 语言 | 规模 | 模型 | findings |
|------|------|------|------|:--------:|
| MemoirAI | Python + Vue | 23 文件 / 11K 行 | DeepSeek-v4-flash | 143 |
| httpx | Python | 23 文件 / 138 块 | DeepSeek-v4-flash | 审查中 |

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

## 最新更新（本轮）

### 1. 跨文件依赖图（零 LLM 确定性算法）

`cra/analysis/dep_graph.py`：从 import/require 语句构建文件间依赖关系（Python/JS/TS/Java + 同目录弱关联兜底），以"本文件被谁依赖、依赖了谁"注入审查 prompt——**召回率 97.4% → 100% 的关键之一**，让模型能发现跨文件接口不一致。

### 2. 语言规则扩展

`prompts/supplements/` 从 3 种语言扩到 **8 种**：新增 Go / Rust / C# / C++ / PHP 高频陷阱补丁（并发误用、类型杂耍、内存安全、空指针等），按文件扩展名自动注入。

### 3. 规则注入系统（参考阿里 Open Code Review 设计）

`cra/agents/rules.py`：项目根目录放 `.codereview/rules.json`，按 glob 模式匹配文件注入自定义审查规则：

```json
{
  "rules": [
    {"path": "**/*.py", "rule": "检查生成器二次消费、可变默认参数"},
    {"path": "api/**/*.ts", "rule": "所有端点必须有输入校验和错误处理"}
  ]
}
```

### 4. 失败块补跑机制（欠费/限流不再全量重跑）

旧版痛点：API 欠费（402）或限流导致的失败块被 checkpoint 记为"已完成"，续跑永远跳过，只能全量重跑。新版：

- **402 归为瞬时错误**：欠费是账户状态，充值后重试即成功
- **失败块登记账本**：瞬时错误重试耗尽的块记入 `failed_blocks`
- **`retry_failed` 补跑节点**：aggregate 后自动派发一轮补跑（轮数上限防死循环）
- **`--retry-failed` 参数**：已跑完的 run 倒带补跑失败块

### 5. 客户端主动限速（低配额账号救星）

`cra/llm/client.py` 的 `_RateGate` 匀速闸门：profile 配 `rpm: 6` 后按每 10 秒放行一个请求，**从源头避免 429**。实测：无限速版前 2 分钟出现十几次重试风暴，限速版 7 分钟仅 2 次重试——把"退避等待"的浪费转化为"并行在飞"的有效吞吐。

### 6. 重试策略升级

| 参数 | 旧值 | 新值 | 理由 |
|------|:----:|:----:|------|
| MAX_RETRIES | 3 | 5 | 限流窗口常超 30s，多给机会 |
| BASE_DELAY | 1.0s | 2.0s | 退避序列 2→4→8→16→32s |
| REVIEW_TIMEOUT_SEC | 120s | 120s（节点级） | 限流期单次调用可达 100s+ |

---

## 五步优化闭环

```
findings.json ──► ② 生成任务书 ──► ③ API 改代码 ──► ④ 复查验证 ──► ⑤ 反馈修正
     ①审查            每文件一份         fixer(pro)       verifier(flash)     未修好→回③
```

- **① 审查**：并行扇出 LLM 初审 + 零 LLM 安全扫描器
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
| quixbugs Python | Python | 40 缺陷程序 | **100%** | 依赖图 + 新重试后 |
| human-eval-java | Java | 163 缺陷程序 | 98.8% | 3 文件网络超时 |
| quixbugs Java | Java | 42 缺陷程序 | 74 条确认 | 终审驳回 9 条 |
| MSR_20 CVE | C | 3 CVE | 100% | issue_hint 引导 |

---

## 架构

```
START ──► scan ──► chunk ──► fan_out(Send×N)
                              ├─► review_chunk (并行) ─┐
                              ├─► review_chunk ────────┤
                              └─► review_chunk ────────┘
                                        │ reducer 汇总
                                        ▼
                              aggregate（去重 + 证据校验）
                                        │
                              ┌─── fan_out_failed 统一路由
                              ├─ 有失败块 → retry_failed(补跑×N) → 回到 aggregate
                              ├─ 无失败块 + 配了终审 → second_review(并行×8) → report → END
                              └─ 无失败块 + 没配终审 ────────────────────────→ report → END
```

### 容错机制

| 机制 | 说明 |
|------|------|
| 客户端匀速限速 | `rpm` 配置，从源头避免 429（低配额账号必配） |
| 指数退避重试 | 最多 5 次（2→4→8→16→32s），应对 API 限流 |
| 失败块补跑 | 重试耗尽的块登记账本，aggregate 后自动补跑一轮 |
| 断点续跑 | SqliteSaver + thread_id，中断后同命令恢复；`--retry-failed` 补跑已完成 run 的失败块 |
| 异常分类 | TransientError（重试）vs PermanentError（放弃，如烂 JSON/401） |
| 单块隔离 | 单块失败不拖垮整个 run |

---

## 项目结构

```
├── cra/                     # 业务模块（v1+v2 共享）
│   ├── agents/              # reviewer / second_reviewer / aggregator / rules
│   ├── analysis/            # AST 扫描 / 切块 / dep_graph（跨文件依赖）
│   ├── llm/                 # LLM 客户端（含 _RateGate 限速）+ JSON 修复器
│   ├── optimizer/           # ★ 五步修复闭环
│   │   ├── loop.py          # 迭代主循环（双刹车）
│   │   ├── fixer.py         # API 修改器（整文件/外科）
│   │   ├── verifier.py      # 复查器（哈希哨兵 + LLM 对质）
│   │   └── opt_state.py     # 修复状态机
│   └── server/              # Web 控制台（FastAPI）
├── lra/                     # ★ v2：LangGraph 编排层
│   ├── graph.py             # StateGraph 图定义（含 retry_failed 补跑路由）
│   ├── nodes.py             # 节点封装 + 重试策略
│   ├── state.py             # ReviewState（含 failed_blocks 账本）
│   ├── tools/               # 零 LLM 检测器
│   ├── diff.py              # 增量审查
│   └── errors.py            # 异常分类（402=瞬时）
├── prompts/                 # 审查/修复提示词
│   └── supplements/         # 8 语言高频陷阱补丁
├── tests/                   # 33 个测试（假模型 + 迷你项目，零 token）
└── config.yaml              # 模型配置（含 rpm 限速）
```

---

## 快速开始

```bash
pip install -e ".[dev]"

# 审查（并发 8；低配额账号建议在 profile 配 rpm 限速）
python -m lra review <项目路径> --profile cloud_api_deepseek-v4-flash \
    --concurrency 8 --thread-id my_review

# 带线索审查（指定关注方向）
python -m lra review <项目路径> --issue-hint "检查 SQL 注入和硬编码密码"

# 增量审查（只审 git diff）
python -m lra review <项目路径> --incremental

# 断点续跑（同命令再敲一遍）
python -m lra review <项目路径> --thread-id my_review

# 补跑失败块（充值/限流缓解后，无需全量重跑）
python -m lra review <项目路径> --thread-id my_review --retry-failed

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
| 依赖图零 LLM | import 是语法事实不是"理解"——正则/AST 做 100% 准确、零 token |
| 永久错误不补跑 | 烂 JSON/401 重跑一百次也一样；补跑只救瞬时错误（限流/欠费） |
| 主动限速优于被动退避 | 匀速放行不撞配额墙，退避等待期间吞吐为零纯属浪费 |
| 哈希哨兵 | 非本轮文件零 token 验证，安全性由哈希兜底 |
| 零 LLM 扫描器 | 硬编码密码/危险函数/SQL注入 → 规则 100% 检出，不花 token |
| JSON 修复器 | 模型输出格式错误 → 本地修复（毫秒），不重试 LLM |

---

## 测试

```bash
pytest tests/ -v        # 33 个测试：假模型 + 迷你项目，零 token
                        # 覆盖：全链路流转 / 断点续跑 / 错误分类 / 失败块补跑路由
```
