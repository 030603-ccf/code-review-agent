"""loop.py — 迭代修复闭环：修 → 复查 → 仍存在的进下一轮。

三个刹车（缺一不可）：
    1. max_rounds   轮次硬上限，防止无限烧 token
    2. 停滞检测      本轮修复后 hash_tree 与上轮完全相同 = 修改器卡住了，提前停
    3. 修复缓存      键 = (finding id 集合排序哈希 + 修复前文件 sha1
                     + 后端名 + 模型名 + prompt 版本常量)，命中直接复用
                     上次修复结果（文件内容），跳过 fixer 调用
"""

import hashlib
import json
import threading
import time
from pathlib import Path

from lra.optimizer.copier import hash_tree
from lra.optimizer.fixer import FixTask
from lra.optimizer.verifier import verify_fixes

# 节流落盘间隔（秒）：与 FindingCache 一致，避免每次 put 全量重写 JSON 的 O(N²) 写放大。
SAVE_INTERVAL_SEC = 2.0

# 任务书模板版本：改 render_fix_prompt 的语义时必须 +1，缓存键随之失效
PROMPT_VERSION = 1

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class FixCache:
    """修复结果缓存：内存 dict + 可选 JSON 落盘（断点续跑时复用）。

    内存更新总是立即（加锁），落盘节流：距上次落盘超过 SAVE_INTERVAL_SEC 才写盘，
    否则只置 dirty；优化闭环结束时 optimize_loop 调用 flush() 兜底。
    """

    def __init__(self, path=None, save_interval: float = SAVE_INTERVAL_SEC):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._data: dict = {}
        self._dirty = False
        self._last_save = 0.0
        self._save_interval = save_interval
        if self.path and self.path.is_file():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def get(self, key: str):
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._data[key] = value
            if self.path:
                self._maybe_save_locked()

    def _maybe_save_locked(self) -> None:
        if time.monotonic() - self._last_save >= self._save_interval:
            self._save_locked()
        else:
            self._dirty = True

    def flush(self) -> None:
        """兜底落盘：run 结束/进程退出前调用，把未落盘的 dirty 数据写下去。"""
        if not self.path:
            return
        with self._lock:
            if self._dirty:
                self._save_locked()

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.path)
        self._dirty = False
        self._last_save = time.monotonic()


def fix_cache_key(finding_ids, file_sha1: str, backend: str, model: str) -> str:
    """缓存键：必须含 finding ids + 修复前文件 sha1 + 后端名 + 模型名 + prompt 版本。"""
    ids_hash = hashlib.sha256(
        ",".join(sorted(finding_ids)).encode("utf-8")).hexdigest()[:16]
    return f"{ids_hash}|{file_sha1}|{backend}|{model}|v{PROMPT_VERSION}"


def file_sha1(content: str) -> str:
    return hashlib.sha1(content.encode("utf-8")).hexdigest()


def group_by_file(findings) -> dict[str, list]:
    """按文件分组，组内按严重度排序。"""
    groups: dict[str, list] = {}
    for f in findings:
        groups.setdefault(f.file_path, []).append(f)
    for fs in groups.values():
        fs.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
    return groups


def render_fix_prompt(file_path: str, findings, code: str,
                      feedback: dict | None = None,
                      keep: list[str] | None = None) -> str:
    """template 模式任务书：零模型调用、结果可预测。"""
    lines = [
        f"# 修复任务：{file_path}",
        "",
        "请修复下面列出的问题。规则：",
        "1. 只修复列出的问题，禁止重构、改名、格式化无关代码",
        "2. 保持公共接口不变（函数名、参数、返回值），除非该接口本身就是问题",
        "3. 修完代码必须能正常运行，不许引入新的 import 错误或语法错误",
        "",
    ]
    if keep:
        lines += ["## ⚠️ 本文件已修好的问题（请勿破坏这些修法）", "",
                  *[f"- {t}" for t in keep], ""]
    lines += [f"## 问题清单（共 {len(findings)} 条）", ""]
    for f in findings:
        lines += [
            f"### {f.id} [{f.severity}] {f.title}",
            f"- 分类：{f.category}",
            f"- 位置：第 {f.line_start}-{f.line_end} 行",
            f"- 问题：{f.description}",
            f"- 证据：\n```\n{f.evidence}\n```",
            f"- 建议修法：{f.suggestion}",
        ]
        if feedback and f.id in feedback:
            lines += [f"- ⚠️ 上一轮修复被复查判为失败：{feedback[f.id]}",
                      "  请分析失败原因，换用真正有效的修法，不要交同样的答案"]
        lines.append("")
    lines += ["## 当前文件全文", "", f"```\n{code}\n```"]
    return "\n".join(lines)


