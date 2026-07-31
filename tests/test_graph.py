"""LangGraph 版编排层的端到端测试：假模型 + 迷你项目，验证全图流转。

仿照原版 tests/test_second_review.py 的 FakeJudge 模式：
造假客户端，chat() 返回固定 JSON——不花一分钱 token，就能验证
扫描 -> 切块 -> Send 扇出 -> reducer 汇总 -> 聚合 -> 条件边 -> 终审 -> 报告
的完整流转，以及"同 thread_id 再跑 = 断点续跑"的 checkpoint 语义。

运行（在原项目的 .venv 下，LangGraph 装在那里）：
    E:\\code-review-agent\\.venv\\Scripts\\python.exe -m pytest \\
        E:\\code-review-agent-langgraph\\tests -q
"""

import json
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from lra.graph import build_graph   # import lra 即触发 cra 的路径引导

# 每种文件埋一条"逐字证据"：证据必须真实存在于对应文件
# （Aggregator 会拿它在源码里定位，编造的会被当幻觉丢掉），
# 且单行证据必须 ≥ 15 字符（聚合器的"太短不足为凭"门槛）。
# 行号统一瞎报 1-1——证据校验应该把它们纠正到真实行（第 3 行）。
EVIDENCE_BY_FILE = {
    "a.py": ('PASSWORD = "123456"  # 硬编码密码', "硬编码密码"),
    "b.py": ('API_KEY = "sk-fake"  # 硬编码密钥', "硬编码密钥"),
}


class FakeClient:
    """初审假模型：审到哪个文件，就报哪个文件里埋好的问题。

    config.model / total_tokens_used 是给 report 节点的 meta 用的
    （报告头部要写模型名和 token 消耗）。
    """

    class config:
        model = "fake-model"

    total_tokens_used = 0

    def __init__(self):
        self.total_requests = 0

    def chat(self, messages, **kw):
        self.total_requests += 1
        # user 消息第一行是 "文件路径：xxx"（cra reviewer 的固定配方）
        path = messages[-1]["content"].splitlines()[0].removeprefix("文件路径：")
        evidence, title = EVIDENCE_BY_FILE[path]
        return json.dumps({"findings": [{
            "id": "F1", "category": "security", "severity": "high",
            "file_path": path, "line_start": 1, "line_end": 1,
            "title": title, "description": "d", "evidence": evidence,
            "suggestion": "s", "confidence": 0.9}]}, ensure_ascii=False)


class FakeJudge:
    """终审假模型（FakeJudge 模式）：构造时给的 dict 就是每次复核的裁决。"""

    class config:
        model = "fake-judge"

    total_tokens_used = 0

    def __init__(self, result: dict):
        self.reply = json.dumps(result, ensure_ascii=False)
        self.calls = 0

    def chat(self, messages, **kw):
        self.calls += 1
        return self.reply


@pytest.fixture
def mini_project(tmp_path):
    """造迷你项目：a.py + b.py 两个小文件，证据都在第 3 行。"""
    (tmp_path / "a.py").write_text(
        '"""模块 a"""\n\nPASSWORD = "123456"  # 硬编码密码\n', encoding="utf-8")
    (tmp_path / "b.py").write_text(
        '"""模块 b"""\n\nAPI_KEY = "sk-fake"  # 硬编码密钥\n', encoding="utf-8")
    return tmp_path


def _run_graph(client, judge, project_root, run_dir, thread_id="t-e2e"):
    """编译 + 跑图的小助手：几个测试共用同一套动作。

    with 管理 SqliteSaver 生命周期；max_concurrency=3 对应原版的 Semaphore(3)。
    """
    builder = build_graph(client, judge)
    config = {"configurable": {"thread_id": thread_id}, "max_concurrency": 3}
    with SqliteSaver.from_conn_string(
            str(run_dir / "checkpoints.sqlite")) as saver:
        graph = builder.compile(checkpointer=saver)
        return graph.invoke(
            {"root": str(project_root), "run_dir": str(run_dir)}, config)


