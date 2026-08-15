"""lra — LangGraph-based code review agent (v2, self-contained).

Everything lives under this single package. There is no sibling package to
import from, and no reference/backup copies in the tree.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
__version__ = "2.0.0"
