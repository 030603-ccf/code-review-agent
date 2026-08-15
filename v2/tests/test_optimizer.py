"""Tests for the optimizer package: copier, opt_state, fixer compile gate,
opencode failure handling, and loop stuck detection / fix cache.

No network, no real opencode — fake clients and monkeypatched subprocess only.
"""

import subprocess

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