def test_graph_end_to_end(mini_project, tmp_path):
    """全链路：扫描 -> 切块 -> 并行审查 -> 聚合 -> 条件边走终审 -> 报告。"""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    client = FakeClient()
    # 聚合后 id 全局重排：F1 = a.py（文件名排序在前），F2 = b.py
    judge = FakeJudge({"verdicts": [
        {"finding_id": "F1", "verdict": "confirmed", "severity": None,
         "reason": "成立"},
        {"finding_id": "F2", "verdict": "rejected", "severity": None,
         "reason": "示例代码，非真实密钥"},
    ]})

    _run_graph(client, judge, mini_project, run_dir)

    # ---- 产物齐全（文件名与原版一致 + 框架账本）----
    assert (run_dir / "project_map.json").exists()
    assert (run_dir / "findings.json").exists()
    assert (run_dir / "report.md").exists()
    assert (run_dir / "checkpoints.sqlite").exists()   # 框架的账本

    # ---- 发现内容：行号被聚合器纠正到证据真实行（第 3 行）----
    saved = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert len(saved) == 2
    by_file = {d["file_path"]: d for d in saved}
    assert (by_file["a.py"]["line_start"], by_file["a.py"]["line_end"]) == (3, 3)
    assert (by_file["b.py"]["line_start"], by_file["b.py"]["line_end"]) == (3, 3)

    # ---- 裁决挂回：confirmed / rejected 都在，一条不删 ----
    assert by_file["a.py"]["second_verdict"] == "confirmed"
    assert by_file["b.py"]["second_verdict"] == "rejected"
    assert "示例代码" in by_file["b.py"]["second_reason"]

    # ---- 报告分三区：确认区 + 驳回区（学习材料）----
    md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "✅ 确认 1" in md
    assert "二级审查驳回" in md

    # ---- 并行扇出的证据：两个文件各一块，各一次初审请求 ----
    assert client.total_requests == 2
    # 终审按文件打包：两个文件两次复核请求
    assert judge.calls == 2


def test_conditional_edge_skips_second_review(mini_project, tmp_path):
    """条件边：不配终审模型时，aggregate 直达 report，报告保持单模型样式。"""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    client = FakeClient()

    # second_client=None：图上 second_review 节点根本不会被执行
    _run_graph(client, None, mini_project, run_dir, thread_id="t-nosecond")

    assert (run_dir / "report.md").exists()
    md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "二级审查" not in md          # 没跑终审，报告不出现分组区块
    assert "## 问题清单" in md            # 保持单模型报告样式

    # findings.json 里的条目没有裁决字段（没跑过终审）
    saved = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert all(d["second_verdict"] is None for d in saved)


def test_resume_via_same_thread_id(mini_project, tmp_path):
    """断点续跑的户头语义：同一 thread_id 再 invoke(None)——
    已跑完的图直接交还旧结果，零新增请求（不重复烧 token）。"""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    client = FakeClient()
    judge = FakeJudge({"verdicts": []})   # 空裁决 -> 全部落存疑，不影响本测试

    _run_graph(client, judge, mini_project, run_dir, thread_id="t-resume")
    assert client.total_requests == 2

    # 模拟"又敲了一遍同一条命令"：新开一个 SqliteSaver（同一个 sqlite 文件），
    # 同一 thread_id，invoke(None) = 从 checkpoint 恢复
    builder = build_graph(client, judge)
    config = {"configurable": {"thread_id": "t-resume"}}
    with SqliteSaver.from_conn_string(
            str(run_dir / "checkpoints.sqlite")) as saver:
        graph = builder.compile(checkpointer=saver)
        snap = graph.get_state(config)
        assert snap.values                # 账本上有账
        assert not snap.next              # 上次已跑完（没有待走的边）
        result = graph.invoke(None, config)

    # 已完成的图不重跑：初审/终审都没有新增请求
    assert client.total_requests == 2
    assert judge.calls == 2
    # 旧结果原样读回
    assert len(result["aggregated"]) == 2
