"""Node implementations. Thin wrappers: pull from state, call a deterministic
module, return a state update. LLM clients live on the Nodes instance, not in
state (state must serialize into SQLite; clients cannot).

Timeout policy (deliberate): there is NO node-level wall-clock timeout wrapping
the LLM call. Python cannot kill a running thread, so a ThreadPoolExecutor
wrapper would only *pretend* to time out — its `with` block blocks on shutdown
anyway. The real timeout is httpx's per-request timeout inside the client; on
top of it we retry transient errors with exponential backoff.
"""

import concurrent.futures
import copy
import hashlib
import json
import re
import time
from pathlib import Path

from lra.agents.aggregator import aggregate as do_aggregate
from lra.agents.reviewer import review_chunk as do_review_chunk
from lra.agents.rules import format_rules_injection, load_rules
from lra.agents.second_reviewer import (MISTAKE_INJECT_LIMIT,
                                        load_mistakes_text,
                                        second_review as do_second_review,
                                        write_mistakes)
from lra.analysis.chunking import chunk_file
from lra.analysis.dep_graph import build_dep_graph, format_dep_context
from lra.analysis.lsp import collect_candidates, lsp_findings
from lra.analysis.scan import scan_project
from lra.errors import PermanentError, classify_error
from lra.ignore import path_is_ignored
from lra.logger import NodeLogger
from lra.report.markdown import render_report
from lra.schemas.finding import Finding
from lra.tools import scan_anti_patterns, scan_security

MAX_RETRIES = 5
BASE_DELAY = 2.0
MAX_RETRY_ROUNDS = 1
SECOND_REVIEW_WORKERS = 8
SECOND_REVIEW_TIMEOUT = 120.0          # 小项目基础预算（秒）
# 单次终审调用的预算（秒）：DeepSeek pro 实测约 13s，qwen3.7-plus 实测约 24s，
# 留余量取 30s。终审超时随「文件数 / 并发 × 该预算」伸缩，避免大项目在固定
# 120s 内跑不完、被误标「终审超时」。
SECOND_REVIEW_PER_CALL_SECONDS = 30.0

# 文件级过滤（按文件名 glob），目录级过滤统一走 lra.ignore。
SKIP_GLOBS = ("*.min.js", "*.min.css", "*.min.js.map", "*.pb.go",
              "*.generated.*", "*.pb.cc", "*.pb.h", "*.lock")


def _should_skip(relpath: str) -> bool:
    import fnmatch
    if path_is_ignored(Path(relpath).parts):
        return True
    fname = Path(relpath).name
    return any(fnmatch.fnmatch(fname, pat) for pat in SKIP_GLOBS)


def _parse_error_line(msg: str) -> int:
    """Extract the failing line number from scan's parse_error string.

    scan_file formats it as ``f"{e.msg} (line {e.lineno})"``; fall back to 1
    when the line number cannot be parsed.
    """
    m = re.search(r"\(line (\d+)\)", msg or "")
    return int(m.group(1)) if m else 1


def _parse_error_findings(files: list[dict]) -> list[Finding]:
    """Deterministic correctness findings for files that failed to parse.

    These files are skipped in the chunk node (never sent to the LLM); without
    this they would vanish from the report entirely. Zero LLM — built straight
    from scan's ``parse_error`` field.
    """
    out: list[Finding] = []
    for entry in files:
        msg = entry.get("parse_error")
        if not msg:
            continue
        line = _parse_error_line(str(msg))
        out.append(Finding(
            id="",
            category="correctness",
            severity="critical",
            file_path=entry["relpath"],
            line_start=line,
            line_end=line,
            title="语法解析失败",
            description=str(msg),
            evidence="",
            suggestion="修复语法错误后重新审查",
            confidence=1.0,
        ))
    return out


def _chunk_cache_context(base_context: str, entry: dict) -> str:
    """把 dep_context 与 LSP 候选纳入缓存键维度。

    review_chunk 的 prompt 会注入 ``entry["_dep_context"]``（依赖文件符号）和
    ``entry["_lsp_candidates"]``（LSP warning 候选）。这两者会改变 reviewer 输出，
    却既不在文件 sha1 里、也不在全局 ``context`` 指纹里：依赖文件改了 / 换 LSP
    服务器，本文件 sha1 不变，旧缓存照样命中过期结果。这里把二者内容哈希拼进
    缓存键的 context 维度，让依赖图 / LSP 配置变化使缓存失效；二者都为空时保持
    原 context 不变（与旧键兼容，不整体失效）。
    """
    dep = entry.get("_dep_context") or ""
    lsp = entry.get("_lsp_candidates") or ""
    if not dep and not lsp:
        return base_context
    extra = hashlib.sha256(
        ("\x00".join((dep, lsp))).encode("utf-8")).hexdigest()[:16]
    return f"{base_context}\x00dep_lsp:{extra}"


