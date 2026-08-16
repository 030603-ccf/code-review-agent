"""IGNORE_DIRS 单一来源 tests：三处共享一份清单，语义宁多勿漏。"""

from lra.ignore import IGNORE_DIRS, is_ignored_dir_name, path_is_ignored
from lra.analysis.scan import scan_project

REQUIRED = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "node_modules",
    "dist", "build", "out", "target", ".idea", ".vscode", "runs",
    "vendor", "third_party", "thirdparty", "site-packages",
}


def test_ignore_dirs_is_superset_of_union():
    assert REQUIRED <= IGNORE_DIRS


def test_is_ignored_dir_name_suffix_egg_info():
    assert is_ignored_dir_name("myproj.egg-info")
    assert is_ignored_dir_name(".venv")
    assert not is_ignored_dir_name("src")


def test_path_is_ignored_any_depth():
    assert path_is_ignored(("proj", "vendor", "a.py"))
    assert path_is_ignored(("proj", "myproj.egg-info", "x"))
    assert not path_is_ignored(("proj", "src", "a.py"))


def test_scan_project_skips_ignored_dirs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "lib.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.py").write_text("z = 3\n", encoding="utf-8")
    # site-packages 只在 nodes 旧清单里、scan 旧清单没有——统一后 scan 也必须跳过
    (tmp_path / "site-packages").mkdir()
    (tmp_path / "site-packages" / "dep.py").write_text("w = 4\n", encoding="utf-8")
    (tmp_path / "myproj.egg-info").mkdir()
    (tmp_path / "myproj.egg-info" / "meta.py").write_text("m = 5\n", encoding="utf-8")

    pm = scan_project(tmp_path)
    assert {f["relpath"] for f in pm["files"]} == {"src/app.py"}


def test_copier_ignored_uses_shared():
    from lra.optimizer.copier import _ignored
    assert _ignored(("proj", "site-packages", "x.py"))
    assert _ignored(("proj", "myproj.egg-info", "x"))
    assert not _ignored(("proj", "src", "x.py"))


def test_nodes_should_skip_uses_shared():
    from lra import nodes
    # runs 在 scan/copier 旧清单里、nodes 旧清单没有——统一后 nodes 也必须跳过
    assert nodes._should_skip("runs/123/project_map.json")
    assert nodes._should_skip("vendor/foo.py")
    assert not nodes._should_skip("src/app.py")
    # 三处都不再各自定义目录清单
    assert not hasattr(nodes, "SKIP_DIR_PARTS")


def test_no_local_ignore_copies():
    import lra.analysis.scan as scan
    import lra.optimizer.copier as copier
    assert not hasattr(scan, "IGNORE_DIRS")
    assert not hasattr(copier, "IGNORE_DIRS")
