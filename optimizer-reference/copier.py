"""copier.py —— 副本机制：优化永远发生在副本上，原项目一根手指都不碰。

为什么这是 Phase 3 的第一块基石：
让 AI 提建议和让 AI 改代码，危险等级完全不同。建议错了，顶多浪费你几分钟
阅读时间；代码改错了，可能把能跑的项目改成跑不起来，而且如果你没有 git，
错误修改是不可逆的。所以立一条铁律：

    所有修改只写进 runs/<run_id>/optimized_copy/，
    验证通过前绝不回写原项目；要不要合并、合并哪些，永远由人拍板。

这个文件提供三个纯函数（无状态、输入确定输出就确定，最好测的一类代码）：
    create_workspace  建副本
    hash_tree         给整棵树算哈希（"修改前快照"）
    diff_hashes       对比两份快照，算出"动了哪些文件"
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

# 复用扫描器的排除清单：.git/.venv/__pycache__/node_modules/runs 等。
# 全项目保持同一个口径——扫描时忽略的目录，复制时同样不该带过去。
from cra.analysis.ast_scan import IGNORE_DIRS

# 副本目录名定义成常量：别的模块（Verifier、CLI）都要引用它，
# 各自硬编码字符串的话，改名字时就会漏改——"魔法字符串"要收编
COPY_DIR_NAME = "optimized_copy"


def create_workspace(target_root: Path, run_dir: Path) -> Path:
    """把目标项目完整复制到 run_dir/optimized_copy/，返回副本路径。

    shutil.copytree 的 ignore 参数是一个回调函数，签名固定为：
        ignore(当前目录路径, 该目录下的条目名列表) -> 要跳过的名字集合
    copytree 每进入一个目录就调它一次，问"这里哪些不要拷"。
    """
    target_root = Path(target_root).resolve()
    copy_root = Path(run_dir) / COPY_DIR_NAME

    if copy_root.exists():
        # 同一个 run 重复建副本：先删干净再重建，避免上一次修复的残留
        # 混进这一次。注意 rmtree 是不可逆删除，所以调用方必须保证
        # 传进来的 copy_root 一定在 runs/ 下，绝不能是原项目
        shutil.rmtree(copy_root)

    def _ignore(dir_path: str, names: list[str]) -> set[str]:
        # names 只是条目名（不含路径），直接和排除清单比对即可
        return {n for n in names if n in IGNORE_DIRS}

    # copytree 内部用 makedirs 创建目标，run_dir 不存在也没关系
    shutil.copytree(target_root, copy_root, ignore=_ignore)

    # 给副本"上户口"：git init 出它自己的 .git。
    # 教训换来的（opencode 越界事件）：编程 agent 会从工作目录向上找 .git
    # 来确定"项目根"。副本里没有 .git，它一路找到外层仓库，
    # 把整个世界当工作区，连原项目都敢改。副本有了自己的 .git，
    # 查找在副本门口就停下，agent 的"势力范围"被钉死在副本里。
    # 静默失败即可：系统没装 git 只是少一层保险，不该让建副本本身失败。
    # 放心：.git 在 IGNORE_DIRS 里，hash_tree 不会把它算进快照。
    try:
        subprocess.run(["git", "init", "-q"], cwd=copy_root,
                       capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return copy_root


def hash_tree(root: Path) -> dict[str, str]:
    """给目录里每个文件算 sha256，返回 {相对路径: 哈希值}。

    这就是你之前问的"memory"问题的答案之一：
    修复前算一遍、修复后算一遍，两个字典一对比，哪些文件被动过一清二楚。
    Verifier 只需要复查动过的文件，没动的文件一个字节都不用再读，
    token 不会浪费在已经确认过的东西上。

    为什么用 sha256 而不是文件修改时间（mtime）：
    mtime 在"内容没变但动过文件"（比如编辑器保存了一次）时也会变，
    哈希只看内容——内容不变，哈希必然不变。
    """
    hashes: dict[str, str] = {}
    # sorted 让遍历顺序固定，哈希字典的键序稳定，方便人眼检查
    for p in sorted(Path(root).rglob("*")):
        if p.is_dir():
            continue
        rel = p.relative_to(root)
        # 注意这里比对的是"相对路径的部件"：项目外面那层路径叫什么不重要，
        # 项目内部的 __pycache__ 才需要跳过
        if any(part in IGNORE_DIRS for part in rel.parts):
            continue
        # 读字节（rb）而不是读文本：哈希不受编码问题干扰，
        # 二进制文件（图片、.pyc）也能正确处理
        hashes[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return hashes


def diff_hashes(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    """对比修复前后两份哈希快照，把变化分成三类。

    返回 {"changed": [...], "added": [...], "deleted": [...]}：
        changed  两边都有但哈希不同 -> 文件被修改了（Verifier 的重点对象）
        added    只有 after 有       -> 修改器新建的文件（要警惕：让它修 bug
                                        它却新建文件，可能是跑偏了）
        deleted  只有 before 有      -> 文件被删了（高危信号，必须报警）

    字典推导式里的 before.keys() & after.keys()：
    dict.keys() 支持集合运算，& 是交集、- 是差集——
    比对两个字典的键时这比 for 循环简洁得多。
    """
    common = before.keys() & after.keys()
    changed = sorted(p for p in common if before[p] != after[p])
    added = sorted(after.keys() - before.keys())
    deleted = sorted(before.keys() - after.keys())
    return {"changed": changed, "added": added, "deleted": deleted}
