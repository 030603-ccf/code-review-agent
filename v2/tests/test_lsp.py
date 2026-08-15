"""LSP 集成测试：用 mock 语言服务器验证 framing 解析与诊断→Finding 映射。

不依赖真实 pyright —— 一个假 python 脚本扮演语言服务器：读 stdin 的
Content-Length framing，收到 initialize 回响应、收到 didOpen 回固定的
publishDiagnostics 通知。由此验证 LspClient.diagnose 与 lsp_findings。
"""

import sys
from pathlib import Path

from lra.agents.reviewer import review_chunk
from lra.analysis.lsp import (lsp_candidates_text, lsp_findings,
                              _resolve_server_cmd)
from lra.lsp_client import LspClient

# 一个极简 mock 语言服务器（作为子进程运行，遵守 LSP stdio framing）。
MOCK_SERVER = '''\
import json
import sys


def read_message():
    header = b""
    while b"\\r\\n\\r\\n" not in header:
        ch = sys.stdin.buffer.read(1)
        if not ch:
            return None
        header += ch
    length = 0
    for line in header.decode("ascii", "replace").split("\\r\\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
    body = sys.stdin.buffer.read(length)
    while len(body) < length:
        body += sys.stdin.buffer.read(length - len(body))
    return json.loads(body.decode("utf-8"))


def send(msg):
    data = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(
        ("Content-Length: %d\\r\\n\\r\\n" % len(data)).encode("ascii") + data)
    sys.stdout.buffer.flush()


DIAGNOSTICS = [
    {"range": {"start": {"line": 1, "character": 0},
               "end": {"line": 1, "character": 3}},
     "severity": 1, "message": "undefined name 'foo'"},
    {"range": {"start": {"line": 3, "character": 0},
               "end": {"line": 3, "character": 3}},
     "severity": 2, "message": "unused variable 'z'"},
    {"range": {"start": {"line": 5, "character": 0},
               "end": {"line": 5, "character": 3}},
     "severity": 3, "message": "type mismatch"},
    {"range": {"start": {"line": 6, "character": 0},
               "end": {"line": 6, "character": 3}},
     "severity": 4, "message": "hint: consider x"},
]

while True:
    msg = read_message()
    if msg is None:
        break
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg.get("id"),
              "result": {"capabilities": {"textDocumentSync": 1}}})
    elif method == "textDocument/didOpen":
        uri = msg["params"]["textDocument"]["uri"]
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
              "params": {"uri": uri, "diagnostics": DIAGNOSTICS}})
    # "initialized" 等通知无需回复
'''

CONTENT = "x = 1\ny = 2\nz = 3\nw = 4\nv = 5\nu = 6\nt = 7\n"


def _write_server(tmp_path: Path) -> Path:
    server = tmp_path / "mock_lsp.py"
    server.write_text(MOCK_SERVER, encoding="utf-8")
    return server


def test_diagnose_parses_content_length_framing(tmp_path):
    server = _write_server(tmp_path)
    src = tmp_path / "sample.py"
    src.write_text(CONTENT, encoding="utf-8")

    client = LspClient([sys.executable, str(server)], timeout=10)
    try:
        client.initialize()
        diags = client.diagnose(str(src), CONTENT, "python")
    finally:
        client.close()

    assert len(diags) == 4
    assert diags[0]["message"] == "undefined name 'foo'"
    assert diags[0]["severity"] == 1
    assert diags[0]["range"]["start"]["line"] == 1  # 0-based 原始值
    assert diags[3]["severity"] == 4


def test_lsp_findings_keeps_only_error_level(tmp_path):
    """error（severity==1）直接保留为 finding；warning/info/hint 不进 finding。"""
    server = _write_server(tmp_path)
    (tmp_path / "sample.py").write_text(CONTENT, encoding="utf-8")

    lsp_cfg = {"enabled": True,
               "servers": {"python": [sys.executable, str(server)]}}
    findings = lsp_findings(str(tmp_path),
                            [{"relpath": "sample.py", "parse_error": None}],
                            lsp_cfg)

    assert len(findings) == 1  # 只有 error 级（undefined name）保留
    f = findings[0]
    assert f.category == "correctness"
    assert f.severity == "critical"
    assert f.file_path == "sample.py"
    assert (f.line_start, f.line_end) == (2, 2)
    assert f.title == "undefined name 'foo'"
    assert f.description == "undefined name 'foo'"
    assert f.evidence == "y = 2"
    assert f.suggestion == ""
    assert f.confidence == 0.9
    assert f.id == ""


def test_lsp_candidates_text_formats_warning_and_above(tmp_path):
    """warning/info/hint（severity>=2）进候选文本；error 不进候选。"""
    server = _write_server(tmp_path)
    (tmp_path / "sample.py").write_text(CONTENT, encoding="utf-8")

    lsp_cfg = {"enabled": True,
               "servers": {"python": [sys.executable, str(server)]}}
    text = lsp_candidates_text(str(tmp_path),
                               {"relpath": "sample.py", "parse_error": None},
                               lsp_cfg)

    # error 级（undefined name）不应出现在候选里
    assert "undefined name 'foo'" not in text
    # 三条 warning/info/hint 都在，且带 1-based 行号与 severity 名
    lines = text.splitlines()
    assert len(lines) == 3
    assert lines[0] == "[行 4] Warning: unused variable 'z'"
    assert lines[1] == "[行 6] Info: type mismatch"
    assert lines[2] == "[行 7] Hint: hint: consider x"


