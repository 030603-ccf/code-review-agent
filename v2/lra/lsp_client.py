"""Minimal LSP client — JSON-RPC over stdio with Content-Length framing.

Talks to a real language server (pyright, typescript-language-server, ...) to
produce deterministic, high-precision diagnostics (type errors, undefined
names, ...). Zero LLM.

Protocol notes:
* Messages are framed with a ``Content-Length`` header whose value is the
  UTF-8 *byte* length of the JSON body (not its character count).
* Responses carry an ``id``; notifications do not. ``diagnose`` blocks until
  the matching ``textDocument/publishDiagnostics`` notification arrives (or
  the timeout elapses).
"""

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote


def _uri_key(uri: str) -> str:
    """Normalize a file:// URI for case-insensitive, percent-decoded compare.

    pyright may answer with a different case or percent-encoding than the
    request (e.g. ``file:///e%3A/...`` vs ``file:///E:/...``), so compare on the
    decoded, lowercased path rather than the raw string.
    """
    path = unquote(uri).replace("file:///", "").replace("file://", "")
    return Path(path).resolve().as_posix().lower()


class LspError(RuntimeError):
    """Raised when the language server cannot be driven."""


class LspClient:
    def __init__(self, server_cmd: list[str], timeout: float = 30.0,
                 root_uri: str | None = None):
        self.server_cmd = list(server_cmd)
        self.timeout = timeout
        # 工作区根目录（路径字符串）。None=进程 cwd（向后兼容）；否则对被审
        # 项目建立工作区，语言服务器才能对着正确目录解析 import / 项目配置。
        self.root_uri = root_uri
        self.proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._messages: queue.Queue = queue.Queue()
        self._next_id = 1

    # ---- process lifecycle ----
    def _start(self) -> None:
        if self.proc is not None:
            return
        self.proc = subprocess.Popen(
            self.server_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        # Single background reader: it puts every framed message onto the
        # queue so callers can wait with a real timeout instead of blocking
        # forever on a pipe read (select() can't poll pipes on Windows).
        while True:
            try:
                msg = self._read_message()
            except Exception:
                msg = None
            self._messages.put(msg)
            if msg is None:
                return

    # ---- wire protocol ----
    def _read_message(self) -> dict:
        """Read one Content-Length framed message from stdout (blocking).

        Returns the parsed JSON body. Raises LspError on EOF/truncation.
        """
        stdout = self.proc.stdout
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = stdout.read(1)
            if not chunk:
                raise LspError("语言服务器在返回消息前关闭了 stdout")
            header += chunk

        content_length: int | None = None
        for line in header.decode("ascii", errors="replace").split("\r\n"):
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())
        if content_length is None:
            raise LspError("LSP 消息缺少 Content-Length header")

        body = stdout.read(content_length) or b""
        while len(body) < content_length:
            more = stdout.read(content_length - len(body))
            if not more:
                raise LspError("LSP 消息体被截断")
            body += more
        return json.loads(body.decode("utf-8"))

    def _send(self, msg: dict) -> None:
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self.proc.stdin.write(header + body)
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict) -> dict:
        msg_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": msg_id,
                    "method": method, "params": params})
        return self._wait(lambda m: m.get("id") == msg_id)

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _wait(self, predicate, timeout: float | None = None) -> dict:
        """Block until a queued message satisfies ``predicate`` (or timeout)."""
        timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LspError(f"等待语言服务器响应超时（>{timeout:.0f}s）")
            try:
                msg = self._messages.get(timeout=remaining)
            except queue.Empty:
                raise LspError(f"等待语言服务器响应超时（>{timeout:.0f}s）")
            if msg is None:
                raise LspError("语言服务器意外退出")
            if predicate(msg):
                return msg

    # ---- LSP operations ----
    def _workspace_root_uri(self) -> str:
        """Resolve the workspace root to a file:// URI.

        ``root_uri`` (a path string) wins; when it's None the process CWD is
        used, preserving the pre-existing behaviour for callers that review
        from the project directory itself.
        """
        root = Path(self.root_uri) if self.root_uri else Path.cwd()
        return root.resolve().as_uri()

    def initialize(self) -> dict:
        self._start()
        root_uri = self._workspace_root_uri()
        resp = self._request("initialize", {
            "capabilities": {},
            # pyright needs a workspace root before it will analyze didOpen'd
            # files; point it at the reviewed project (root_uri), NOT the CWD.
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": "workspace"}],
        })
        self._notify("initialized", {})
        return resp

    def diagnose(self, file_path: str, content: str,
                 language_id: str) -> list[dict]:
        self._start()
        uri = Path(file_path).resolve().as_uri()
        self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": language_id,
                "version": 1,
                "text": content,
            },
        })
        msg = self._wait(
            lambda m: m.get("method") == "textDocument/publishDiagnostics"
            and _uri_key((m.get("params") or {}).get("uri", "")) == _uri_key(uri))
        return (msg.get("params") or {}).get("diagnostics", [])

    def close(self) -> None:
        if self.proc is not None:
            try:
                self.proc.terminate()
            except OSError:
                pass
            self.proc = None
            self._reader = None

    def __enter__(self) -> "LspClient":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False
