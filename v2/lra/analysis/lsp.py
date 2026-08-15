"""LSP diagnostics → Finding conversion (deterministic, zero LLM).

Spawns one language server per language and turns its
``textDocument/publishDiagnostics`` output into ``correctness`` findings.
These are high-precision candidate bugs the LLM only has to verify, not hunt
for. Missing/uninstalled servers are skipped silently.
"""

import os
import shlex
import shutil
from pathlib import Path

from lra.analysis.languages import LANG_BY_EXT
from lra.lsp_client import LspClient
from lra.schemas.finding import Finding

# LSP DiagnosticSeverity (1=Error, 2=Warning, 3=Info, 4=Hint) → Finding.severity
_SEVERITY = {1: "critical", 2: "high", 3: "medium", 4: "low"}
_TITLE_MAX = 120


def _resolve_server_cmd(spec) -> list[str]:
    """Normalize a config ``servers`` entry (string or list) into argv list."""
    if spec is None:
        return []
    if isinstance(spec, (list, tuple)):
        parts = [str(p) for p in spec]
    else:
        parts = shlex.split(str(spec))
    if not parts:
        return []
    if os.name == "nt":
        exe = parts[0]
        resolved = shutil.which(exe) or exe
        if resolved.lower().endswith((".cmd", ".bat")):
            # CreateProcess cannot exec a .cmd/.bat directly; route via cmd.exe.
            return ["cmd", "/c"] + parts
    return parts


def _diag_to_finding(relpath: str, content: str, diag: dict) -> Finding | None:
    message = str(diag.get("message", "")).strip()
    if not message:
        return None
    severity = _SEVERITY.get(diag.get("severity"), "low")
    rng = diag.get("range") or {}
    start = rng.get("start") or {}
    end = rng.get("end") or start
    # LSP lines are 0-based; Finding is 1-based.
    line_start = int(start.get("line", 0)) + 1
    line_end = max(int(end.get("line", 0)) + 1, line_start)

    lines = content.splitlines()
    evidence = ""
    if 1 <= line_start <= len(lines):
        evidence = lines[line_start - 1]

    title = message if len(message) <= _TITLE_MAX else message[:_TITLE_MAX - 3] + "..."
    return Finding(
        id="",
        category="correctness",
        severity=severity,
        file_path=relpath,
        line_start=line_start,
        line_end=line_end,
        title=title,
        description=message,
        evidence=evidence,
        suggestion="",
        confidence=0.9,
    )


def lsp_findings(root, files, lsp_cfg) -> list[Finding]:
    """Run LSP diagnostics over ``files`` and return a Finding per diagnostic.

    ``lsp_cfg`` is the config ``lsp`` section: ``{enabled, timeout?, servers}``
    where ``servers`` maps a language_id (from LANG_BY_EXT) to a command string
    or argv list. Any server that cannot start is skipped silently.
    """
    cfg = lsp_cfg or {}
    if not cfg.get("enabled"):
        return []
    servers = cfg.get("servers") or {}
    if not servers:
        return []
    timeout = float(cfg.get("timeout", 30))
    root = Path(root)

    # Group files by language so each server spawns once per language.
    by_lang: dict[str, list[dict]] = {}
    for entry in files:
        relpath = entry.get("relpath", "")
        if entry.get("parse_error"):
            continue  # 语法错误文件已产出 critical finding，避免重复
        lang = LANG_BY_EXT.get(Path(relpath).suffix.lower(), "")
        if lang and servers.get(lang):
            by_lang.setdefault(lang, []).append(entry)

    out: list[Finding] = []
    for lang, entries in by_lang.items():
        cmd = _resolve_server_cmd(servers[lang])
        if not cmd:
            continue
        client = LspClient(cmd, timeout=timeout)
        try:
            client.initialize()
        except Exception:
            # 服务器没装 / 启动失败：静默跳过该语言，不崩主流程。
            client.close()
            continue
        try:
            for entry in entries:
                relpath = entry["relpath"]
                src_path = root / relpath
                try:
                    content = src_path.read_text(encoding="utf-8",
                                                 errors="replace")
                    diags = client.diagnose(str(src_path), content, lang)
                except Exception:
                    continue  # 单文件诊断失败不影响其他文件
                for d in diags:
                    finding = _diag_to_finding(relpath, content, d)
                    if finding is not None:
                        out.append(finding)
        finally:
            client.close()
    return out
