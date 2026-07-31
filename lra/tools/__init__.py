"""lra/tools/ —— 零 LLM 确定性检测器。

这些扫描器不调 LLM、不烧 token，用正则和简单 AST 发现已知模式。
每条发现生成与 Finding schema 兼容的 dict，直接注入审查流水线。

原则："能确定做的事，绝不交给概率模型。"
"""

from lra.tools.security_scanner import scan_security
from lra.tools.anti_pattern_scanner import scan_anti_patterns
