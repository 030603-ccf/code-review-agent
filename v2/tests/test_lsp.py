"""LSP 集成测试：用 mock 语言服务器验证 framing 解析与诊断→Finding 映射。

不依赖真实 pyright —— 一个假 python 脚本扮演语言服务器：读 stdin 的
Content-Length framing，收到 initialize 回响应、收到 didOpen 回固定的
publishDiagnostics 通知。由此验证 LspClient.diagnose 与 lsp_findings。
"""

import sys
from pathlib import Path

from lra.analysis.lsp import lsp_findings, _resolve_server_cmd
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


def test_lsp_findings_maps_diagnostics_to_findings(tmp_path):
    server = _write_server(tmp_path)
    (tmp_path / "sample.py").write_text(CONTENT, encoding="utf-8")

    lsp_cfg = {"enabled": True,
               "servers": {"python": [sys.executable, str(server)]}}
    findings = lsp_findings(str(tmp_path),
                            [{"relpath": "sample.py", "parse_error": None}],
                            lsp_cfg)

    assert len(findings) == 4
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

    # severity 映射：2→high 3→medium 4→low
    assert [g.severity for g in findings] == ["critical", "high", "medium", "low"]
    assert findings[1].line_start == 4 and findings[1].evidence == "w = 4"
    assert findings[2].line_start == 6 and findings[2].evidence == "u = 6"
    assert findings[3].line_start == 7 and findings[3].evidence == "t = 7"


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
