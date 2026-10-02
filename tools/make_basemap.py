#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成网页底图 web/map.jpg
=========================
从 pzmap.org 的 DZI 瓦片金字塔下载等距渲染图，重采样成【正投影】大图，
供 web/index.html 当作 map.jpg 使用（按世界方格坐标对齐）。

pzmap.org 的渲染是等距菱形（旋转 45°），直接拼出来会和网页的矩形选区对不上，
所以这里用仿射变换把它掰回正投影——最终图仍然是"上北下南"的方块图。

投影公式（来自 pzmap2dzi 的 map_info.json，与 pzmap.org 网页一致）：
    原图px = x0 + (世界x - 世界y) * sqr / 2
    原图py = y0 + (世界x + 世界y) * sqr / 4
    x0 = 1040384, y0 = -139296, sqr = 128, 世界范围 x∈[0,19968) y∈[0,16128)

用法：
    python tools/make_basemap.py                 # 从 pzmap.org 在线瓦片生成
    python tools/make_basemap.py --level 14 --width 4096
    python tools/make_basemap.py --keep-tiles    # 保留下载的瓦片（默认保留，便于重跑）

依赖：Pillow（pip install pillow）。瓦片下载用标准库。
"""

from __future__ import annotations

import argparse
import io
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("需要 Pillow：pip install pillow")
    sys.exit(1)

# 世界大图有 3 亿像素，远超 Pillow 默认的 1.79 亿上限（那是防解压炸弹的）
Image.MAX_IMAGE_PIXELS = None

# pzmap2dzi 投影参数（pzmap.org / pzmap.net 同源）
X0, Y0, SQR = 1040384.0, -139296.0, 128.0
WORLD_W, WORLD_H = 19968, 16128          # 世界方格数
FULL_W, FULL_H = 2318656, 1019040        # DZI 最大级别（level 22）的像素尺寸
MAX_LEVEL = 22

BASE_URL = "https://tiles.pzmap.org/42.20.0/base/layer0_files"
TILE = 2048
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
CACHE = HERE.parent / "_tiles_cache"


def level_size(level: int) -> tuple[int, int]:
    d = 2 ** (MAX_LEVEL - level)
    return -(-FULL_W // d), -(-FULL_H // d)


def fetch_tile(level: int, tx: int, ty: int, cache_dir: Path) -> Path | None:
    """下载一块瓦片，返回本地路径；瓦片不存在（404）返回 None。"""
    dst = cache_dir / f"{tx}_{ty}.jpg"
    if dst.is_file() and dst.stat().st_size > 1024:
        return dst
    url = f"{BASE_URL}/{level}/{tx}_{ty}.jpg"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://pzmap.org/",
        "Accept": "image/avif,image/webp,image/jpeg,*/*",
    })
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                data = r.read()
            if data[:2] != b"\xff\xd8":          # 不是 JPEG（多半是错误页）
                return None
            dst.write_bytes(data)
            return dst
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == 2:
                print(f"    瓦片 {tx}_{ty} 失败：HTTP {e.code}")
                return None
            time.sleep(1.5)
        except Exception as e:                    # noqa: BLE001
            if attempt == 2:
                print(f"    瓦片 {tx}_{ty} 失败：{e}")
                return None
            time.sleep(1.5)
    return None


def download_level(level: int, workers: int = 4) -> tuple[Image.Image, int]:
    """下载并拼出该级别的整幅等距图。"""
    w, h = level_size(level)
    nx, ny = -(-w // TILE), -(-h // TILE)
    cache_dir = CACHE / f"L{level}"
    cache_dir.mkdir(parents=True, exist_ok=True)

    jobs = [(x, y) for y in range(ny) for x in range(nx)]
    print(f"级别 {level}：图像 {w}×{h}，需要 {nx}×{ny} = {len(jobs)} 块瓦片")

    got = 0
    canvas = Image.new("RGB", (w, h), (0, 0, 0))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_tile, level, x, y, cache_dir): (x, y) for x, y in jobs}
        for i, fut in enumerate(futures, 1):
            x, y = futures[fut]
            p = fut.result()
            if p is None:
                continue
            try:
                tile = Image.open(p)
                tile.load()
            except Exception:                     # noqa: BLE001
                continue
            canvas.paste(tile, (x * TILE, y * TILE))
            got += 1
            print(f"\r  下载 {i}/{len(jobs)}  已拼 {got} 块  {time.time()-t0:.0f}s", end="", flush=True)
    print(f"\r  完成：{got}/{len(jobs)} 块，用时 {time.time()-t0:.0f}s          ")
    return canvas, got


def to_topdown(iso: Image.Image, level: int, out_w: int) -> Image.Image:
    """
    等距图 → 正投影图（仿射重采样）。
    输出覆盖世界矩形 x∈[0,WORLD_W) y∈[0,WORLD_H)，像素中心对齐。
    """
    d = 2 ** (MAX_LEVEL - level)
    A = X0 / d                    # 原图 px 的常数项
    B = Y0 / d                    # 原图 py 的常数项
    k = SQR / d                   # 每 (x-y) 的原图像素
    s = out_w / WORLD_W           # 输出：每世界方格的像素数
    out_h = round(WORLD_H * s)

    # 输出像素 (tx,ty) 的中心 → 世界坐标 ((tx+.5)/s, (ty+.5)/s)
    #   px = A + (x-y)*k/2   →  A + k/(2s)*(tx-ty)
    #   py = B + (x+y)*k/4   →  B + k/(4s)*(tx+ty+1)
    a = k / (2 * s)
    b = -a
    c = A
    dd = k / (4 * s)
    e = dd
    f = B + dd
    print(f"  仿射重采样：{iso.size[0]}×{iso.size[1]}  →  {out_w}×{out_h}")
    return iso.transform((out_w, out_h), Image.AFFINE, (a, b, c, dd, e, f),
                         resample=Image.BICUBIC, fillcolor=(0, 0, 0))


def from_local_image(path: Path, crop: str, out_w: int, quality: int, out_path: Path) -> int:
    """
    用本地大图生成底图。约定：图内 1 像素 = 1 世界格，内容左上角 = 世界坐标 (0,0)。
    （new\\Map.png / new\\world.png 都符合这个约定，已用配准验证：k=1.0000、偏移 0。）
    """
    x0, y0, x1, y1 = (int(v) for v in crop.split(","))
    im = Image.open(path).convert("RGB").crop((x0, y0, x1, y1))
    w, h = im.size
    print(f"  源图 {path.name}: 内容 {w}×{h} 像素 = 世界 ({x0},{y0}) 起的 {w}×{h} 格")
    out_h = round(h * out_w / w)
    print(f"  缩放 → {out_w}×{out_h}  ({out_w/WORLD_W:.3f} 像素/格)")
    im = im.resize((out_w, out_h), Image.LANCZOS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_path, "JPEG", quality=quality, optimize=True, progressive=True)
    mb = out_path.stat().st_size / 1048576
    print(f"\n已写出 {out_path}  ({out_w}×{out_h}, {mb:.1f} MB)")
    print(f"网页锚点应为： TR = {x0+w},{y0}   BL = {x0},{y0+h}")
    print(f"（解码后约需内存 {out_w*out_h*4/1048576:.0f} MB）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="生成网页正投影底图")
    ap.add_argument("--from-image", help="用本地大图（1 像素 = 1 世界格，内容左上角 = 世界 0,0）")
    ap.add_argument("--crop", default="0,0,19797,15897", help="--from-image 时截取的内容区域 x0,y0,x1,y1")
    ap.add_argument("--level", type=int, default=15, help="pzmap 瓦片级别 8~17（越大越清晰，默认 15）")
    ap.add_argument("--width", type=int, default=8192, help="输出图宽度（默认 8192）")
    ap.add_argument("--quality", type=int, default=85, help="JPEG 质量（默认 85）")
    ap.add_argument("--out", default=str(WEB / "map.jpg"), help="输出路径")
    ap.add_argument("--workers", type=int, default=4, help="并发下载数")
    ap.add_argument("--no-cache", action="store_true", help="不保留瓦片缓存")
    args = ap.parse_args()

    if args.from_image:
        p = Path(args.from_image)
        if not p.is_absolute():
            p = HERE.parent / p
        if not p.is_file():
            print(f"找不到源图：{p}")
            return 1
        return from_local_image(p, args.crop, args.width, args.quality, Path(args.out))

    if not (8 <= args.level <= 17):
        print("级别建议 8~17：太小不清晰，太大（≥18）会占用几个 GB 内存。")
        return 1

    iso, got = download_level(args.level, args.workers)
    if got == 0:
        print("一块瓦片都没下到，检查网络或 URL。")
        return 1

    out = to_topdown(iso, args.level, args.width)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(out_path, "JPEG", quality=args.quality, optimize=True, progressive=True)
    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"\n已写出 {out_path}  ({out.size[0]}×{out.size[1]}, {size_mb:.1f} MB)")
    print(f"网页锚点应为： TR = {WORLD_W},{0}   BL = {0},{WORLD_H}")
    print(f"（解码后约需内存 {out.size[0]*out.size[1]*4/1024/1024:.0f} MB）")

    if args.no_cache:
        import shutil
        shutil.rmtree(CACHE, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
