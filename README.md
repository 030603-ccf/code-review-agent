# lra — LangGraph 代码审查智能体

[![test](https://github.com/030603-ccf/code-review-agent/actions/workflows/test.yml/badge.svg)](https://github.com/030603-ccf/code-review-agent/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

基于 LangGraph map-reduce 流水线构建的代码审查工具。单包自包含，无兄弟包、无参考副本、无提交的密钥。

## 安装

```bash
pip install lra-code-review
# 或临时跑一次
uvx lra-code-review review /path/to/project
```

源码开发安装见下（推荐从 GitHub clone）：

```bash
pip install -e ".[dev]"
```

## 流水线

```
scan ──► chunk ──► fan-out ──► review_chunk (并行) ──► aggregate
                                                        │
                        ┌───────────────────────────────┤
                        ▼                               ▼
               second_review (可选)               report ──► END
                        │
                        └──► report
```

- **scan** — 建立项目索引（文件清单 + 符号表），Python 用 `ast`，其他语言用轻量启发式。零 LLM。
- **chunk** — 沿符号边界切块，绝不腰斩函数。零 LLM。
- **review_chunk** — 每块一次 LLM 调用，`Send` 扇出并行。确定性安全/反模式扫描器 + 跨文件依赖上下文先跑，再合并 LLM 的发现。
- **aggregate** — 证据校验（行号纠正）、去重、全局重排 id。零 LLM。
- **second_review** — 可选云端仲裁，对每条发现确认/驳回/存疑，按文件并行。
- **report** — 生成 `report.md`。

## 用法

```bash
cp config.example.yaml config.yaml   # 然后导出 API key 环境变量
python -m lra review /path/to/project
python -m lra review /path/to/project --incremental          # 只审 git diff
python -m lra review /path/to/project --incremental --incremental-strict  # 非 git 报错；零变更审零文件
python -m lra review /path/to/project --issue-hint "检查 SQL 注入"
python -m lra review /path/to/project --run-dir /data/lra-runs  # 产物根目录

# 审查后，对发现运行修复闭环
python -m lra optimize runs/<thread-id> --backend api --max-rounds 3
python -m lra optimize runs/<thread-id> --backend api --issue-hint "检查 SQL 注入"
```

续跑 / 重试 / 缓存：

```bash
python -m lra review /path/to/project --thread-id my-run   # 同 thread 续跑
python -m lra review /path/to/project --thread-id my-run --retry-failed
python -m lra review /path/to/project --no-cache           # 跳过 sha1 缓存
```

产物落在 `<run-dir>/<thread-id>/`（默认 `runs/<thread-id>/`）：`project_map.json`、`findings.json`、`report.md`、`checkpoints.sqlite`、**`summary.json`**（机器可读摘要：mode/status/token/耗时/失败块/LLM 永久错误）。跨 run 的发现缓存在 `<run-dir>/.findings_cache.json`。

## 功能地图

| 层 | 模块 |
| --- | --- |
| LLM | `client`（线程安全、httpx 真超时、env 密钥、JSON mode）· `structured`（解析 → 修复 → 字段提取 → 重试）· `prompts`（profile 变体 + 8 语言补丁） |
| 分析 | `scan`（ast + 启发式）· `chunking`（符号边界）· `dep_graph`（跨文件 import 图）· `lsp`（语言服务器诊断） |
| 智能体 | `reviewer` · `aggregator` · `second_reviewer` · `rules`（`.codereview/rules.json`） |
| 工具 | `security_scanner` · `anti_pattern_scanner` · `lsp_client`（stdio 上的 JSON-RPC）—— 确定性、零 LLM、诚实置信度 |
| 优化 | `copier` · `fixer`（api/opencode，compile 闸门）· `verifier` · `loop`（修复缓存 + 停滞检测） |
| 其他 | `mistake_notebook`（驳回发现 → 负样本）· `cache`（sha1 发现缓存） |

评估：`python scripts/analyze.py <run_dir> <quixbugs_dir> --correct-dir <correct_dir>` 对照 quixbugs 已知 bug 数据集计算召回率/精确率；`--correct-dir`（正确版程序）启用行级召回指标。

## 配置

见 `config.example.yaml`。密钥从每个 profile 的 `api_key_env` 指定的环境变量读取，永不进仓库。`cloud`（DeepSeek）profile 已内置 `extra_body: {"thinking": {"type": "enabled"}}`，配合 `lra/agents/reviewer.py` 的 `MAX_OUTPUT_TOKENS = 16384` —— 原因见下方调优决策 #1。

`lsp` 节（默认 `enabled: false`）开启确定性语言服务器诊断：severity 为 Error 的诊断直接变成 `correctness` 发现，Warning/Info/Hint 作为候选注入 reviewer prompt 让 LLM 验证。需要每个配置的服务器在 PATH 上（`pip install pyright` 提供 `pyright-langserver`）；缺失或损坏的服务器静默跳过，没装就别开。

## 调优决策（为什么这么配）

这些是 quixbugs 数据集真实 A/B 跑出来的经验结论。未经重新测量不要回退。

1. **开思考模式 + 提高 `MAX_OUTPUT_TOKENS`。** 思考模式（`thinking: enabled`）显著提升检出率（quixbugs 行级召回 57.5%→92.5%、文件级 100%）。但 reasoning 与正文**共享 max_tokens 预算**，而 `reviewer.py` 的 `MAX_OUTPUT_TOKENS` 会覆盖 config.yaml 的 max_tokens——旧值 2048 会被 reasoning 吃满、截断正文 JSON（`no JSON object found`），这正是早先「关思考更好」的假象根源。正确姿势：`extra_body: {"thinking": {"type": "enabled"}}` 且 `MAX_OUTPUT_TOKENS = 16384`。代价总 token ~2.5×（124k→313k，含二审），换来召回接近翻倍，划算。
2. **用 `deepseek-v4-flash`，不要 `-pro`。** pro 实测每项都更差：12 个 JSON 失败 vs 5、315s vs 143s、362k vs 291k token —— 因为更强的模型反而**更不守格式**。对严格 JSON 输出，「听话」胜过「聪明」。
3. **rpm 120、并发 16。** 探测 20/50/100 并发都零 429，旧的 `rpm: 6` 极度保守。8 倍提速。
4. **sha1 发现缓存。** scan 已给每个文件算哈希；`review_chunk` 在 `(file, sha1, 行区间)` 命中缓存时跳过 LLM。键还嵌了模型名和 `CACHE_VERSION`（改 prompt/schema 时 +1），旧结果永不浮出。重复跑：0 请求、0.1s。
5. **结构化输出三层容错。** JSON mode（预防）→ 机械修复 → 字段提取（抢救）→ LLM 重试。字段提取器只信任**块内唯一出现**的分类/严重度，且从不逐字提取 `evidence` —— 聚合器按报告的行号重新回读源码。

### 实测结果（quixbugs、deepseek-v4-flash、开思考 + MAX_OUTPUT_TOKENS=16384）

| 指标 | Java（40 buggy） | Python（40 buggy） |
| --- | --- | --- |
| 行级召回率 | 92.5% | 77.5% |
| 文件级召回率 | 100.0% | 97.5% |
| 文件级精确率 | 83.3% | 79.6% |
| JSON 失败 | 0 | 0 |
| 全量耗时 | 234s | 260s |
| Token | 313k | 331k |

召回率按**行级**报告：一条发现必须覆盖真实 bug 行（`scripts/analyze.py --correct-dir` 用正确版 diff 定位它）。文件级召回（Java 100% / Python 97.5%）只统计「有发现落在 bug 文件」就计数，会虚高，不再作为标题指标。

### 多数据集测试效果

同一套配置（`deepseek-v4-flash` 初审·开思考 + `MAX_OUTPUT_TOKENS=16384`，`deepseek-v4-pro` 二审）在 `targets/` 下多个公开 bug 数据集上的实测：

| 数据集 | 规模 | 行级召回 | 文件级召回 | 说明 |
| --- | --- | --- | --- | --- |
| quixbugs（Java） | 40 程序 | 92.5% | 100% | 每程序 1 个植入 bug，`analyze.py --correct-dir` 行级口径 |
| human-eval-java | 163 程序 | 89.0% | 99.4% | 突变算子植入，`buggy/` vs `correct/` diff 定位 bug 行 |
| msr20_samples | 3 CVE | 2/3 精准 | — | 整数溢出 + SQL 注入精准命中；1 个缓冲区溢出找到但未定位到整数溢出根因 |
| smoke | 2 文件 | 4/4 | — | 硬编码密码 / SQL 注入 / eval 全部命中 |

本地无独立 buggy 代码、不可作文件级审查的数据集：

| 数据集 | 原因 |
| --- | --- |
| defects4j-master | 本地是框架代码 + `project_repos/get_repos.sh`，真正的 buggy 项目未下载 |
| MSR_20_Code_vulnerability_CSV_Dataset | 44361 条漏洞记录是 CSV 元数据（`cve_id`/`commit_id`/`files_changed`），代码未落成独立文件 |

注：human-eval（163 文件）二审 `deepseek-v4-pro` 约需 253s 跑完——已把二审超时从固定 120s 改为随文件数伸缩（`SECOND_REVIEW_PER_CALL_SECONDS`），大项目不再被误标「终审超时」。初审结果进 sha1 缓存，重跑二审只花 ~189k token、0 初审请求。

### 优化演进（每次改动的效果）

以下数字都在同一 quixbugs Java 集（48 文件）+ `deepseek-v4-flash` 上测出（除非注明）。「改前/改后」是真实运行，不是估算。

| 改动 | 改前 | 改后 | 提升 |
| --- | --- | --- | --- |
| 开思考 + `MAX_OUTPUT_TOKENS` 2048→16384 | 行级召回 57.5%、文件级 ~95%、总 token 124k | 行级召回 92.5%、文件级 100%、总 token 313k | 召回 +35pp · 文件级 100%（token ~2.5×） |
| 关思考模式（`thinking: disabled`） | 5 个 JSON 失败、291k token、143.8s、50 发现 | 0 失败、59k token、28.3s、85 发现 | 失败清零 · -80% token · 5× 提速 |
| rpm 6 → 120 + 并发 4 → 16 | 全量 1439s | 143.8s | 8.4× 提速 |
| sha1 发现缓存 | 重复跑 28.3s / 59k token | 0.1s / 0 token | 零 LLM 请求 |
| JSON mode + 字段提取 | 失败率 25%（12/48） | 10%（5/48） | -58% 失败 |
| 终审开思考（`deepseek-pro-think`） | confirmed 精确率 35.4% | 45.2% | +10pp 精确率 |
| 行级指标（`analyze.py --correct-dir`） | 「召回 95%」（文件级，虚高） | 57.5% 行级（真实） | 诚实的标题 |

发现级精确率（覆盖真实 bug 行的发现占全部发现）：Java 33.8%、Python 41.3% —— 本工具偏召回导向（「宁可多报不可漏报」），这是有意为之的取舍。

### 测试数演进

| 阶段 | 测试数 | 落地的功能 |
| --- | --- | --- |
| v2 重写 | 78 | 单包核心 + graph/state/nodes + CLI |
| +correctness 分类、字段提取、rules、错题本、profile 提示词 | 85 | |
| +评估行级、ignore 清单合并、缓存节流、build 复查收口、错题本去重 | 107 | |
| +LSP 集成 | 113 | |
| +LSP 两阶段（error 直接、warning→LLM 验证） | 119 | |
| +issue-hint（审查 + 修复） | 124 | |
| +错题本去重键 (file,title)、README 同步 | 126 | |
| 第三轮修复（rootUri、incremental、LSP 性能、指纹粒度） | 137 | |
| +optimize 断点续跑 + second_review 线程泄漏修复 | 141 | |

开思考后，历史「能力边界」漏报 quicksort（丢 pivot 相等元素）与 reverse_linked_list（指针接错）已被补上；当前行级漏报仅剩 GET_FACTORS / POSSIBLE_CHANGE / WRAP 三处，均是 finding 语义正确但行号偏 1~2 行的定位误差（文件级已 100% 命中）。

## 设计说明

- **LLM 调用没有墙钟 kill switch。** Python 线程无法被强杀，所以「节点级超时」包裹阻塞调用是假的 —— `ThreadPoolExecutor.__exit__` 退出时照样阻塞。真正的超时来自 `httpx`；在此基础上，瞬时错误用指数退避重试。
- **线程安全的 token 计数。** 客户端用锁保护计数器。
- **失败块账本去重并消解。** 自定义 reducer 把 `failed_blocks` 按 `(file, 行区间)` 键控，重试成功就删掉该块，`--retry-failed` 不会重跑已恢复的块。

## 明确不做（deliberately）

- **symbol_backend / distill / aging** —— 旧配置声明了这些但实现从未存在；它们是死开关，不是功能。

## 变更日志（主要改动及其效果）

每条对应 `main` 上的一个 git commit；效果列是实测差值，不是预估。

| 改动 | 效果 |
| --- | --- |
| 二审超时随工作量伸缩（`SECOND_REVIEW_TIMEOUT` 固定 120s → 动态） | human-eval 162 文件二审 120s 超时→253s 完整跑完，confirmed 104→209 |
| 开思考 + `MAX_OUTPUT_TOKENS` 2048→16384 | quixbugs Java 行级召回 57.5%→92.5%、文件级 100% |
| v2 单包重写（只留 lra，无 cra/参考副本） | 干净的 78 测试基线 |
| 补回 optimizer / rules / 错题本 / 依赖图 / 字段提取 / 8 语言补丁 / 评估脚本 | 功能恢复，测试 85 |
| 关 DeepSeek 思考模式（`thinking: disabled`） | JSON 失败 5→0、token -80%、5× 提速 |
| rpm 6→120 + 并发 16 | Java 全量 1439s → 143.8s（8.4×） |
| sha1 发现缓存 + CACHE_VERSION + 模型入键 | 重复跑 0 请求 / 0.1s |
| 行级召回指标（`analyze.py --correct-dir`） | 诚实的 57.5% vs 虚高的 95% |
| 第一轮评审修复（package-data、缓存键维度、parse_error、extra_body） | 可安装性 + 正确性 |
| 第二轮评审修复（rootUri、incremental、LSP 性能、指纹粒度、去重键、README） | LSP rootUri + incremental 语义 |
| optimize 断点续跑（OptState.load + 持久 FixCache）+ second_review 线程泄漏修复 | 真续跑、无副作用泄漏 |
| 目录整理：v2 提升为仓库根、删除全部旧残留 | 仓库 == 干净新项目 |
| v2.1 MCP 前置改造：`--run-dir` + `summary.json` + `--incremental-strict` | 产物目录可控、机器可读摘要、零变更不误跑全量 |

测试数：**78 → 173**（贯穿以上，`python -m pytest tests -q` 全绿）。
