"""错误路径测试：假模型抛异常时，图的容错设计真实生效。

覆盖场景：
    - 单块瞬时失败后重试成功（TransientError -> 重试 -> 正常交卷）
    - 单块永久失败（不重试，直接放弃，其他块照常）
    - 全部块永久失败（0 条发现，报告照常生成，图不崩）
"""

import json

from test_graph import FakeClient, _run_graph   # conftest 已把 tests/ 加进 sys.path


class FlakyClient(FakeClient):
    """可配置"第 N 次调用抛什么异常"的假模型。

    failures: {调用序号: (异常类, 参数)} —— 序号从 1 开始计数。
    没在表里的调用正常返回该文件的埋点发现。
    """

    def __init__(self, failures: dict | None = None):
        super().__init__()
        self.failures = failures or {}
        self.call_count = 0

    def chat(self, messages, **kw):
        self.call_count += 1
        self.total_requests += 1
        failure = self.failures.get(self.call_count)
        if failure:
            exc_cls, arg = failure
            raise exc_cls(arg)
        return super().chat(messages, **kw)


def test_transient_failure_retries_then_succeeds(tmp_path):
    """第一次调用抛瞬时错误（ConnectionError），重试后成功 -> 发现不丢。"""
    project = tmp_path / "p"
    project.mkdir()
    (project / "a.py").write_text(
        '"""模块 a"""\n\nPASSWORD = "123456"  # 硬编码密码\n', encoding="utf-8")
    (project / "b.py").write_text(
        '"""模块 b"""\n\nAPI_KEY = "sk-fake"  # 硬编码密钥\n', encoding="utf-8")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # 第 1 次调用（a.py）抛 ConnectionError -> 分类为瞬时 -> 重试
    # 但重试前要 sleep 1s（BASE_DELAY）——测试里把重试等待打掉
    import lra.nodes as nodes_mod
    old_delay = nodes_mod.BASE_DELAY
    nodes_mod.BASE_DELAY = 0.01
    try:
        client = FlakyClient({1: (ConnectionError, "网络抖动")})
        _run_graph(client, None, project, run_dir, thread_id="t-retry")
    finally:
        nodes_mod.BASE_DELAY = old_delay

    # a.py 重试成功，b.py 正常：2 条发现都拿到
    saved = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert len(saved) == 2
    # 第 1 次失败 + 重试 1 次 = 总共 3 次调用（a.py 2 次 + b.py 1 次）
    assert client.call_count == 3


def test_permanent_failure_no_retry_other_chunks_ok(tmp_path):
    """第一次调用抛永久错误（ValueError）-> 不重试直接放弃，b.py 照常。"""
    project = tmp_path / "p"
    project.mkdir()
    (project / "a.py").write_text(
        '"""模块 a"""\n\nPASSWORD = "123456"  # 硬编码密码\n', encoding="utf-8")
    (project / "b.py").write_text(
        '"""模块 b"""\n\nAPI_KEY = "sk-fake"  # 硬编码密钥\n', encoding="utf-8")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    client = FlakyClient({1: (ValueError, "模型输出不可修复")})
    _run_graph(client, None, project, run_dir, thread_id="t-perm")

    # 只有 b.py 的发现：a.py 永久失败被放弃
    saved = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert len(saved) == 1
    assert saved[0]["file_path"] == "b.py"
    # 永久错误不重试：a.py 只调了 1 次
    assert client.call_count == 2


def test_all_chunks_fail_graph_still_completes(tmp_path):
    """全部块永久失败 -> 0 条发现，报告照常生成，图不崩。"""
    project = tmp_path / "p"
    project.mkdir()
    (project / "a.py").write_text(
        '"""模块 a"""\n\nPASSWORD = "123456"  # 硬编码密码\n', encoding="utf-8")
    (project / "b.py").write_text(
        '"""模块 b"""\n\nAPI_KEY = "sk-fake"  # 硬编码密钥\n', encoding="utf-8")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # 两个文件每次都抛永久错误
    client = FlakyClient({1: (ValueError, "x"), 2: (ValueError, "y")})
    _run_graph(client, None, project, run_dir, thread_id="t-allfail")

    saved = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert saved == []                     # 0 条发现
    assert (run_dir / "report.md").exists()   # 报告照常生成
    md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "未发现问题" in md


def test_retry_exhausted_after_transient_errors(tmp_path):
    """瞬时错误反复发生，重试耗尽后放弃（不无限重试）。"""
    project = tmp_path / "p"
    project.mkdir()
    (project / "a.py").write_text(
        '"""模块 a"""\n\nPASSWORD = "123456"  # 硬编码密码\n', encoding="utf-8")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    import lra.nodes as nodes_mod
    old_delay = nodes_mod.BASE_DELAY
    nodes_mod.BASE_DELAY = 0.01
    try:
        # 前 4 次（MAX_RETRIES+1）都抛瞬时错误 -> 重试耗尽放弃
        client = FlakyClient({i: (ConnectionError, "一直抖动")
                              for i in range(1, 5)})
        _run_graph(client, None, project, run_dir, thread_id="t-exhaust")
    finally:
        nodes_mod.BASE_DELAY = old_delay

    saved = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert saved == []                     # 放弃后 0 条发现
    assert client.call_count == 4          # 首试 + 3 次重试，没有第 5 次
    assert (run_dir / "report.md").exists()