def _incremental_filter(files: list[dict], diff_set: set[str]) -> list[dict]:
    """增量模式下只保留变更文件；全量模式（diff_set 为空）原样返回全部。

    parse_error 与 LSP 确定性诊断在 aggregate 节点注入，不看 diff_files 就会
    打破 ``--incremental`` 只审变更文件的承诺。这里统一按 relpath 过滤。
    """
    if not diff_set:
        return files
    return [f for f in files if f.get("relpath") in diff_set]


def _mode_filter(files: list[dict], diff_files: list[str] | None,
                 incremental: bool) -> list[dict]:
    """按本次 run 的实际模式过滤文件。

    三种情况：
      - incremental=False              → 全量，原样返回（含 full_fallback）
      - incremental=True 且 diff 非空 → 只保留变更文件
      - incremental=True 且 diff 为空 → strict 零变更，返回 []

    空的 ``diff_files`` 不能再用"空集合=全量"的旧语义：strict 模式下空集合
    必须表示"一个文件都不审"，否则 --incremental-strict 会静默退化成全量。
    """
    if not incremental:
        return files
    diff_set = set(diff_files or [])
    return [f for f in files if f.get("relpath") in diff_set]


class Nodes:
    def __init__(self, client, second_client=None, cache=None, context="",
                 lsp_cfg=None):
        self.client = client
        self.second_client = second_client
        self.cache = cache  # FindingCache | None；None=禁用
        # 缓存键的输入指纹：--issue-hint + rules.json + 错题本 的 sha256 前 16 位。
        # 这些输入影响 reviewer 输出却不体现在文件 sha1 里，必须拼进缓存键。
        self.context = context
        # LSP 确定性诊断配置（config 的 lsp 节）；None/未启用=不跑。
        self.lsp_cfg = lsp_cfg or {}

    # ---- scan ----
    def scan(self, state: dict) -> dict:
        logger = NodeLogger(state["run_dir"], "scan")
        logger.start()
        pm = scan_project(state["root"])
        (Path(state["run_dir"]) / "project_map.json").write_text(
            json.dumps(pm, ensure_ascii=False, indent=2), encoding="utf-8")
        # 错题本：run_dir 上级 memory/mistakes.jsonl（跨 run 共享），
        # 注入 state 供 review_chunk 提醒模型别重复误报；只取最近 N 条，
        # 防止长期运行下错题本无限膨胀、每个 chunk 的 prompt 越来越贵。
        mistakes_text = load_mistakes_text(
            Path(state["run_dir"]).parent / "memory" / "mistakes.jsonl",
            limit=MISTAKE_INJECT_LIMIT)
        logger.done(files=pm["file_count"])
        return {"project_map": pm, "mistakes_text": mistakes_text}

    # ---- chunk ----
    def chunk(self, state: dict) -> dict:
        logger = NodeLogger(state["run_dir"], "chunk")
        logger.start()
        root = Path(state["root"])
        pm = state["project_map"]
        # 兼容旧 checkpoint：没有 incremental 字段时，用 diff_files 是否
        # 非空来推断增量语义；strict 零变更由 incremental=True + [] 表达。
        diff_files = state.get("diff_files")
        incremental = bool(state.get("incremental")) or bool(diff_files)
        diff_set = set(diff_files or [])
        work: list[dict] = []
        for entry in pm["files"]:
            relpath = entry["relpath"]
            if _should_skip(relpath):
                continue
            if incremental and relpath not in diff_set:
                continue
            if entry.get("parse_error"):
                continue
            content = (root / relpath).read_text(encoding="utf-8", errors="replace")
            for c in chunk_file(entry, content):
                work.append({"entry": entry, "chunk": c})

        # 项目规则注入：.codereview/rules.json 中匹配该文件的规则 → _rules_text
        rules = load_rules(root)
        if rules:
            for w in work:
                rules_text = format_rules_injection(w["entry"]["relpath"], rules)
                if rules_text:
                    w["entry"]["_rules_text"] = rules_text

        # 跨文件依赖图（零 LLM）：注入 _dep_context，让 reviewer 看到依赖关系
        file_contents: dict[str, str] = {}
        for entry in pm["files"]:
            rp = entry["relpath"]
            if _should_skip(rp):
                continue
            try:
                file_contents[rp] = (root / rp).read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                pass
        try:
            dep_graph = build_dep_graph(pm, file_contents)
            for w in work:
                ctx = format_dep_context(w["entry"]["relpath"], dep_graph, pm)
                if ctx:
                    w["entry"]["_dep_context"] = ctx
        except Exception:
            pass  # 依赖图是增强项，失败不影响主流程

        # LSP 候选问题（warning/info/hint，需 LLM 验证）：按语言分组一次
        # spawn 服务器、一次遍历该语言所有文件，结果塞进 entry，该文件的每个
        # chunk 复用同一份候选文本。避免旧实现每文件 spawn 一次服务器串行
        # 白等冷启动。没配服务器/启动失败在函数内部静默跳过。
        if self.lsp_cfg.get("enabled"):
            unique_entries: dict[str, dict] = {}
            for w in work:
                relpath = w["entry"].get("relpath", "")
                if relpath and relpath not in unique_entries:
                    unique_entries[relpath] = w["entry"]
            try:
                candidates = collect_candidates(
                    root, list(unique_entries.values()), self.lsp_cfg)
            except Exception:
                candidates = {}  # LSP 候选是增强项，失败不影响主流程
            for relpath, text in candidates.items():
                unique_entries[relpath]["_lsp_candidates"] = text

        logger.done(files=len(pm["files"]), chunks=len(work), diff_only=incremental)
        return {"work": work}

    # ---- review_chunk (parallel fan-out worker) ----
    def review_chunk(self, payload: dict) -> dict:
        chunk = payload["chunk"]
        entry = payload["entry"]
        tag = f"{chunk['file']}:{chunk['line_start']}-{chunk['line_end']}"
        logger = NodeLogger(payload.get("run_dir", ""), "review_chunk")
        logger.start(tag=tag)

        hint = payload.get("issue_hint", "")
        if hint:
            entry["_issue_hint"] = hint
        mistakes_text = payload.get("mistakes_text", "")

        # sha1 增量缓存：文件内容未变 + 行区间相同的 chunk 直接复用上次 findings，
        # 跳过工具扫描和 LLM（aggregate 仍会重新定位 evidence 行号）。
        # 键含模型名：换模型后旧缓存不命中。
        relpath = entry.get("relpath") or chunk.get("file", "")
        sha1 = entry.get("sha1", "")
        model = self.client.config.model
        # 缓存键 context 维度 = 全局指纹 + dep_context/LSP 候选的内容哈希。
        # 依赖图变化或换 LSP 服务器会让缓存 miss，重新审查。
        cache_ctx = _chunk_cache_context(self.context, entry)
        if self.cache is not None:
            cached = self.cache.get(relpath, sha1,
                                    chunk["line_start"], chunk["line_end"], model,
                                    context=cache_ctx)
            if cached is not None:
                logger.done(tag=tag + "（缓存命中）", findings=len(cached))
                return {"findings": cached}

        file = chunk.get("file", "")
        lang = file.rsplit(".", 1)[-1] if "." in file else ""
        content = chunk.get("text", "")
        tool_findings = scan_security(entry.get("relpath", file), content, lang)
        tool_findings += scan_anti_patterns(entry.get("relpath", file), content, lang)

        last_err: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                fs = do_review_chunk(self.client, entry, chunk,
                                     mistakes_text=mistakes_text)
            except Exception as e:
                last_err = e
                err = classify_error(e)
                if isinstance(err, PermanentError):
                    logger.fail(f"{tag} 永久错误：{type(e).__name__}: {e}")
                    # 永久错误（JSON 输出失败）**不进缓存**：此时只有 tool_findings
                    # / 空清单，若写缓存，后续 run 会把它当完整命中、该块 LLM 审查
                    # 永久丢失。只有成功（LLM 返回合法 findings）才 put 缓存。
                    return {"findings": tool_findings,
                            "llm_errors": [{"entry": entry, "chunk": chunk,
                                            "error": f"{type(e).__name__}: {str(e)[:200]}"}]}
                if attempt >= MAX_RETRIES:
                    break
                delay = err.retry_after_sec if err.retry_after_sec else BASE_DELAY * 2 ** attempt
                time.sleep(delay)
                continue
            all_findings = tool_findings + [f.model_dump(mode="json") for f in fs]
            if self.cache is not None:
                self.cache.put(relpath, sha1,
                               chunk["line_start"], chunk["line_end"],
                               all_findings, model, context=cache_ctx)
            logger.done(tag=tag, findings=len(all_findings),
                        tool=len(tool_findings), llm=len(fs))
            return {"findings": all_findings}

        logger.fail(f"{tag} 失败（重试耗尽）：{type(last_err).__name__}: {last_err}")
        return {"findings": tool_findings,
                "failed_blocks": [{"entry": entry, "chunk": chunk,
                                   "error": f"{type(last_err).__name__}: {str(last_err)[:200]}"}]}

    # ---- retry_failed ----
    def retry_failed(self, payload: dict) -> dict:
        chunk = payload["chunk"]
        entry = payload["entry"]
        tag = f"{chunk['file']}:{chunk['line_start']}-{chunk['line_end']}"
        logger = NodeLogger(payload.get("run_dir", ""), "retry_failed")
        logger.start(tag=tag)

        last_err: Exception | None = None
        permanent_error: dict | None = None
        relpath = entry.get("relpath") or chunk.get("file", "")
        sha1 = entry.get("sha1", "")
        model = self.client.config.model
        cache_ctx = _chunk_cache_context(self.context, entry)
        for attempt in range(MAX_RETRIES + 1):
            try:
                fs = do_review_chunk(self.client, entry, chunk,
                                     mistakes_text=payload.get("mistakes_text", ""))
            except Exception as e:
                last_err = e
                err = classify_error(e)
                if isinstance(err, PermanentError):
                    logger.fail(f"{tag} 补跑永久错误：{type(e).__name__}: {e}")
                    # 永久错误不进缓存（与 review_chunk 一致：只有成功才 put），
                    # 否则空清单会被当成完整命中、该块审查永久丢失。
                    permanent_error = {"entry": entry, "chunk": chunk,
                                       "error": f"{type(e).__name__}: {str(e)[:200]}"}
                    break
                if attempt >= MAX_RETRIES:
                    break
                delay = err.retry_after_sec if err.retry_after_sec else BASE_DELAY * 2 ** attempt
                time.sleep(delay)
                continue
            findings = [f.model_dump(mode="json") for f in fs]
            if self.cache is not None:
                self.cache.put(relpath, sha1,
                               chunk["line_start"], chunk["line_end"],
                               findings, model, context=cache_ctx)
            logger.done(tag=tag, findings=len(findings))
            return {"findings": findings, "retry_round": payload["round"] + 1,
                    "failed_blocks": [{"entry": entry, "chunk": chunk, "resolved": True}]}

        logger.fail(f"{tag} 补跑失败：{type(last_err).__name__}: {last_err}")
        # 补跑仍失败：不标 resolved。账本保留该块，等下次 --retry-failed 再试。
        # 单次 run 内不会死循环：fan_out_failed 用 MAX_RETRY_ROUNDS 封顶，
        # retry_round+1 之后不再路由到 retry_failed。旧实现这里也标 resolved，
        # 导致跑完后 failed_blocks 恒为空，--retry-failed 永远找不到可补跑的块。
        out = {"findings": [], "retry_round": payload["round"] + 1,
               "failed_blocks": [{"entry": entry, "chunk": chunk,
                                  "error": f"{type(last_err).__name__}: {str(last_err)[:200]}"}]}
        if permanent_error:
            out["llm_errors"] = [permanent_error]
        return out

    # ---- aggregate ----
    def aggregate(self, state: dict) -> dict:
        logger = NodeLogger(state["run_dir"], "aggregate")
        logger.start()
        findings = [Finding(**d) for d in state.get("findings", [])]
        # 与 chunk 节点一致：parse_error / LSP 诊断只注入本次 run 实际审查的文件。
        # strict 零变更（incremental=True + diff_files=[]）必须注入空集，
        # 不能沿用旧的"空集合=全量"语义。
        diff_files = state.get("diff_files")
        incremental = bool(state.get("incremental")) or bool(diff_files)
        inject_files = _mode_filter(
            (state.get("project_map") or {}).get("files", []),
            diff_files, incremental)
        # 语法错误文件在 chunk 节点被跳过、不进 LLM，绝不能静默消失：
        # 确定性补一条 correctness/critical finding（零 LLM，不依赖缓存）。
        findings.extend(_parse_error_findings(inject_files))
        # LSP 确定性诊断（零 LLM）：语言服务器产出的高精度候选 bug，
        # 与 parse_error 一样在 do_aggregate 前并入。失败/没装服务器时静默跳过。
        if self.lsp_cfg.get("enabled"):
            try:
                findings.extend(lsp_findings(state["root"], inject_files,
                                             self.lsp_cfg))
            except Exception:
                pass  # LSP 是增强项，失败不影响主流程
        out = do_aggregate(findings, state["root"])
        (Path(state["run_dir"]) / "findings.json").write_text(
            json.dumps([f.model_dump(mode="json") for f in out],
                       ensure_ascii=False, indent=2), encoding="utf-8")
        logger.done(raw=len(findings), after_qc=len(out))
        return {"aggregated": [f.model_dump(mode="json") for f in out]}

    # ---- second_review (optional) ----
    def second_review(self, state: dict) -> dict:
        logger = NodeLogger(state["run_dir"], "second_review")
        findings = [Finding(**d) for d in state.get("aggregated", [])]
        if not findings:
            logger.done(findings=0)
            return {"aggregated": []}

        by_file: dict[str, list[Finding]] = {}
        for f in findings:
            by_file.setdefault(f.file_path, []).append(f)
        logger.start(findings=len(findings), files=len(by_file))

        all_out: list[Finding] = []
        save_path = Path(state["run_dir"]) / "findings.json"
        # 错题本与 scan 读取路径一致：run_dir 上级 memory/mistakes.jsonl
        mistakes_path = Path(state["run_dir"]).parent / "memory" / "mistakes.jsonl"

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=SECOND_REVIEW_WORKERS)
        try:
            # 终审线程竞态：do_second_review 会就地改写 Finding 对象。超时线程
            # （shutdown(wait=False) 后继续跑）若改的是原对象，主线程收集结果 /
            # 序列化时会被同时改写（verdict 与错题本写入竞态）。这里给 worker
            # 传深拷贝：超时线程改的是副本，主线程用原对象标 uncertain，互不影响。
            futures = {ex.submit(do_second_review, copy.deepcopy(items),
                                 state["root"], self.second_client, None): relpath
                       for relpath, items in by_file.items()}
            timeout = max(SECOND_REVIEW_TIMEOUT,
                          len(by_file) / SECOND_REVIEW_WORKERS * SECOND_REVIEW_PER_CALL_SECONDS)
            done, not_done = concurrent.futures.wait(futures, timeout=timeout)
            for fut in done:
                relpath = futures[fut]
                try:
                    all_out.extend(fut.result())
                except Exception as e:
                    logger.fail(f"{relpath} 终审失败: {type(e).__name__}: {e}")
                    self._mark_uncertain(by_file[relpath], f"终审失败: {type(e).__name__}")
                    all_out.extend(by_file[relpath])
            for fut in not_done:
                relpath = futures[fut]
                logger.fail(f"{relpath} 终审超时")
                self._mark_uncertain(by_file[relpath], "终审超时")
                all_out.extend(by_file[relpath])
        finally:
            # shutdown(wait=False) 让超时线程继续后台跑直到 httpx timeout（≤120s）
            # 结束，是有界泄漏；但其结果不进 findings（not_done 已标 uncertain），
            # 且不再写错题本（已移到主线程 write_mistakes），所以无副作用。
            ex.shutdown(wait=False, cancel_futures=True)

        # 错题本：只把 done（正常完成）里 rejected 的 finding 写进错题本；
        # 超时线程不写（其结果不进 all_out，且 second_review 已不再内部写）。
        write_mistakes(all_out, mistakes_path)

        all_out.sort(key=lambda f: f.id)
        save_path.write_text(
            json.dumps([f.model_dump(mode="json") for f in all_out],
                       ensure_ascii=False, indent=2), encoding="utf-8")

        counts = {"confirmed": 0, "rejected": 0, "uncertain": 0}
        for f in all_out:
            if f.second_verdict in counts:
                counts[f.second_verdict] += 1
        logger.done(**counts)
        return {"aggregated": [f.model_dump(mode="json") for f in all_out]}

    @staticmethod
    def _mark_uncertain(items: list[Finding], reason: str) -> None:
        for f in items:
            f.second_verdict = "uncertain"
            f.second_reason = reason

    # ---- report ----
    def report(self, state: dict) -> dict:
        logger = NodeLogger(state["run_dir"], "report")
        logger.start()
        run_dir = Path(state["run_dir"])
        findings = [Finding(**d) for d in state.get("aggregated", [])]
        md = render_report(findings, {
            "project": state["root"],
            "file_count": state["project_map"]["file_count"],
            "model": self.client.config.model,
            "tokens": self.client.total_tokens_used,
            "second_model": (self.second_client.config.model
                             if self.second_client else None),
            "second_tokens": (self.second_client.total_tokens_used
                              if self.second_client else None),
        })
        (run_dir / "report.md").write_text(md, encoding="utf-8")
        # 节流落盘兜底：review_chunk 并发 put 后可能只置了 dirty，run 结束前写盘。
        if self.cache is not None:
            self.cache.flush()
        logger.done(findings=len(findings), path=str(run_dir / "report.md"))
        return {"report_done": True}
