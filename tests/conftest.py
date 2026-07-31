"""conftest.py —— pytest 的"开场白"：每个测试文件加载前都会先执行它。

为什么需要它：
    直接跑 `pytest E:\\code-review-agent-langgraph\\tests` 时，
    pytest 只会把 tests\\ 目录加进 sys.path，找不到隔壁的 lra 包。
    这里把项目根目录（tests 的上一级）插进 sys.path，
    `import lra` 才能成功——而 import lra 又会触发 lra/__init__.py
    里的路径引导，把 cra 也接上。两层引导，一次完成。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
