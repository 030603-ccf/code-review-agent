"""文件过滤与增量审查的测试。

覆盖对象：
    - _should_skip     路径/文件名跳过判据（纯函数，好测）
    - changed_files    git diff 变更清单（真实 git 仓库 / 非 git 仓库两态）
"""

import subprocess

from lra.nodes import _should_skip


# ==================== _should_skip（nodes.py）====================

def test_skip_built_dirs():
    """依赖/构建目录里的文件一律跳过。"""
    assert _should_skip("node_modules/foo/index.js")
    assert _should_skip("dist/bundle.js")
    assert _should_skip("__pycache__/mod.cpython-312.pyc")
    assert _should_skip(".venv/Lib/site-packages/pkg/mod.py")


def test_skip_minified_and_generated():
    """压缩产物和生成代码跳过。"""
    assert _should_skip("static/app.min.js")
    assert _should_skip("static/app.min.css")
    assert _should_skip("api/generated.pb.go")


def test_keep_normal_source():
    """正常源码不跳过。"""
    assert not _should_skip("src/main.py")
    assert not _should_skip("tests/test_app.py")
    assert not _should_skip("src/utils/helpers.ts")


# ==================== changed_files（diff.py）====================

def test_changed_files_git_repo(tmp_path):
    """真实 git 仓库：只返回变更文件的相对路径。"""
    from lra.diff import changed_files

    # git init + 提交一个文件，再改它
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(tmp_path),
                   check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path),
                   check=True)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(tmp_path),
                   check=True)

    # 修改 a.py，不动 b.py
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")

    changed = changed_files(tmp_path, base_ref="HEAD")
    assert "a.py" in changed
    assert "b.py" not in changed


def test_changed_files_no_git(tmp_path):
    """非 git 仓库：优雅降级返回空列表（不抛异常）。"""
    from lra.diff import changed_files
    assert changed_files(tmp_path) == []
