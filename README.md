# Code Review Agent — LangGraph 编排版

多 Agent 代码审查 + 自动修复：扫描 → 切块 → LLM 初审（跨文件依赖感知）→ 聚合质检 →（可选终审）→ 报告 → **自动修复闭环**。

> **当前版本是 `v2/`**（单包自包含重写）。旧版 `cra/` + `lra/` 保留作历史参考，不再维护。完整文档见 **[v2/README.md](v2/README.md)**。

---

## 一句话

编程 Agent 是「全科医生」，本系统是「体检中心」——批量筛查、精准定位、自动开刀、无需挂号排队。

## 实测成绩（v2 · deepseek-v4-flash · 思考模式关闭）

| 数据集 | 召回率 | 文件级精确率 | 全量耗时 | Token |
|--------|:------:|:----------:|:------:|------:|
| quixbugs Java（40 缺陷程序） | **95.0%** | 84.4% | 28.3s | 59k |
| quixbugs Python（40 缺陷程序） | **90.0%** | 87.8% | 34.2s | 61k |

重复审查（代码未变）：**0 请求 · 0 token · 0.1s**（sha1 增量缓存命中）。

## 三条关键调优结论

1. **关掉思考模式**（`extra_body: {"thinking": {"type": "disabled"}}）——DeepSeek 思考默认开启且 `temperature` 失效，`reasoning_content` 占 token 导致 JSON 截断。关掉后：JSON 失败 5→0、token 291k→59k、143s→28s、召回率上升。
2. **用 `deepseek-v4-flash` 而非 `-pro`**——pro 实测全面更差（12 失败 vs 5、315s vs 143s），更强模型反而更不守格式。
3. **rpm 120 + 并发 16**——实测 100 并发零 429，旧 rpm 6 过度保守。

## 快速开始

```bash
cd v2
cp config.example.yaml config.yaml    # 填 api_key_env 对应的环境变量
python -m lra review /path/to/project
python -m lra review /path/to/project --incremental
python -m lra optimize runs/<thread-id> --backend api --max-rounds 3
```

依赖：`langgraph` / `httpx` / `pydantic` / `pyyaml`（见 `v2/pyproject.toml`）。

## 功能全景

审查（scan/chunk/并行初审/聚合/终审/报告）、零 LLM 安全扫描器、跨文件依赖图、自定义规则注入、错题本、结构化输出三层容错（JSON mode → 机械修复 → 字段提取）、sha1 增量缓存、优化闭环（修复缓存 + 停滞检测）、8 语言高频陷阱补丁、`scripts/analyze.py` 召回率/精确率评估。

详见 **[v2/README.md](v2/README.md)** 的完整「调优决策记录」。