def optimize_loop(run_dir, copy_root, findings, state, fixer,
                  review_client=None,
                  max_rounds: int = 3,
                  verify_mode: str = "llm",
                  build_cmd: str = "ruff check",
                  build_timeout: int = 60,
                  cache=None,
                  log=None) -> dict:
    """迭代主循环：修 → 复查 → 仍存在的进下一轮。返回最终汇总 + 轮次历史。

    fixer          修改器（api / opencode 后端）
    review_client  llm 复查模式的模型 client（build 模式可省）
    cache          FixCache 实例（默认新建内存缓存）
    log            可选日志回调（默认静默）
    """
    run_dir = Path(run_dir)
    copy_root = Path(copy_root)
    cache = cache if cache is not None else FixCache()
    log = log if log is not None else (lambda s: None)
    history: list[dict] = []
    prev_tree: dict | None = None
    final: dict = {}

    backend = getattr(fixer, "backend", "api")
    model = getattr(fixer, "model", "") or ""
    remaining = {f.id for f in findings}

    for round_no in range(1, max_rounds + 1):
        # 防回归清单：已 verified 的 finding 提醒修改器别破坏
        keep: dict[str, list[str]] = {}
        for f in findings:
            rec = state.data["findings"].get(f.id, {})
            if rec.get("status") == "verified":
                keep.setdefault(f.file_path, []).append(f"{f.id} {f.title}")

        feedback = {fid: state.data["findings"][fid].get("note", "")
                    for fid in remaining}
        to_fix = [f for f in findings if f.id in remaining]
        round_files: list[str] = []
        prompts_dir = run_dir / "prompts" / f"round{round_no}"
        prompts_dir.mkdir(parents=True, exist_ok=True)

        # ---------- 本轮：任务书 -> 修改（带修复缓存） ----------
        for file_path, fs in group_by_file(to_fix).items():
            target = copy_root / file_path
            if not target.is_file():
                for f in fs:
                    state.set_finding_status(f.id, "failed", "副本中文件不存在")
                log(f"[第{round_no}轮] ⛔ {file_path} 不存在，跳过")
                continue

            code = target.read_text(encoding="utf-8", errors="replace")
            key = fix_cache_key([f.id for f in fs], file_sha1(code), backend, model)
            prompt = render_fix_prompt(file_path, fs, code, feedback,
                                       keep.get(file_path))
            safe = file_path.replace("/", "__").replace("\\", "__")
            prompt_file = prompts_dir / f"{safe}.task.md"
            prompt_file.write_text(prompt, encoding="utf-8")
            task = FixTask(file_path=file_path, findings=fs,
                           prompt_text=prompt, prompt_file=str(prompt_file))

            hit = cache.get(key)
            if hit is not None:
                if hit.get("ok"):
                    if hit.get("code") is not None:
                        target.write_text(hit["code"], encoding="utf-8")
                    for f in fs:
                        state.set_finding_status(f.id, "fixed", "修复缓存命中")
                    log(f"[第{round_no}轮] 🔁 {file_path} 修复缓存命中")
                else:
                    for f in fs:
                        state.set_finding_status(
                            f.id, "failed", hit.get("note") or "缓存命中：上次修复失败")
                    log(f"[第{round_no}轮] ⛔ {file_path} 缓存命中失败结果，跳过")
            else:
                ok = fixer.apply(task)
                if ok:
                    new_code = target.read_text(encoding="utf-8", errors="replace")
                    cache.put(key, {"ok": True, "code": new_code})
                    log(f"[第{round_no}轮] ✅ {file_path} 修复写回副本")
                else:
                    note = ""
                    try:
                        note = state.data["findings"][fs[0].id].get("note", "修复失败")
                    except (KeyError, AttributeError):
                        note = "修复失败"
                    cache.put(key, {"ok": False, "note": note})
                    log(f"[第{round_no}轮] ❌ {file_path} 修复失败")
            round_files.append(file_path)

        # ---------- 本轮：复查 ----------
        summary = verify_fixes(
            run_dir, copy_root, review_client, state, findings,
            round_files=set(round_files) or None,
            mode=verify_mode, build_cmd=build_cmd, build_timeout=build_timeout,
        )
        history.append({"round": round_no,
                        **{k: len(v) for k, v in summary.items()
                           if isinstance(v, list)}})
        final = summary

        remaining = set(state.findings_by_status("remaining"))
        log(f"[第{round_no}轮] 结果：✅ {len(summary['verified'])} 修好 · "
            f"❌ {len(remaining)} 仍在 · ⛔ {len(summary['failed'])} 改砸")
        if not remaining:
            log("  全部修好，收工！")
            break

        # ---------- 停滞检测 ----------
        # 本轮修复后 hash_tree 与上轮完全相同 = 修改器没动任何东西，提前停
        tree = hash_tree(copy_root)
        if prev_tree is not None and tree == prev_tree:
            final["stuck"] = True
            log("  停滞检测触发：两轮 hash_tree 完全相同，修改器卡住了，停下。")
            break
        prev_tree = tree

    cache.flush()  # 兜底落盘：节流后未落盘的修复缓存，run 结束前写盘
    final["rounds"] = len(history)
    final["history"] = history
    return final
