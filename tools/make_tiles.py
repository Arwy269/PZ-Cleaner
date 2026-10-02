#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把大图切成网页用的瓦片金字塔
============================
源图约定：1 像素 = 1 世界格，内容左上角 = 世界坐标 (0,0)（见 README「底图」一节）。

生成 web/tiles/<级别>/<x>_<y>.jpg（源图放 assets/，见 assets/README.md）：
    级别 0 = 原始分辨率（1 像素 = 1 格），级别每 +1 分辨率减半，覆盖的世界范围翻倍。
    每块 TILE×TILE 像素，第 L 级第 (x,y) 块覆盖世界格 [x*TILE*2^L, (x+1)*TILE*2^L)。

网页只加载视野内的瓦片，所以全分辨率也不吃内存（内存 = 可见块数 × TILE² × 4 字节）。

用法：
    python tools/make_tiles.py                                  # 从 assets/Map.png 生成
    python tools/make_tiles.py --from-image assets/world.png    # 换成地形渲染
    python tools/make_tiles.py --tile 1024 --quality 80
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("需要 Pillow：pip install pillow")
    sys.exit(1)

Image.MAX_IMAGE_PIXELS = None      # 世界大图 3 亿像素，远超默认上限

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"


def main() -> int:
    ap = argparse.ArgumentParser(description="生成网页瓦片金字塔")
    ap.add_argument("--from-image", default="assets/Map.png", help="源图（1 像素 = 1 世界格）")
    ap.add_argument("--crop", default="0,0,19797,15897", help="内容区 x0,y0,x1,y1")
    ap.add_argument("--tile", type=int, default=512, help="每块边长（默认 512）")
    ap.add_argument("--quality", type=int, default=82, help="JPEG 质量（默认 82）")
    ap.add_argument("--out", default=str(WEB / "tiles"), help="输出目录")
    ap.add_argument("--max-level", type=int, default=None, help="最大级别（默认按尺寸自动）")
    ap.add_argument("--keep", action="store_true", help="保留旧瓦片（默认先清空输出目录）")
    args = ap.parse_args()

    src_path = Path(args.from_image)
    if not src_path.is_absolute():
        src_path = HERE.parent / src_path
    if not src_path.is_file():
        print(f"找不到源图：{src_path}")
        return 1

    x0, y0, x1, y1 = (int(v) for v in args.crop.split(","))
    im = Image.open(src_path).convert("RGB").crop((x0, y0, x1, y1))
    W, H = im.size
    T = args.tile
    max_level = args.max_level
    if max_level is None:
        max_level = 0
        while (W >> max_level) > T or (H >> max_level) > T:
            max_level += 1

    out_dir = Path(args.out)
    if not args.keep and out_dir.exists():
        shutil.rmtree(out_dir)
    print(f"源图 {src_path.name} 内容 {W}×{H} 格 → 级别 0..{max_level}，每块 {T}×{T}")
    print(f"对应世界坐标 (0,0) 起，网页锚点应为 TR = {x0+W},{y0}  BL = {x0},{y0+H}\n")

    total = 0
    t0 = time.time()
    for L in range(max_level, -1, -1):
        lw, lh = max(1, W >> L), max(1, H >> L)
        img = im if L == 0 else im.resize((lw, lh), Image.LANCZOS)
        nx, ny = -(-lw // T), -(-lh // T)
        d = out_dir / str(L)
        d.mkdir(parents=True, exist_ok=True)
        size_sum = 0
        for ty in range(ny):
            for tx in range(nx):
                box = (tx*T, ty*T, min(lw, (tx+1)*T), min(lh, (ty+1)*T))
                p = d / f"{tx}_{ty}.jpg"
                img.crop(box).save(p, "JPEG", quality=args.quality, optimize=True)
                size_sum += p.stat().st_size
                total += 1
        print(f"  级别 {L:>2}: {lw}×{lh}  {nx}×{ny} = {nx*ny:>5} 块  {size_sum/1048576:6.1f} MB")

    out_mb = sum(f.stat().st_size for f in out_dir.rglob("*.jpg")) / 1048576
    print(f"\n共 {total} 块，{out_mb:.1f} MB，用时 {time.time()-t0:.0f}s → {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
