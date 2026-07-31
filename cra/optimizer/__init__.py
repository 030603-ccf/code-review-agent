"""cra.optimizer —— 修复闭环子包。

模块分工：
    copier          副本机制（建副本、哈希快照、对比变化）
    opt_state       优化阶段的情景记忆（每条漏洞的修复命运）
    prompt_builder  把漏洞日志翻译成修复任务书
    fixer           修改器（api / opencode 两个后端）
    verifier        复查员（定向对质，判"修没修好"）
    build_check     构建验证层（ruff check 等确定性检查）
    splice          AST 外科缝合（大文件零件替换）
    loop            迭代主循环（反馈回路 + 双刹车）
"""
