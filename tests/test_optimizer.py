"""Tests for the optimizer package: copier, opt_state, fixer compile gate,
opencode failure handling, and loop stuck detection / fix cache.

No network, no real opencode — fake clients and monkeypatched subprocess only.
"""

import argparse
import json
import os
import subprocess
import types
from pathlib import Path

import pytest

from lra.optimizer.copier import create_workspace, hash_tree
from lra.optimizer.fixer import (ApiFixer, FixTask, OpencodeFixer,
                                 extract_code_block, make_fixer, python_compiles)
from lra.optimizer.loop import (FixCache, PROMPT_VERSION, file_sha1,
                                fix_cache_key, optimize_loop)
from lra.optimizer.opt_state import OptState
from lra.optimizer.verifier import verify_fixes
from lra.schemas.finding import Finding


def _finding(fid="F1", file_path="src/a.py"):
    return Finding(
        id=fid, category="best_practice", severity="high",
        file_path=file_path, line_start=1, line_end=3,
        title="unused import", description="import os is unused",
        evidence="import os", suggestion="remove it", confidence=0.9,
    )


class FakeClient:
    """最小假 LLM client：config.model 供缓存键取模型名，chat 返回固定回复。"""

    def __init__(self, reply: str = "", model: str = "fake-model"):
        self.reply = reply
        self.config = type("Cfg", (), {"model": model})()
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        return self.reply


# ---------- copier ----------

