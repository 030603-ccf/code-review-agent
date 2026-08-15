"""lra.optimizer — 优化闭环子包：审查发现问题后自动修复并复查迭代。

自包含单包，不依赖任何旧实现（cra/、旧 lra/）。模块分工：

    opt_state  每条 finding 的修复状态机（pending/fixed/failed/…）+ JSON 持久化
    copier     副本机制（建副本、哈希快照）——优化永远发生在副本上
    fixer      修改器（api / opencode 两个后端，compile 语法闸门）
    verifier   复查（LLM 重审 / lint 确定性闸门二选一）
    loop       迭代主循环（修 → 复查 → 判停；修复缓存 + 停滞检测）
"""
