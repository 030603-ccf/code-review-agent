"""nodes.py —— 六个节点的薄封装。

"薄封装"是什么意思：
    节点函数里**没有业务逻辑**，只有三件事：
      1. 从 State（工作台）取材料
      2. 调用 cra 里对应的确定性函数干活
      3. 把成果写成 dict 返回（框架负责放回工作台）

    对照原版 pipeline.py：那里每个"状态"是 _run() 函数里的一段代码块；
    这里每段代码块升级成了独立函数——因为 LangGraph 的节点必须是
    "接收 state、返回 state 更新"的可调用对象。

为什么把节点收进一个 Nodes 类：
    LLMClient 对象（带连接、带 token 计数）**不能放进 State**——
    checkpoint 要把 State 序列化进 SQLite，client 序列化不了。
    所以让 Nodes 实例用 self.client 持有它（这叫**闭包/依赖注入**：
    图在编译前把依赖"缝"进节点函数里），State 里只留纯数据。
    这是用 LangGraph 时最常见的姿势，记住它。

进度展示的去处：
    原版用 EventBus + RunState 记进度（为了 Web 端的 SSE 推送）。
    这个重写版聚焦编排层本身，进度直接 print——
    想接回 EventBus 的话，在节点里加一行 emit 就行，cra 的 EventBus
    同样可以原样复用。
"""

import concurrent.futures
import json
import time
from pathlib import Path

# 确定性模块原样复用：import 时起别名（as cra_xxx），
# 一是避免和本类的方法名撞车，二是让读者一眼看出"这是借来的车"
from cra.agents.aggregator import aggregate as cra_aggregate
from cra.agents.reviewer import review_chunk as cra_review_chunk
from cra.agents.second_reviewer import second_review as cra_second_review
from cra.analysis.ast_scan import scan_project
from cra.analysis.chunking import chunk_file
from cra.memory.project_map import brief, save_project_map
from cra.report.markdown import render_report
from cra.schemas.finding import Finding

from lra.errors import PermanentError, TransientError, classify_error
from lra.logger import NodeLogger
from lra.state import ReviewState
from lra.tools import scan_anti_patterns, scan_security

# ---- 单块审查的容错参数（节点级，见 review_chunk 的注释）----
MAX_RETRIES = 5            # 瞬时错误最多重试 5 次（算上首试共 6 次机会）
BASE_DELAY = 2.0           # 指数退避基数：2s, 4s, 8s, 16s, 32s（限流后给 API 喘息时间）
REVIEW_TIMEOUT_SEC = 90    # 单次审查调用的超时上限（正常 <30s，90s 足够）
SECOND_REVIEW_WORKERS = 8  # 终审并行度（与初审不同：不受框架 max_concurrency 管，由线程池控制）

# ---- 文件过滤（chunk 节点用）：这些文件不切块、不审查 ----
# 两层判据：
#   SKIP_DIR_PARTS  路径里任意一段命中即跳过（生成物/依赖/元数据目录）
#   SKIP_GLOBS      文件名模式命中即跳过（压缩产物/生成代码）
SKIP_DIR_PARTS = {
    "node_modules", ".git", ".hg", ".svn", "__pycache__",
    ".venv", "venv", "env", "dist", "build", "out", "target",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode",
    "vendor", "third_party", "thirdparty", "site-packages", ".tox",
}
SKIP_GLOBS = ("*.min.js", "*.min.css", "*.min.js.map", "*.pb.go",
              "*.generated.*", "*.pb.cc", "*.pb.h", "*.lock")


def _should_skip(relpath: str) -> bool:
    """判断文件是否应跳过审查（返回 True = 跳过）。

    relpath 是项目内相对路径（正斜杠/反斜杠都可能），
    切段时用 Path(relpath).parts 统一处理跨平台分隔符。
    """
    import fnmatch
    if any(part in SKIP_DIR_PARTS for part in Path(relpath).parts):
        return True
    fname = Path(relpath).name
    if any(fnmatch.fnmatch(fname, pat) for pat in SKIP_GLOBS):
        return True
    return False