def test_lsp_candidates_text_disabled_returns_empty(tmp_path):
    (tmp_path / "sample.py").write_text(CONTENT, encoding="utf-8")
    lsp_cfg = {"enabled": False, "servers": {"python": "pyright-langserver --stdio"}}
    assert lsp_candidates_text(str(tmp_path),
                               {"relpath": "sample.py"}, lsp_cfg) == ""


def test_lsp_candidates_text_missing_server_returns_empty(tmp_path):
    (tmp_path / "sample.py").write_text(CONTENT, encoding="utf-8")
    lsp_cfg = {"enabled": True,
               "servers": {"python": "definitely-not-a-real-lsp-server-xyz"}}
    assert lsp_candidates_text(str(tmp_path),
                               {"relpath": "sample.py"}, lsp_cfg) == ""


def test_lsp_candidates_text_no_configured_language_returns_empty(tmp_path):
    (tmp_path / "sample.py").write_text(CONTENT, encoding="utf-8")
    lsp_cfg = {"enabled": True,
               "servers": {"javascript": "typescript-language-server --stdio"}}
    assert lsp_candidates_text(str(tmp_path),
                               {"relpath": "sample.py"}, lsp_cfg) == ""


class _FakeClient:
    """记录 user prompt 的最小 LLM client，供 review_chunk 注入测试使用。"""

    total_tokens_used = 0

    def __init__(self):
        self.config = type("Cfg", (), {"model": "fake",
                                       "context_length": 8192,
                                       "name": ""})()
        self.last_user = ""

    def chat(self, messages, **kw):
        self.last_user = messages[1]["content"]
        return '{"findings": []}'


def test_review_chunk_injects_lsp_candidates_before_code(tmp_path):
    client = _FakeClient()
    entry = {"relpath": "a.py", "symbols": [], "imports": [],
             "_lsp_candidates":
                 "[行 4] Warning: unused variable 'z'\n[行 6] Info: type mismatch"}
    chunk = {"file": "a.py", "line_start": 1, "line_end": 2,
             "text": "1: x = 1\n2: y = 2\n"}
    review_chunk(client, entry, chunk)

    user = client.last_user
    assert "【语言服务器候选问题】" in user
    assert "unused variable 'z'" in user
    assert "type mismatch" in user
    assert "请验证" in user
    # 候选文本在代码块之前
    assert user.index("【语言服务器候选问题】") < user.index("```")


def test_review_chunk_no_candidates_when_absent(tmp_path):
    client = _FakeClient()
    entry = {"relpath": "a.py", "symbols": [], "imports": []}
    chunk = {"file": "a.py", "line_start": 1, "line_end": 2,
             "text": "1: x = 1\n2: y = 2\n"}
    review_chunk(client, entry, chunk)

    assert "【语言服务器候选问题】" not in client.last_user


def test_lsp_findings_disabled_returns_empty(tmp_path):
    lsp_cfg = {"enabled": False, "servers": {"python": "pyright-langserver --stdio"}}
    out = lsp_findings(str(tmp_path),
                       [{"relpath": "sample.py", "parse_error": None}], lsp_cfg)
    assert out == []


def test_lsp_findings_missing_server_skips_silently(tmp_path):
    (tmp_path / "sample.py").write_text(CONTENT, encoding="utf-8")
    lsp_cfg = {"enabled": True,
               "servers": {"python": "definitely-not-a-real-lsp-server-xyz"}}
    out = lsp_findings(str(tmp_path),
                       [{"relpath": "sample.py", "parse_error": None}], lsp_cfg)
    assert out == []  # 没装服务器：静默跳过，不崩


def test_lsp_findings_skips_parse_error_files(tmp_path):
    server = _write_server(tmp_path)
    (tmp_path / "sample.py").write_text(CONTENT, encoding="utf-8")
    lsp_cfg = {"enabled": True,
               "servers": {"python": [sys.executable, str(server)]}}
    # parse_error 非空的文件不再跑 LSP（已有语法错误 finding）
    out = lsp_findings(str(tmp_path),
                       [{"relpath": "sample.py", "parse_error": "bad (line 1)"}],
                       lsp_cfg)
    assert out == []


def test_resolve_server_cmd_string_and_list():
    assert _resolve_server_cmd("pyright-langserver --stdio") == \
        ["pyright-langserver", "--stdio"]
    assert _resolve_server_cmd(["pyright-langserver", "--stdio"]) == \
        ["pyright-langserver", "--stdio"]
    assert _resolve_server_cmd(None) == []
    assert _resolve_server_cmd("") == []
