"""Make `import lra` resolve to the v2 package when running pytest from v2/."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
