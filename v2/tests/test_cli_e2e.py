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


def _run_cli(project, config_path, thread_id, cwd):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.run(
        [sys.executable, "-m", "lra", "review", str(project),
         "--config", str(config_path), "--thread-id", thread_id],
        cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8")


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
