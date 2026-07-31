"""lra（Langgraph Review Agent）包初始化。

cra 包已内置在本项目根目录下（cra/ 目录），无需外部路径引导。
运行前确保从项目根目录执行即可（python -m lra review ...）。
"""

from pathlib import Path

# 项目根目录（lra 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
