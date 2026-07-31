"""边界测试：图在"极端输入"下也要正常收工。

覆盖场景（都是日常跑不到、但真实会发生的事）：
    - 空项目：没有任何代码文件 -> 图走完，报告照常生成
    - 全语法错误：每个文件都解析失败 -> chunk 全跳过，work 为空，图不崩
    - 多文件扇出：20 个文件 -> 20 张 Send 工单，reducer 汇总一条不丢
"""

import json

from langgraph.checkpoint.sqlite import SqliteSaver

from lra.graph import build_graph

from test_graph import FakeClient, _run_graph   # conftest 已把 tests/ 加进 sys.path


# ==================== 空项目 ====================

def test_empty_project_graph_completes(tmp_path):
    """目录里没有可审的代码 -> 图仍然走到 END，报告照常生成。"""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    # 放一个不可解析的文件类型（不属于支持语言，等于空项目）
    (empty_dir / "notes.txt").write_text("hello", encoding="utf-8")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    client = FakeClient()
    result = _run_graph(client, None, empty_dir, run_dir,
                        thread_id="t-empty")

    # 图走完：aggregated 有值（空清单），report 落盘
    assert result["aggregated"] == []
    assert (run_dir / "report.md").exists()
    md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "未发现问题" in md
    # 没审过任何块：零请求零 token
    assert client.total_requests == 0


# ==================== 全语法错误 ====================

def test_all_parse_errors_skip_chunking(tmp_path):
    """全部文件语法错误 -> chunk 跳过所有文件，fan_out 走 ["aggregate"] 保底。"""
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    (bad_dir / "a.py").write_text("def broken(:\n  pass", encoding="utf-8")
    (bad_dir / "b.py").write_text("class {:\n  pass", encoding="utf-8")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    client = FakeClient()
    result = _run_graph(client, None, bad_dir, run_dir,
                        thread_id="t-parseerr")

    assert result["aggregated"] == []
    assert (run_dir / "project_map.json").exists()   # 索引照常生成
    assert (run_dir / "report.md").exists()          # 报告照常生成
    assert client.total_requests == 0                # 零审查请求


# ==================== 多文件扇出 ====================

class ManyFilesClient(FakeClient):
    """认识 m00.py ~ m19.py 的假模型：每个文件一条固定发现。"""

    def chat(self, messages, **kw):
        self.total_requests += 1
        path = messages[-1]["content"].splitlines()[0].removeprefix("文件路径：")
        i = int(path[1:3])                          # "m03.py" -> 3
        evidence = f'PASSWORD_{i} = "pwd-{i}"  # 硬编码密码'
        return json.dumps({"findings": [{
            "id": f"F{i}", "category": "security", "severity": "high",
            "file_path": path, "line_start": 1, "line_end": 1,
            "title": "硬编码密码", "description": "d",
            "evidence": evidence,
            "suggestion": "s", "confidence": 0.9}]}, ensure_ascii=False)


def test_many_files_fan_out_all_reviewed(tmp_path):
    """20 个文件 -> 20 张 Send 工单，全部审到，reducer 汇总不丢。"""
    project = tmp_path / "many"
    project.mkdir()
    for i in range(20):
        (project / f"m{i:02d}.py").write_text(
            f'"""模块 {i}"""\n\nPASSWORD_{i} = "pwd-{i}"  # 硬编码密码\n',
            encoding="utf-8")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    client = ManyFilesClient()
    _run_graph(client, None, project, run_dir, thread_id="t-many")

    saved = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert len(saved) == 20            # 20 个文件各 1 条，一条不丢
    assert client.total_requests == 20
