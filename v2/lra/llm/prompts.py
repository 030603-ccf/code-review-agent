"""Prompt loading — prompts live inside the package so the tool is self-contained.

Layout under lra/prompts/:
    reviewer.md              base reviewer system prompt
    second_reviewer.md       arbitration prompt
    supplements/<ext>.md     per-language supplements (java, python, javascript)
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
SUPPLEMENTS_DIR = PROMPTS_DIR / "supplements"

# file extension -> supplement filename
_EXT_SUPPLEMENT = {
    "py": "python.md",
    "java": "java.md",
    "js": "javascript.md", "ts": "javascript.md",
    "jsx": "javascript.md", "tsx": "javascript.md", "vue": "javascript.md",
    "cs": "csharp.md",
    "go": "go.md",
    "rs": "rust.md",
    "c": "cpp.md", "cc": "cpp.md", "cpp": "cpp.md",
    "h": "cpp.md", "hpp": "cpp.md",
    "php": "php.md",
}


def load_prompt(name: str, profile: str | None = None) -> str:
    """Load a base prompt file; raise a clear error if missing.

    When `profile` is non-empty, {name}.{profile}.md is preferred and
    {name}.md is the fallback.
    """
    if profile:
        path = PROMPTS_DIR / f"{name}.{profile}.md"
        if path.is_file():
            return path.read_text(encoding="utf-8")
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"missing prompt file: {path}")
    return path.read_text(encoding="utf-8")


def load_supplement(ext: str) -> str:
    """Load the language supplement for `ext`, or "" when none is defined."""
    filename = _EXT_SUPPLEMENT.get(ext)
    if not filename:
        return ""
    path = SUPPLEMENTS_DIR / filename
    return path.read_text(encoding="utf-8") if path.is_file() else ""
