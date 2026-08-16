"""End-to-end CLI test: real HTTP path (mock OpenAI server) -> subprocess CLI.

Covers what the FakeClient unit tests can't: the real LLMClient.chat over
HTTP, config loading, argparse, checkpoint wiring, and report rendering.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# The mock always returns one finding; the reviewer overwrites file_path with
# the real chunk path, and the aggregator must correct the (deliberately wrong)
# reported line 1 to the real line of the evidence.
_FINDINGS = json.dumps({"findings": [{
    "id": "F1", "category": "security", "severity": "high",
    "file_path": "app.py", "line_start": 1, "line_end": 1,
    "title": "除零风险", "description": "整数除法可能除以零",
    "evidence": "result = 100 / 0", "suggestion": "加除数为零的判断",
    "confidence": 0.9,
}]}, ensure_ascii=False)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({
            "choices": [{"message": {"content": _FINDINGS}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20,
                      "total_tokens": 30},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def mock_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)  # port 0 -> ephemeral
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture
def sample_project(tmp_path):
    p = tmp_path / "sample"
    p.mkdir()
    # "result = 100 / 0" is line 5
    (p / "app.py").write_text(
        '"""demo"""\n\n\ndef run():\n    result = 100 / 0\n    return result\n',
        encoding="utf-8")
    return p


def _run_cli(project, config_path, thread_id, cwd, extra=None):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    cmd = [sys.executable, "-m", "lra", "review", str(project),
           "--config", str(config_path), "--thread-id", thread_id]
    if extra:
        cmd.extend(extra)
    return subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True,
                          text=True, encoding="utf-8")


def _write_mock_config(tmp_path, mock_server) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(f"""default_profile: mock
profiles:
  mock:
    base_url: "http://127.0.0.1:{mock_server}/v1"
    api_key_env: "MOCK_KEY"
    model: "mock-model"
    temperature: 0.2
    max_tokens: 4096
    context_length: 8192
    timeout: 30
review:
  second_profile: null
  concurrency: 4
""", encoding="utf-8")
    return config


def test_cli_end_to_end(tmp_path, sample_project, mock_server):
    config = tmp_path / "config.yaml"
    config.write_text(f"""default_profile: mock
profiles:
  mock:
    base_url: "http://127.0.0.1:{mock_server}/v1"
    api_key_env: "MOCK_KEY"
    model: "mock-model"
    temperature: 0.2
    max_tokens: 4096
    context_length: 8192
    timeout: 30
review:
  second_profile: null
  concurrency: 4
""", encoding="utf-8")

    result = _run_cli(sample_project, config, "itest", cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr

    run_dir = tmp_path / "runs" / "itest"
    findings = json.loads(
        (run_dir / "findings.json").read_text(encoding="utf-8"))
    assert len(findings) == 1
    # aggregator corrected the mock's bogus line 1 -> real line 5
    assert (findings[0]["line_start"], findings[0]["line_end"]) == (5, 5)
    assert findings[0]["file_path"] == "app.py"

    md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "## 问题清单" in md
    assert "## ##" not in md  # regression guard for the double-heading bug
    assert (run_dir / "project_map.json").exists()
    assert (run_dir / "checkpoints.sqlite").exists()


def test_cli_same_thread_id_different_path_fails(tmp_path, sample_project, mock_server):
    """同 thread-id 换目标路径必须拒绝续跑，不能拿 A 项目的 checkpoint 当 B 的结果。"""
    config = tmp_path / "config.yaml"
    config.write_text(f"""default_profile: mock
profiles:
  mock:
    base_url: "http://127.0.0.1:{mock_server}/v1"
    api_key_env: "MOCK_KEY"
    model: "mock-model"
    temperature: 0.2
    max_tokens: 4096
    context_length: 8192
    timeout: 30
review:
  second_profile: null
  concurrency: 4
""", encoding="utf-8")

    # 第一次跑项目 A，正常完成并留下 checkpoint
    r1 = _run_cli(sample_project, config, "itest-path", cwd=tmp_path)
    assert r1.returncode == 0, r1.stdout + r1.stderr

    # 换一个项目 B，复用同一个 thread-id：必须报错退出
    other = tmp_path / "other"
    other.mkdir()
    (other / "app.py").write_text("# clean\nx = 1\n", encoding="utf-8")

    r2 = _run_cli(other, config, "itest-path", cwd=tmp_path)
    assert r2.returncode != 0
    combined = r2.stdout + r2.stderr
    assert "不一致" in combined
    assert "--thread-id" in combined


def test_cli_custom_run_dir_and_summary(tmp_path, sample_project, mock_server):
    """--run-dir 把 runs/ 钉到指定根目录，且结束后写出机器可读 summary.json。"""
    config = _write_mock_config(tmp_path, mock_server)
    run_root = tmp_path / "custom-runs"

    result = _run_cli(sample_project, config, "run-dir-test", cwd=tmp_path,
                      extra=["--run-dir", str(run_root)])
    assert result.returncode == 0, result.stdout + result.stderr

    run_dir = run_root / "run-dir-test"
    assert run_dir.is_dir()
    assert not (tmp_path / "runs" / "run-dir-test").exists()

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["thread_id"] == "run-dir-test"
    assert summary["status"] == "completed"
    assert summary["mode"] == "full"
    assert summary["findings_count"] == 1
    assert summary["failed_blocks_count"] == 0
    assert summary["initial_tokens"] == 30
    assert summary["initial_requests"] >= 1
    assert summary["wall_seconds"] >= 0
    assert Path(summary["run_dir"]) == run_dir


def test_cli_incremental_strict_non_git_fails(tmp_path, sample_project, mock_server):
    """--incremental-strict 在非 git 目录必须报错，不能静默退化为全量。"""
    config = _write_mock_config(tmp_path, mock_server)
    result = _run_cli(sample_project, config, "strict-nogit", cwd=tmp_path,
                      extra=["--incremental", "--incremental-strict"])
    assert result.returncode == 2
    assert "--incremental-strict" in (result.stdout + result.stderr)


def test_cli_incremental_non_git_fallback_summary(tmp_path, sample_project, mock_server):
    """非 strict 增量在非 git 目录退化为全量，summary.mode 必须是 full_fallback。"""
    config = _write_mock_config(tmp_path, mock_server)
    result = _run_cli(sample_project, config, "fallback-test", cwd=tmp_path,
                      extra=["--incremental"])
    assert result.returncode == 0, result.stdout + result.stderr

    summary = json.loads(
        (tmp_path / "runs" / "fallback-test" / "summary.json")
        .read_text(encoding="utf-8"))
    assert summary["mode"] == "full_fallback"
    assert summary["status"] == "completed"


def test_cli_incremental_strict_without_incremental_fails(tmp_path, sample_project,
                                                          mock_server):
    """--incremental-strict 单独使用是参数错误。"""
    config = _write_mock_config(tmp_path, mock_server)
    result = _run_cli(sample_project, config, "strict-alone", cwd=tmp_path,
                      extra=["--incremental-strict"])
    assert result.returncode == 2
    assert "必须与 --incremental 一起使用" in (result.stdout + result.stderr)