def _review_with_timeout(client, entry: dict, chunk: dict,
                         timeout_sec: float = REVIEW_TIMEOUT_SEC) -> list:
    """带超时的单块审查：把同步调用丢进单线程池，超时就报 TransientError。

    为什么用线程池而不是简单的调用：
        cra 的 LLMClient 用的是 httpx，自带 timeout（config 里 120s），
        但那是"单次 HTTP 请求"的超时。这里管的是**节点级**的墙钟时间——
        万一客户端内部重试把单次调用拖到几分钟，节点不能无限等下去。

    超时之后线程还在后台跑（拦不住），但我们这边先返回 TransientError，
    由调用方决定重试——旧的请求结果即使回来也不会被采用。
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(cra_review_chunk, client, entry, chunk)
        try:
            return fut.result(timeout=timeout_sec)
        except concurrent.futures.TimeoutError:
            raise TransientError(
                f"审查超时（>{timeout_sec:.0f}s）")  # 不设 retry_after_sec，走指数退避


class Nodes:
    """六个节点函数的集合，持有两个 LLM client（初审 + 可选的终审）。"""

    def __init__(self, client, second_client=None):
        self.client = client                  # 初审模型（本地 14B/7B）
        # 终审模型；None = 不启用二级审查（对应原版 pipeline 的 second_client）
        self.second_client = second_client

    # ==================== 节点 1：scan ====================

    def scan(self, state: ReviewState) -> dict:
        """静态扫描建索引（零 LLM）。对应原版 _run() 的 SCAN 段。"""
        logger = NodeLogger(state["run_dir"], "scan")
        logger.start()
        pm = scan_project(state["root"])
        # 产物落盘：project_map.json 文件名与原版保持一致
        save_project_map(pm, Path(state["run_dir"]) / "project_map.json")
        logger.done(files=pm["file_count"], brief=brief(pm))
        # 返回的 dict 会被框架合并进 State：pm 同时上了工作台
        return {"project_map": pm}

    # ==================== 节点 2：chunk ====================

    def chunk(self, state: ReviewState) -> dict:
        """沿符号边界切块（零 LLM）。对应原版 _run() 的 CHUNK 段。

        这里叠加了两道 lra 自有的过滤（cra 的 scan 不管这些）：
            1. 文件过滤：node_modules / *.min.js 等不切块（_should_skip）
            2. 增量模式：--incremental 时只切 git diff 变更的文件（diff_files）
        """
        logger = NodeLogger(state["run_dir"], "chunk")
        logger.start()
        root = Path(state["root"])
        pm = state["project_map"]
        diff_set = set(state.get("diff_files") or [])   # 增量模式的变更清单
        work: list[dict] = []
        for entry in pm["files"]:
            relpath = entry["relpath"]
            if _should_skip(relpath):
                logger.skip(f"跳过 {relpath}（匹配跳过规则）")
                continue
            if diff_set and relpath not in diff_set:
                logger.skip(f"跳过 {relpath}（未变更，增量模式）")
                continue
            if entry.get("parse_error"):
                # 语法错误的文件不炸：记下来，跳过（原版同款行为）
                logger.skip(f"跳过 {relpath}"
                            f"（语法错误：{entry['parse_error']}）")
                continue
            content = (root / relpath).read_text(
                encoding="utf-8", errors="replace")
            for c in chunk_file(entry, content):
                work.append({"entry": entry, "chunk": c})
        logger.done(files=len(pm["files"]), chunks=len(work),
                    diff_only=bool(diff_set))
        return {"work": work}

    # ==================== 节点 3：review_chunk（并行扇出的工人）====================

    def review_chunk(self, payload: dict) -> dict:
        """审查一个代码块。这个节点会被 Send **并行派发**很多次——
        每块一次，互不干扰。对应原版 _run() 里的 worker() 协程。

        原版的一句注释在这里依然成立，而且被框架放大了：
        "单块失败不拖垮整个 run：记录并继续——编排器的韧性设计。"

        payload 里除了 entry/chunk，还带着 run_dir（fan_out 塞进来的），
        这样每个并行分支都能写自己的结构化日志。

        用户线索（issue_hint）的注入走方案 B：不碰 cra_review_chunk 的
        调用签名，而是把线索写进 entry 字典的 "_issue_hint" 键，由
        cra 的 review_chunk 自己读取追加（见 cra/agents/reviewer.py）。
        注意同一文件的多个 chunk 可能共享同一个 entry dict，但这里的
        赋值是幂等的（每次写同一个字符串），并行分支互相覆盖也无所谓。
        """
        chunk = payload["chunk"]
        entry = payload["entry"]
        tag = f"{chunk['file']}:{chunk['line_start']}-{chunk['line_end']}"
        logger = NodeLogger(payload.get("run_dir", ""), "review_chunk")
        logger.start(tag=tag)

        # ---- 用户线索：有线索才往 entry 里塞，没有就什么都不做 ----
        issue_hint = payload.get("issue_hint", "")
        if issue_hint:
            entry["_issue_hint"] = issue_hint

        # ---- 零 LLM 确定性扫描：安全模式 + 语言反模式 ----
        # 不烧 token，用正则检测已知问题，生成的 dict 格式与 LLM 发现一致
        content = chunk.get("text", "")
        lang = chunk.get("file", "").rsplit(".", 1)[-1] if "." in chunk.get("file", "") else ""
        tool_findings = scan_security(entry.get("relpath", chunk.get("file", "")), content, lang)
        tool_findings += scan_anti_patterns(entry.get("relpath", chunk.get("file", "")), content, lang)
        if tool_findings:
            logger.done(tag=tag + "（工）", findings=len(tool_findings), attempt=0, tokens=0)

        # 容错策略（比原版多一层分类）：
        #   TransientError（网络抖动/超时/429）→ 指数退避重试，最多 MAX_RETRIES 次
        #   PermanentError（模型输出不可修复等）→ 不重试，直接放弃
        # 无论哪种，最终都返回 [] 而不是抛异常——单块失败不拖垮整个 run
        # 这道防线框架不替你做（它默认节点异常会中断整张图），必须自己留
        last_err: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                fs = _review_with_timeout(self.client,
                                          payload["entry"], chunk)
            except Exception as e:
                # 统一分类：_review_with_timeout 抛的 TransientError 也会
                # 被 classify_error 原样放行（errors.py 第 0 条判据）
                last_err = e
                err = classify_error(e)
                if isinstance(err, PermanentError):
                    # 永久错误：不重试，直接放弃（工具发现还在）
                    logger.fail(f"{tag} 永久错误：{type(e).__name__}: {e}")
                    return {"findings": tool_findings}
                if attempt >= MAX_RETRIES:
                    break    # 瞬时错误但重试耗尽，落到最后的 fail 分支
                delay = (err.retry_after_sec if err.retry_after_sec
                         else BASE_DELAY * 2 ** attempt)
                logger.skip(f"{tag} 瞬时错误（{type(e).__name__}），"
                            f"{delay:.0f}s 后第 {attempt + 1} 次重试")
                time.sleep(delay)
                continue
            # 成功：工具发现 + LLM 发现一起交卷
            all_findings = tool_findings + [f.model_dump(mode="json") for f in fs]
            logger.done(tag=tag, findings=len(all_findings), attempt=attempt + 1,
                        tool=len(tool_findings), llm=len(fs),
                        tokens=self.client.total_tokens_used)
            return {"findings": all_findings}

        logger.fail(f"{tag} 失败（重试耗尽）："
                    f"{type(last_err).__name__}: {last_err}"
                    if last_err else f"{tag} 失败")
        return {"findings": tool_findings}

    # ==================== 节点 4：aggregate ====================

    def aggregate(self, state: ReviewState) -> dict:
        """证据校验 + 去重 + 重排 id（零 LLM）。对应原版 AGGREGATE 段。

        框架保证：所有 review_chunk 分支都跑完，这个节点才会被触发——
        这就是 map-reduce 里的 reduce 汇合点。原版靠
        `await asyncio.gather(...)` 等所有人回来，现在靠图的拓扑保证。
        """
        logger = NodeLogger(state["run_dir"], "aggregate")
        logger.start()
        # state.get("findings", [])：极端情况（全部块失败/零块）时
        # 收纳盒是空的，get 给默认值而不是 KeyError
        findings = [Finding(**d) for d in state.get("findings", [])]
        out = cra_aggregate(findings, state["root"])
        # 落盘 findings.json（与原版同名同格式）
        (Path(state["run_dir"]) / "findings.json").write_text(
            json.dumps([f.model_dump(mode="json") for f in out],
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.done(raw=len(findings), after_qc=len(out))
        return {"aggregated": [f.model_dump(mode="json") for f in out]}

    # ==================== 节点 5：second_review（可选，条件边决定走不走）====================

    def second_review(self, state: ReviewState) -> dict:
        """终审仲裁（云模型）——并行版。

        原版 cra_second_review 逐文件串行调云 API。这里按文件拆成
        独立任务，用线程池并行提交：N 个文件的终审耗时 ≈ 最慢那个文件，
        而不是 N × 单文件耗时。每条发现就地挂回裁决，一条不删。
        """
        logger = NodeLogger(state["run_dir"], "second_review")
        findings = [Finding(**d) for d in state["aggregated"]]

        if not findings:
            logger.done(findings=0)
            return {"aggregated": []}

        # ---- 按文件分组 ----
        by_file: dict[str, list[Finding]] = {}
        for f in findings:
            by_file.setdefault(f.file_path, []).append(f)

        logger.start(findings=len(findings), files=len(by_file))

        all_out: list[Finding] = []
        save_path = Path(state["run_dir"]) / "findings.json"

        # ---- 并行：每个文件一个任务，独立调 cra_second_review ----
        # save_path=None 避免多线程抢写同一个文件；所有裁决收齐后统一落盘
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=SECOND_REVIEW_WORKERS) as ex:
            futures: dict[concurrent.futures.Future, str] = {}
            for relpath, items in by_file.items():
                fut = ex.submit(
                    cra_second_review, items, state["root"],
                    self.second_client,
                    save_path=None,   # 禁止内部写盘，线程安全
                )
                futures[fut] = relpath

            for fut in concurrent.futures.as_completed(futures):
                relpath = futures[fut]
                try:
                    result = fut.result()
                    all_out.extend(result)
                except Exception as e:
                    # 单文件终审失败：该文件所有发现标 uncertain（保守兜底）
                    logger.fail(f"{relpath} 终审失败: "
                                f"{type(e).__name__}: {e}")
                    items = by_file[relpath]
                    for f_item in items:
                        f_item.second_verdict = "uncertain"
                        f_item.second_reason = \
                            f"终审失败: {type(e).__name__}"
                    all_out.extend(items)

        # ---- 按原 id 排序回写 ----
        all_out.sort(key=lambda f: f.id)

        # ---- 统一落盘 ----
        save_path.write_text(
            json.dumps([f.model_dump(mode="json") for f in all_out],
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # ---- 战报 ----
        counts = {"confirmed": 0, "rejected": 0, "uncertain": 0}
        for f in all_out:
            if f.second_verdict in counts:
                counts[f.second_verdict] += 1
        logger.done(
            confirmed=counts["confirmed"], uncertain=counts["uncertain"],
            rejected=counts["rejected"],
            tokens=(self.second_client.total_tokens_used
                    if self.second_client else 0),
        )
        return {"aggregated": [f.model_dump(mode="json") for f in all_out]}

    # ==================== 节点 6：report ====================

    def report(self, state: ReviewState) -> dict:
        """生成 Markdown 报告。对应原版 REPORT 段。"""
        logger = NodeLogger(state["run_dir"], "report")
        logger.start()
        run_dir = Path(state["run_dir"])
        findings = [Finding(**d) for d in state["aggregated"]]
        md = render_report(findings, {
            "project": state["root"],
            "file_count": state["project_map"]["file_count"],
            "model": self.client.config.model,
            "tokens": self.client.total_tokens_used,
            # 漏斗下半段的模型和消耗（没启用就是 None，报告不显示这两行）
            "second_model": (self.second_client.config.model
                             if self.second_client else None),
            "second_tokens": (self.second_client.total_tokens_used
                              if self.second_client else None),
        })
        (run_dir / "report.md").write_text(md, encoding="utf-8")
        logger.done(findings=len(findings),
                    path=str(run_dir / "report.md"))
        return {"report_done": True}
