"""scan.py 单文件索引测试：一次读盘 + 正确 sha1/size/line_count。"""

import hashlib
from pathlib import Path

from lra.analysis.scan import scan_file


def test_scan_file_reads_once_and_hashes_bytes(tmp_path, monkeypatch):
    src = tmp_path / "a.py"
    src.write_text("x = 1\ny = 2\n", encoding="utf-8")
    original = src.read_bytes()

    calls = {"read_bytes": 0, "read_text": 0, "stat": 0}
    real_read_bytes = Path.read_bytes
    real_read_text = Path.read_text
    real_stat = Path.stat

    def counted(fn, name):
        def wrapper(self, *a, **k):
            calls[name] += 1
            return fn(self, *a, **k)
        return wrapper

    monkeypatch.setattr(Path, "read_bytes", counted(real_read_bytes, "read_bytes"))
    monkeypatch.setattr(Path, "read_text", counted(real_read_text, "read_text"))
    monkeypatch.setattr(Path, "stat", counted(real_stat, "stat"))

    entry = scan_file(src, "a.py")

    # 输出正确性：sha1 取字节哈希、size 取字节数、行数按文本
    assert entry["sha1"] == hashlib.sha1(original).hexdigest()
    assert entry["size_bytes"] == len(original)
    assert entry["line_count"] == 2
    # 性能修复：只读一次盘；read_text / stat 不再二次访问文件
    assert calls["read_bytes"] == 1
    assert calls["read_text"] == 0
    assert calls["stat"] == 0
    # ast 符号表仍正常产出
    assert {s["name"] for s in entry["symbols"]} == {"x", "y"}
