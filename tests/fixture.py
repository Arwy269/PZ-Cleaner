#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试用的样例存档生成器
======================
造一个假的存档目录（map/<X>/<Y>.bin + map_meta.bin），让测试不依赖任何外部文件。

map_meta.bin 里安全屋记录的结构（解析器认的就是这个签名）：
    [int32 x][int32 y][int32 w][int32 h][u16 长度][屋主][8 字节][u16 长度][屋主][名字][城镇][成员...]
                       屋主字符串出现两次，用它定位

用法：
    from fixture import make_save, default_safehouses
    root = make_save(Path("tmp/save"))
"""

from __future__ import annotations

import shutil
import struct
from pathlib import Path
from typing import Iterable, Sequence


def _utf(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


def build_map_meta(records: Iterable[Sequence]) -> bytes:
    """records 里每项：(x, y, w, h, 屋主, 名字, 城镇, [成员...])，字符串都得是 ASCII。"""
    out = bytearray(b"META" + struct.pack(">i", 249))
    for x, y, w, h, owner, name, town, members in records:
        out += struct.pack(">iiii", x, y, w, h)
        out += _utf(owner)
        out += struct.pack(">q", 0)          # 两次屋主之间那个 long
        out += _utf(owner)
        for s in [name, town, *members]:
            out += _utf(s)
    return bytes(out)


def default_safehouses() -> list:
    """两个安全屋，坐标固定，方便测试断言。"""
    return [
        # 覆盖区块 bx 1225..1229 / by 1135..1139（世界格 9801..9837 × 9087..9119）
        (9801, 9087, 36, 32, "Owner1", "Base One", "Muldraugh, KY", ["friend1", "friend2"]),
        # 覆盖区块 bx 1250..1251 / by 1250（世界格 10000..10007 × 10000..10007）
        (10000, 10000, 8, 8, "Owner2", "Pit", "Rosewood, KY", []),
    ]


def make_save(root: Path, bins: Iterable[tuple] = ((1225, 1135), (1226, 1136), (1000, 1000), (999, 500)),
              safehouses: list | None = None, bin_bytes: int = 4096) -> Path:
    """造一个存档目录。bins 里是 (X, Y) 区块坐标，会各写一个 <Y>.bin。"""
    if root.exists():
        shutil.rmtree(root)
    m = root / "map"
    for bx, by in bins:
        d = m / str(bx)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{by}.bin").write_bytes(b"X" * bin_bytes)
    (root / "map_meta.bin").write_bytes(build_map_meta(safehouses if safehouses is not None else default_safehouses()))
    return root


if __name__ == "__main__":       # 手动造一个来看看
    import sys
    p = make_save(Path(sys.argv[1] if len(sys.argv) > 1 else "sample_save"))
    print(f"已生成样例存档：{p}")
