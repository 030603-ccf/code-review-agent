"""Deterministic scanners — zero LLM, zero tokens.

Shared language normalization lives here (defined first so the scanners can
import it during package init without a circular-import race).
"""

from pathlib import Path


def normalize_lang(relpath: str, lang: str) -> str:
    """Map a language tag or file extension to python/java/javascript (or "")."""
    if lang in ("py", "python"):
        return "python"
    if lang == "java":
        return "java"
    if lang in ("js", "ts", "jsx", "tsx", "javascript", "typescript"):
        return "javascript"
    ext = Path(relpath).suffix.lower()
    if ext == ".py":
        return "python"
    if ext == ".java":
        return "java"
    if ext in (".js", ".ts", ".jsx", ".tsx"):
        return "javascript"
    return ""


from lra.tools.security_scanner import scan_security  # noqa: E402
from lra.tools.anti_pattern_scanner import scan_anti_patterns  # noqa: E402

__all__ = ["scan_security", "scan_anti_patterns", "normalize_lang"]