def test_create_workspace_copies_and_ignores(tmp_path):
    target = tmp_path / "proj"
    (target / "src").mkdir(parents=True)
    (target / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (target / "keep.txt").write_text("keep me\n", encoding="utf-8")
    for d in (".git", ".venv", "__pycache__", ".pytest_cache",
              "node_modules", "runs", ".idea"):
        (target / d).mkdir(parents=True, exist_ok=True)
    (target / ".git" / "config").write_text("ignored", encoding="utf-8")
    (target / ".venv" / "pyvenv.cfg").write_text("ignored", encoding="utf-8")
    (target / "__pycache__" / "a.cpython-311.pyc").write_text("ignored", encoding="utf-8")
    (target / ".pytest_cache" / "README.md").write_text("ignored", encoding="utf-8")
    (target / "node_modules" / "x").mkdir(parents=True)
    (target / "node_modules" / "x" / "index.js").write_text("ignored", encoding="utf-8")
    (target / "runs" / "old").mkdir(parents=True)
    (target / "runs" / "old" / "log.txt").write_text("ignored", encoding="utf-8")
    (target / ".idea" / "workspace.xml").write_text("ignored", encoding="utf-8")

    run_dir = tmp_path / "runs" / "run1"
    copy_root = create_workspace(target, run_dir)
    assert copy_root.is_dir()
    assert (copy_root / "src" / "a.py").is_file()
    assert (copy_root / "keep.txt").is_file()
    for rel in (".git/config", ".venv/pyvenv.cfg", "__pycache__/a.cpython-311.pyc",
                ".pytest_cache/README.md", "node_modules/x/index.js",
                "runs/old/log.txt", ".idea/workspace.xml"):
        assert not (copy_root / rel).exists(), f"{rel} 不应被复制"

    # 副本里的内容与源一致
    assert (copy_root / "src" / "a.py").read_text(encoding="utf-8") == "x = 1\n"


def test_create_workspace_replaces_existing_copy(tmp_path):
    target = tmp_path / "proj"
    target.mkdir()
    (target / "a.py").write_text("x = 1\n", encoding="utf-8")
    run_dir = tmp_path / "runs"
    copy_root = create_workspace(target, run_dir)
    stale = copy_root / "stale.txt"
    stale.write_text("old", encoding="utf-8")
    copy_root = create_workspace(target, run_dir)  # 重建副本
    assert copy_root.is_dir()
    assert not stale.exists()
    assert (copy_root / "a.py").is_file()


def test_hash_tree_hashes_and_ignores(tmp_path):
    root = tmp_path / "t"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_text("bin", encoding="utf-8")
    (root / "keep.py").write_text("a=1\n", encoding="utf-8")
    h = hash_tree(root)
    assert set(h) == {"keep.py"}
    assert len(h["keep.py"]) == 64  # sha256 hex
    # 幂等
    assert h == hash_tree(root)


# ---------- opt_state ----------

def test_opt_state_persistence_roundtrip(tmp_path):
    f = _finding()
    st = OptState("target", "copy")
    st.register_findings([f])
    assert st.data["findings"]["F1"]["status"] == "pending"
    assert st.findings_by_status("pending") == ["F1"]

    st.set_finding_status("F1", "fixed", "已修复")
    assert st.data["findings"]["F1"]["status"] == "fixed"
    assert st.data["findings"]["F1"]["note"] == "已修复"

    path = tmp_path / "opt_state.json"
    st.save(path)
    st2 = OptState.load(path)
    assert st2.data == st.data
    assert st2.data["findings"]["F1"]["note"] == "已修复"
    assert st2.findings_by_status("fixed") == ["F1"]


def test_opt_state_rejects_bad_status_and_unknown_id():
    st = OptState("t", "c")
    st.register_findings([_finding()])
    with pytest.raises(ValueError):
        st.set_finding_status("F1", "bogus")
    with pytest.raises(KeyError):
        st.set_finding_status("NOPE", "fixed")


# ---------- fixer ----------

def test_extract_code_block_and_compile_gate_helpers():
    assert python_compiles("x = 1\n")
    assert not python_compiles("def broken(:\n    pass\n")
    assert extract_code_block("```python\nprint(1)\n```") == "print(1)\n"
    # 非 python 围栏走退化分支
    assert extract_code_block("```javascript\nvar x = 1;\n```") == "var x = 1;\n"
    assert extract_code_block("没有代码块") is None


def test_api_fixer_compile_gate(tmp_path):
    copy_root = tmp_path / "copy"
    (copy_root / "src").mkdir(parents=True)
    target = copy_root / "src" / "a.py"
    target.write_text("print('old')\n", encoding="utf-8")
    st = OptState(str(tmp_path), str(copy_root))
    st.register_findings([_finding(file_path="src/a.py")])
    finding = _finding(file_path="src/a.py")

    # 合法代码块 → 写回 + fixed
    good = ApiFixer(FakeClient("```python\nprint('fixed')\n```"), copy_root, state=st)
    assert good.apply(FixTask("src/a.py", [finding], "task"))
    assert target.read_text(encoding="utf-8").strip() == "print('fixed')"
    assert st.data["findings"]["F1"]["status"] == "fixed"

    # 语法不过 → 不写回 + failed（compile 闸门）
    bad = ApiFixer(FakeClient("```python\ndef broken(:\n    pass\n```"), copy_root, state=st)
    assert not bad.apply(FixTask("src/a.py", [finding], "task"))
    assert target.read_text(encoding="utf-8").strip() == "print('fixed')"  # 文件没动
    assert st.data["findings"]["F1"]["status"] == "failed"

    # 没有代码块 → 失败
    no_block = ApiFixer(FakeClient("抱歉，我修不了"), copy_root, state=st)
    assert not no_block.apply(FixTask("src/a.py", [finding], "task"))
    assert st.data["findings"]["F1"]["status"] == "failed"


def test_api_fixer_skips_compile_gate_for_non_py(tmp_path):
    copy_root = tmp_path / "copy"
    (copy_root / "src").mkdir(parents=True)
    target = copy_root / "src" / "a.js"
    target.write_text("var x = 1;\n", encoding="utf-8")
    st = OptState(str(tmp_path), str(copy_root))
    st.register_findings([_finding(file_path="src/a.js")])
    # 非 python 文件：js 围栏即可，不跑 compile 闸门
    fx = ApiFixer(FakeClient("```javascript\nvar y = 2;\n```"), copy_root, state=st)
    assert fx.apply(FixTask("src/a.js", [_finding(file_path="src/a.js")], "task"))
    assert "var y = 2;" in target.read_text(encoding="utf-8")


def test_opencode_fixer_timeout_nonzero_and_ok(monkeypatch, tmp_path):
    copy_root = tmp_path / "copy"
    (copy_root / "src").mkdir(parents=True)
    (copy_root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    st = OptState(str(tmp_path), str(copy_root))
    st.register_findings([_finding(file_path="src/a.py")])
    finding = _finding(file_path="src/a.py")

    def _timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv[0], kw.get("timeout", 600))
    monkeypatch.setattr(subprocess, "run", _timeout)
    fx = OpencodeFixer(copy_root, timeout=5, state=st)
    assert not fx.apply(FixTask("src/a.py", [finding], "task"))
    assert st.data["findings"]["F1"]["status"] == "failed"

    def _nonzero(argv, **kw):
        class P:
            returncode = 3
            stdout = ""
            stderr = "boom"
        return P()
    monkeypatch.setattr(subprocess, "run", _nonzero)
    fx2 = OpencodeFixer(copy_root, timeout=5, state=st)
    assert not fx2.apply(FixTask("src/a.py", [finding], "task"))
    assert st.data["findings"]["F1"]["status"] == "failed"

    def _ok(argv, **kw):
        (copy_root / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()
    monkeypatch.setattr(subprocess, "run", _ok)
    fx3 = OpencodeFixer(copy_root, timeout=5, state=st)
    assert fx3.apply(FixTask("src/a.py", [finding], "task"))
    assert st.data["findings"]["F1"]["status"] == "fixed"
    assert (copy_root / "src" / "a.py").read_text(encoding="utf-8") == "x = 2\n"


def test_make_fixer_factory():
    with pytest.raises(ValueError):
        make_fixer("nope", ".")
    with pytest.raises(ValueError):
        make_fixer("api", ".")  # api 必须带 client
    assert make_fixer("api", ".", client=FakeClient()).backend == "api"
    assert make_fixer("opencode", ".").backend == "opencode"
    assert make_fixer("api", ".", client=FakeClient()).model == "fake-model"


# ---------- verifier ----------

def test_verify_fixes_llm_mode(tmp_path):
    copy_root = tmp_path / "copy"
    (copy_root / "src").mkdir(parents=True)
    (copy_root / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    findings = [_finding(file_path="src/a.py")]
    st = OptState(str(tmp_path), str(copy_root))
    st.register_findings(findings)
    st.set_finding_status("F1", "fixed")

    client = FakeClient('{"verdicts": [{"finding_id": "F1", "still_exists": false, '
                        '"reason": "gone"}]}')
    summary = verify_fixes(tmp_path / "run", copy_root, client, st, findings,
                           round_files={"src/a.py"})
    assert summary["verified"] == ["F1"]
    assert st.data["findings"]["F1"]["status"] == "verified"
    assert (tmp_path / "run" / "verification.md").is_file()
    assert (tmp_path / "run" / "opt_state.json").is_file()


def test_verify_fixes_llm_mode_syntax_gate(tmp_path):
    copy_root = tmp_path / "copy"
    (copy_root / "src").mkdir(parents=True)
    (copy_root / "src" / "a.py").write_text("def broken(:\n", encoding="utf-8")
    findings = [_finding(file_path="src/a.py")]
    st = OptState(str(tmp_path), str(copy_root))
    st.register_findings(findings)
    st.set_finding_status("F1", "fixed")

    client = FakeClient('{"verdicts": []}')  # 不应被调用
    summary = verify_fixes(tmp_path / "run", copy_root, client, st, findings,
                           round_files={"src/a.py"})
    assert summary["failed"] == ["F1"]
    assert client.calls == 0  # 语法闸门拦截，零 token


def test_verify_fixes_build_mode_pass(tmp_path, monkeypatch):
    copy_root = tmp_path / "copy"
    (copy_root / "src").mkdir(parents=True)
    (copy_root / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    findings = [_finding(file_path="src/a.py")]
    st = OptState(str(tmp_path), str(copy_root))
    st.register_findings(findings)
    st.set_finding_status("F1", "fixed")

    from lra.optimizer import verifier as v
    monkeypatch.setattr(v, "run_build_check",
                        lambda *a, **k: v.BuildCheckResult(passed=True, command="ruff check"))
    summary = verify_fixes(tmp_path / "run", copy_root, None, st, findings,
                           round_files={"src/a.py"}, mode="build")
    assert summary["verified"] == ["F1"]
    assert st.data["findings"]["F1"]["status"] == "verified"
    assert summary["build_check"]["passed"] is True


def test_verify_fixes_build_mode_fail(tmp_path, monkeypatch):
    copy_root = tmp_path / "copy"
    (copy_root / "src").mkdir(parents=True)
    (copy_root / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    findings = [_finding(file_path="src/a.py")]
    st = OptState(str(tmp_path), str(copy_root))
    st.register_findings(findings)
    st.set_finding_status("F1", "fixed")

    from lra.optimizer import verifier as v
    monkeypatch.setattr(v, "run_build_check",
                        lambda *a, **k: v.BuildCheckResult(passed=False, command="ruff check",
                                                           output="E501 line too long"))
    summary = verify_fixes(tmp_path / "run", copy_root, None, st, findings,
                           round_files={"src/a.py"}, mode="build")
    assert summary["remaining"] == ["F1"]
    assert st.data["findings"]["F1"]["status"] == "remaining"


def test_run_build_check_skips_missing_command(tmp_path):
    from lra.optimizer.verifier import run_build_check
    result = run_build_check(tmp_path, command="definitely-not-a-real-cmd-xyz")
    assert result.skipped and result.passed  # 命令不存在 → skipped，不算失败


def test_verify_fixes_build_mode_does_not_verify_security(tmp_path, monkeypatch):
    copy_root = tmp_path / "copy"
    (copy_root / "src").mkdir(parents=True)
    (copy_root / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    findings = [Finding(
        id="F1", category="security", severity="high",
        file_path="src/a.py", line_start=1, line_end=1,
        title="SQL 注入", description="拼接 SQL", evidence="sql",
        suggestion="参数化", confidence=0.9,
    )]
    st = OptState(str(tmp_path), str(copy_root))
    st.register_findings(findings)
    st.set_finding_status("F1", "fixed")

    from lra.optimizer import verifier as v
    monkeypatch.setattr(v, "run_build_check",
                        lambda *a, **k: v.BuildCheckResult(passed=True, command="ruff check"))
    summary = verify_fixes(tmp_path / "run", copy_root, None, st, findings,
                           round_files={"src/a.py"}, mode="build")
    # build 无法验证语义正确性 → security 不标 verified，改标 remaining + 附注
    assert summary["verified"] == []
    assert summary["remaining"] == ["F1"]
    assert st.data["findings"]["F1"]["status"] == "remaining"
    assert "llm 复查" in st.data["findings"]["F1"]["note"]


def test_verify_fixes_build_mode_verifies_syntax_error(tmp_path, monkeypatch):
    copy_root = tmp_path / "copy"
    (copy_root / "src").mkdir(parents=True)
    (copy_root / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    findings = [Finding(
        id="F1", category="correctness", severity="critical",
        file_path="src/a.py", line_start=1, line_end=1,
        title="语法解析失败", description="bad syntax", evidence="",
        suggestion="fix", confidence=1.0,
    )]
    st = OptState(str(tmp_path), str(copy_root))
    st.register_findings(findings)
    st.set_finding_status("F1", "fixed")

    from lra.optimizer import verifier as v
    monkeypatch.setattr(v, "run_build_check",
                        lambda *a, **k: v.BuildCheckResult(passed=True, command="ruff check"))
    summary = verify_fixes(tmp_path / "run", copy_root, None, st, findings,
                           round_files={"src/a.py"}, mode="build")
    # 语法错误类 finding 可被 build 通过确认
    assert summary["verified"] == ["F1"]
    assert st.data["findings"]["F1"]["status"] == "verified"


# ---------- loop ----------

def test_fix_cache_key_contains_model_and_prompt_version():
    k1 = fix_cache_key(["F1", "F2"], "sha1abc", "api", "model-a")
    k2 = fix_cache_key(["F1", "F2"], "sha1abc", "api", "model-b")
    k3 = fix_cache_key(["F2", "F1"], "sha1abc", "api", "model-a")
    assert k1 != k2          # 模型不同 → 键不同
    assert k1 == k3          # finding 顺序无关
    assert f"v{PROMPT_VERSION}" in k1
    assert "api" in k1 and "model-a" in k1 and "sha1abc" in k1
    assert fix_cache_key(["F1"], "sha", "opencode", "") != k1


def test_fix_cache_throttles_disk_writes(tmp_path, monkeypatch):
    cache = FixCache(tmp_path / "fix.json")
    saves = {"n": 0}
    original = cache._save_locked

    def counting_save():
        saves["n"] += 1
        original()

    monkeypatch.setattr(cache, "_save_locked", counting_save)
    for i in range(50):
        cache.put(f"k{i}", {"ok": True, "code": "x"})
    cache.flush()
    # 50 次 put 只有寥寥几次落盘，远小于 put 次数
    assert 1 <= saves["n"] <= 3
    # 内存里数据全在
    assert cache.get("k0") == {"ok": True, "code": "x"}
    assert cache.get("k49") == {"ok": True, "code": "x"}


def test_loop_stuck_detection(tmp_path):
    copy_root = tmp_path / "copy"
    (copy_root / "src").mkdir(parents=True)
    target = copy_root / "src" / "a.py"
    target.write_text("print('bad')\n", encoding="utf-8")
    findings = [_finding(file_path="src/a.py")]
    st = OptState(str(tmp_path), str(copy_root))
    st.register_findings(findings)
    run_dir = tmp_path / "run"

    class StuckFixer:
        """每次写同样的内容 → 两轮 hash_tree 相同 → 停滞。"""
        backend = "api"
        model = "fake-model"

        def __init__(self, copy_root, state):
            self.copy_root = copy_root

        def apply(self, task):
            (self.copy_root / task.file_path).write_text(
                "print('same')\n", encoding="utf-8")
            return True

    class AlwaysStillClient:
        config = type("Cfg", (), {"model": "fake-model"})()

        def chat(self, messages, **kwargs):
            return '{"verdicts": [{"finding_id": "F1", "still_exists": true, ' \
                   '"reason": "still there"}]}'

    result = optimize_loop(run_dir, copy_root, findings, st,
                           StuckFixer(copy_root, st),
                           review_client=AlwaysStillClient(),
                           max_rounds=3)
    assert result["stuck"] is True
    assert result["rounds"] == 2  # 第 2 轮 hash 相同即停
    assert st.data["findings"]["F1"]["status"] == "remaining"
    assert target.read_text(encoding="utf-8").strip() == "print('same')"


def test_loop_fix_cache_hit_skips_fixer(tmp_path):
    copy_root = tmp_path / "copy"
    (copy_root / "src").mkdir(parents=True)
    target = copy_root / "src" / "a.py"
    target.write_text("print('bad')\n", encoding="utf-8")
    findings = [_finding(file_path="src/a.py")]
    st = OptState(str(tmp_path), str(copy_root))
    st.register_findings(findings)
    run_dir = tmp_path / "run"
    calls = {"n": 0}

    class CountingFixer:
        backend = "api"
        model = "fake-model"

        def __init__(self, copy_root, state):
            self.copy_root = copy_root

        def apply(self, task):
            calls["n"] += 1
            (self.copy_root / task.file_path).write_text(
                "print('ok')\n", encoding="utf-8")
            return True

    class FixedClient:
        config = type("Cfg", (), {"model": "fake-model"})()

        def chat(self, messages, **kwargs):
            return '{"verdicts": [{"finding_id": "F1", "still_exists": false, ' \
                   '"reason": "gone"}]}'

    # 预置缓存：键 = 当前内容 sha1 + 后端 + 模型 + prompt 版本
    key = fix_cache_key(["F1"], file_sha1(target.read_text(encoding="utf-8")),
                        "api", "fake-model")
    cache = FixCache()
    cache.put(key, {"ok": True, "code": "print('from-cache')\n"})

    result = optimize_loop(run_dir, copy_root, findings, st,
                           CountingFixer(copy_root, st),
                           review_client=FixedClient(),
                           max_rounds=1, cache=cache)
    assert calls["n"] == 0  # 缓存命中，fixer 一次都没被调
    assert target.read_text(encoding="utf-8").strip() == "print('from-cache')"
    assert result["verified"] == ["F1"]
    assert st.data["findings"]["F1"]["status"] == "verified"


def test_loop_success_rounds(tmp_path):
    copy_root = tmp_path / "copy"
    (copy_root / "src").mkdir(parents=True)
    target = copy_root / "src" / "a.py"
    target.write_text("print('bad')\n", encoding="utf-8")
    findings = [_finding(file_path="src/a.py")]
    st = OptState(str(tmp_path), str(copy_root))
    st.register_findings(findings)
    run_dir = tmp_path / "run"

    class GoodFixer:
        backend = "api"
        model = "fake-model"

        def __init__(self, copy_root, state):
            self.copy_root = copy_root

        def apply(self, task):
            (self.copy_root / task.file_path).write_text(
                "print('fixed-good')\n", encoding="utf-8")
            return True

    class FixedClient:
        config = type("Cfg", (), {"model": "fake-model"})()

        def chat(self, messages, **kwargs):
            return '{"verdicts": [{"finding_id": "F1", "still_exists": false, ' \
                   '"reason": "gone"}]}'

    result = optimize_loop(run_dir, copy_root, findings, st,
                           GoodFixer(copy_root, st),
                           review_client=FixedClient(),
                           max_rounds=3)
    assert "stuck" not in result
    assert result["rounds"] == 1  # 一轮全清
    assert result["verified"] == ["F1"]
    assert st.findings_by_status("verified") == ["F1"]


# ---------- 断点续跑 ----------

def test_loop_resume_skips_verified_findings(tmp_path):
    """断点续跑：上次已 verified 的 finding 不重修，只修 remaining。"""
    copy_root = tmp_path / "copy"
    (copy_root / "src").mkdir(parents=True)
    (copy_root / "src" / "a.py").write_text("print('bad')\n", encoding="utf-8")
    (copy_root / "src" / "b.py").write_text("print('bad')\n", encoding="utf-8")
    findings = [
        _finding(fid="F1", file_path="src/a.py"),
        _finding(fid="F2", file_path="src/b.py"),
    ]
    st = OptState(str(tmp_path), str(copy_root))
    st.register_findings(findings)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # --- 首次跑：F1 修好 verified，F2 复查仍在 remaining（模拟中断点） ---
    class Phase1Fixer:
        backend = "api"
        model = "fake-model"

        def __init__(self, copy_root, state):
            self.copy_root = copy_root

        def apply(self, task):
            (self.copy_root / task.file_path).write_text(
                "print('fixed')\n", encoding="utf-8")
            return True

    class Phase1Client:
        config = type("Cfg", (), {"model": "fake-model"})()

        def chat(self, messages, **kwargs):
            return ('{"verdicts": ['
                    '{"finding_id": "F1", "still_exists": false, "reason": "gone"},'
                    '{"finding_id": "F2", "still_exists": true, "reason": "still"}'
                    ']}')

    optimize_loop(run_dir, copy_root, findings, st,
                  Phase1Fixer(copy_root, st), review_client=Phase1Client(),
                  max_rounds=1)
    assert st.data["findings"]["F1"]["status"] == "verified"
    assert st.data["findings"]["F2"]["status"] == "remaining"

    # 落盘状态，模拟进程中断重启
    state_path = run_dir / "opt_state.json"
    st.save(state_path)
    st2 = OptState.load(state_path)

    # --- 续跑：只修 F2 所在文件，F1 的文件不再被 fixer 触碰 ---
    touched = []

    class Phase2Fixer:
        backend = "api"
        model = "fake-model"

        def __init__(self, copy_root, state):
            self.copy_root = copy_root

        def apply(self, task):
            touched.append(task.file_path)
            (self.copy_root / task.file_path).write_text(
                "print('fixed2')\n", encoding="utf-8")
            return True

    class Phase2Client:
        config = type("Cfg", (), {"model": "fake-model"})()

        def chat(self, messages, **kwargs):
            return ('{"verdicts": ['
                    '{"finding_id": "F2", "still_exists": false, "reason": "gone"}'
                    ']}')

    optimize_loop(run_dir, copy_root, findings, st2,
                  Phase2Fixer(copy_root, st2), review_client=Phase2Client(),
                  max_rounds=1)
    assert touched == ["src/b.py"]  # 只修 remaining 的文件，verified 的不重修
    assert st2.data["findings"]["F1"]["status"] == "verified"
    assert st2.data["findings"]["F2"]["status"] == "verified"
    # F1 的文件内容没有被二次修改
    assert (copy_root / "src" / "a.py").read_text(encoding="utf-8") == "print('fixed')\n"


def test_cmd_optimize_resume_reuses_copy_and_state(tmp_path, monkeypatch):
    """cmd_optimize 续跑分支：不重建副本、加载旧状态、传持久化 FixCache。"""
    import lra.__main__ as main

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    findings = [_finding(fid="F1", file_path="src/a.py")]
    (run_dir / "findings.json").write_text(
        json.dumps([f.model_dump(mode="json") for f in findings],
                   ensure_ascii=False), encoding="utf-8")

    # 上次跑过：副本已存在，F1 已 verified
    copy_root = run_dir / "optimized_copy"
    (copy_root / "src").mkdir(parents=True)
    (copy_root / "src" / "a.py").write_text("print('fixed')\n", encoding="utf-8")
    st = OptState(str(tmp_path), str(copy_root))
    st.register_findings(findings)
    st.set_finding_status("F1", "verified", "done")
    st.save(run_dir / "opt_state.json")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "default_profile: x\nprofiles:\n  x:\n    model: m\n"
        "    base_url: http://127.0.0.1\n", encoding="utf-8")

    calls = {}

    def fake_create_workspace(target_root, run_dir_):
        calls["create_workspace"] = True
        return Path(run_dir_) / "optimized_copy"

    class FakeLLM:
        @classmethod
        def from_config(cls, path, profile=None):
            return FakeClient()

    def fake_make_fixer(**kwargs):
        return type("F", (), {"backend": "api", "model": "fake-model"})()

    def fake_optimize_loop(**kwargs):
        calls["optimize_loop_kwargs"] = kwargs
        return {"rounds": 0, "verified": [], "remaining": [], "failed": []}

    monkeypatch.setattr(main, "create_workspace", fake_create_workspace)
    monkeypatch.setattr(main, "LLMClient", FakeLLM)
    monkeypatch.setattr(main, "make_fixer", fake_make_fixer)
    monkeypatch.setattr(main, "optimize_loop", fake_optimize_loop)

    args = argparse.Namespace(run_dir=str(run_dir), path=str(tmp_path),
                              config=str(config_path), profile=None,
                              backend=None, max_rounds=3, verify="llm",
                              build_cmd="ruff check", issue_hint=None)
    rc = main.cmd_optimize(args)
    assert rc == 0
    assert "create_workspace" not in calls  # 续跑不重建副本
    kw = calls["optimize_loop_kwargs"]
    assert kw["copy_root"] == run_dir / "optimized_copy"
    assert kw["state"].data["findings"]["F1"]["status"] == "verified"
    assert kw["cache"].path == run_dir / "fix_cache.json"


def test_cmd_optimize_first_run_creates_workspace_and_registers(tmp_path, monkeypatch):
    """cmd_optimize 首次分支：建副本、注册 findings（pending）、传持久化 FixCache。"""
    import lra.__main__ as main

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    findings = [_finding(fid="F1", file_path="src/a.py")]
    (run_dir / "findings.json").write_text(
        json.dumps([f.model_dump(mode="json") for f in findings],
                   ensure_ascii=False), encoding="utf-8")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "default_profile: x\nprofiles:\n  x:\n    model: m\n"
        "    base_url: http://127.0.0.1\n", encoding="utf-8")

    calls = {}

    def fake_create_workspace(target_root, run_dir_):
        calls["create_workspace"] = True
        p = Path(run_dir_) / "optimized_copy"
        p.mkdir(parents=True, exist_ok=True)
        return p

    class FakeLLM:
        @classmethod
        def from_config(cls, path, profile=None):
            return FakeClient()

    def fake_make_fixer(**kwargs):
        return type("F", (), {"backend": "api", "model": "fake-model"})()

    def fake_optimize_loop(**kwargs):
        calls["optimize_loop_kwargs"] = kwargs
        return {"rounds": 0, "verified": [], "remaining": [], "failed": []}

    monkeypatch.setattr(main, "create_workspace", fake_create_workspace)
    monkeypatch.setattr(main, "LLMClient", FakeLLM)
    monkeypatch.setattr(main, "make_fixer", fake_make_fixer)
    monkeypatch.setattr(main, "optimize_loop", fake_optimize_loop)

    args = argparse.Namespace(run_dir=str(run_dir), path=str(tmp_path),
                              config=str(config_path), profile=None,
                              backend=None, max_rounds=3, verify="llm",
                              build_cmd="ruff check", issue_hint=None)
    rc = main.cmd_optimize(args)
    assert rc == 0
    assert calls["create_workspace"] is True  # 首次才建副本
    kw = calls["optimize_loop_kwargs"]
    assert kw["state"].data["findings"]["F1"]["status"] == "pending"
    assert kw["cache"].path == run_dir / "fix_cache.json"
    assert (run_dir / "opt_state.json").is_file()  # 首次已落盘


# ---------- 第三轮评审回归：build 放行 / fixer 空块 / 续跑漂移 / cmd 193 ----------

def test_build_can_verify_whitelist():
    """build 模式只放行语法类标题 + best_practice；performance/readability 不放行。"""
    from lra.optimizer.verifier import _build_can_verify

    # best_practice：lint 可覆盖 → 可 verify
    assert _build_can_verify(_finding(fid="F1")) is True  # category=best_practice

    # 语法类标题：即便 category=correctness 也可 verify
    assert _build_can_verify(Finding(
        id="F2", category="correctness", severity="critical",
        file_path="a.py", line_start=1, line_end=1,
        title="语法解析失败", description="", evidence="", suggestion="",
        confidence=1.0)) is True

    # performance / readability / security / correctness（非语法标题）→ 不可 verify
    for cat in ("performance", "readability", "security", "correctness"):
        f = Finding(id="F3", category=cat, severity="high",
                    file_path="a.py", line_start=1, line_end=1,
                    title="some issue", description="", evidence="",
                    suggestion="", confidence=0.9)
        assert _build_can_verify(f) is False, f"{cat} 不应被 build 放行"


def test_verify_fixes_build_mode_does_not_verify_performance(tmp_path, monkeypatch):
    """build 通过时 performance/readability 也不标 verified，改标 remaining 等 llm。"""
    copy_root = tmp_path / "copy"
    (copy_root / "src").mkdir(parents=True)
    (copy_root / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    findings = [Finding(
        id="F1", category="performance", severity="medium",
        file_path="src/a.py", line_start=1, line_end=1,
        title="低效循环", description="O(n^2)", evidence="", suggestion="",
        confidence=0.8,
    )]
    st = OptState(str(tmp_path), str(copy_root))
    st.register_findings(findings)
    st.set_finding_status("F1", "fixed")

    from lra.optimizer import verifier as v
    monkeypatch.setattr(v, "run_build_check",
                        lambda *a, **k: v.BuildCheckResult(passed=True, command="ruff check"))
    summary = verify_fixes(tmp_path / "run", copy_root, None, st, findings,
                           round_files={"src/a.py"}, mode="build")
    assert summary["verified"] == []
    assert summary["remaining"] == ["F1"]
    assert st.data["findings"]["F1"]["status"] == "remaining"
    assert "llm 复查" in st.data["findings"]["F1"]["note"]


def test_api_fixer_rejects_empty_code_block(tmp_path):
    """模型回复空代码围栏时不得清空文件：判 failed，文件保持原样。"""
    copy_root = tmp_path / "copy"
    (copy_root / "src").mkdir(parents=True)
    target = copy_root / "src" / "a.py"
    target.write_text("print('old')\n", encoding="utf-8")
    st = OptState(str(tmp_path), str(copy_root))
    st.register_findings([_finding(file_path="src/a.py")])
    finding = _finding(file_path="src/a.py")

    # 空 python 围栏：extract 返回空串，compile 闸门会放行，必须在写盘前拦下
    empty = ApiFixer(FakeClient("```python\n\n```"), copy_root, state=st)
    assert not empty.apply(FixTask("src/a.py", [finding], "task"))
    assert target.read_text(encoding="utf-8").strip() == "print('old')"  # 文件没动
    assert st.data["findings"]["F1"]["status"] == "failed"
    assert "空" in st.data["findings"]["F1"]["note"]

    # 无语言围栏的空块同样拦截
    empty2 = ApiFixer(FakeClient("```\n\n```"), copy_root, state=st)
    assert not empty2.apply(FixTask("src/a.py", [finding], "task"))
    assert target.read_text(encoding="utf-8").strip() == "print('old')"


def test_loop_resume_drifted_ids_merge_and_no_keyerror(tmp_path):
    """断点续跑 id 漂移：新 findings 含旧 opt_state 没有的 id，合并注册为 pending，
    feedback 用 .get() 兜底，不再 KeyError 崩循环。"""
    copy_root = tmp_path / "copy"
    (copy_root / "src").mkdir(parents=True)
    target = copy_root / "src" / "a.py"
    target.write_text("print('bad')\n", encoding="utf-8")
    run_dir = tmp_path / "run"

    # 旧 opt_state 只认识 F1（已 verified）；新 findings 重新生成了 id → F2 是陌生 id
    st = OptState(str(tmp_path), str(copy_root))
    st.register_findings([_finding(fid="F1", file_path="src/a.py")])
    st.set_finding_status("F1", "verified", "done")
    findings = [_finding(fid="F2", file_path="src/a.py")]

    class DriftFixer:
        backend = "api"
        model = "fake-model"

        def __init__(self, copy_root, state):
            self.copy_root = copy_root

        def apply(self, task):
            (self.copy_root / task.file_path).write_text(
                "print('fixed')\n", encoding="utf-8")
            return True

    class DriftClient:
        config = type("Cfg", (), {"model": "fake-model"})()
        def chat(self, messages, **kwargs):
            return '{"verdicts": [{"finding_id": "F2", "still_exists": false, "reason": "gone"}]}'

    result = optimize_loop(run_dir, copy_root, findings, st,
                           DriftFixer(copy_root, st), review_client=DriftClient(),
                           max_rounds=1)
    assert result["verified"] == ["F2"]
    assert st.data["findings"]["F1"]["status"] == "verified"  # 旧记录保留
    assert st.data["findings"]["F2"]["status"] == "verified"  # 新 id 被注册并修好


def _fake_nt_os():
    """模拟 Windows 的 os 模块（name="nt"），不污染全局 os.name。

    verifier.run_build_check 只读 os.pathsep / os.environ / os.name 三个属性，
    这里用真实 pathsep/environ 拼一个轻量替身。直接 monkeypatch 全局 os.name
    会让 Linux 上的 pathlib.Path 误判为 WindowsPath 而抛 NotImplementedError。
    """
    return types.SimpleNamespace(
        name="nt", pathsep=os.pathsep, environ=os.environ,
    )


def test_run_build_check_routes_cmd_bat_through_cmd(monkeypatch, tmp_path):
    """.cmd/.bat 命令在 Windows 上走 cmd /c 转交，避免 WinError 193。"""
    import lra.optimizer.verifier as v
    monkeypatch.setattr(v, "os", _fake_nt_os())
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    monkeypatch.setattr(v.subprocess, "run", fake_run)
    monkeypatch.setattr(v.shutil, "which",
                        lambda name, path=None: "C:\\bin\\ruff.cmd")
    result = v.run_build_check(tmp_path, command="ruff check")
    assert result.passed and not result.skipped
    assert captured["argv"][:2] == ["cmd", "/c"]
    assert captured["argv"][2].lower().endswith("ruff.cmd")


def test_run_build_check_oserror_treated_as_skipped(monkeypatch, tmp_path):
    """subprocess.run 抛 OSError（如 WinError 193）时按 skipped 处理，不崩。"""
    import lra.optimizer.verifier as v
    monkeypatch.setattr(v, "os", _fake_nt_os())

    def fake_run(argv, **kw):
        raise OSError(193, "not a valid Win32 application")
    monkeypatch.setattr(v.subprocess, "run", fake_run)
    monkeypatch.setattr(v.shutil, "which",
                        lambda name, path=None: "C:\\bin\\ruff.cmd")
    result = v.run_build_check(tmp_path, command="ruff check")
    assert result.skipped and result.passed  # OSError → skipped，不算失败
