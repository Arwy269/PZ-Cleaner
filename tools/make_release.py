#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打发布包（Release 附件）
=======================
把 dist\\ 里的成品打成一个 zip，直接拖到 GitHub Release 页面上传即可。

包含：区块清理器.exe + 使用说明.txt + web\\（网页、底图、全分辨率瓦片）
不含：config.json（使用者自己的运行配置，里面有他的存档路径，不能发出去）

版本号取自 pz_chunk_cleaner.APP_VERSION，不用手改。

用法：
    python tools/make_release.py              # 打成 僵毁区块清理器-v<版本>.zip
    python tools/make_release.py --out D:\\   # 指定输出目录
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import pz_chunk_cleaner as eng  # noqa: E402

# 这些是使用者自己的运行数据，不能进发布包
EXCLUDE = ("config.json", "save_index.json", "keep.json")


def build(dist: Path, out_dir: Path, version: str) -> Path:
    if not dist.is_dir():
        raise SystemExit(f"找不到 {dist}，先跑一次 build.bat 打包")
    exe = dist / "区块清理器.exe"
    if not exe.is_file():
        raise SystemExit(f"找不到 {exe}，先跑一次 build.bat 打包")

    name = f"{eng.APP_NAME}-v{version}"
    stage = out_dir / f"_{name}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    shutil.copy2(exe, stage / exe.name)
    if (dist / "使用说明.txt").is_file():
        shutil.copy2(dist / "使用说明.txt", stage / "使用说明.txt")
    # 网页和底图
    web = dist / "web"
    if web.is_dir():
        shutil.copytree(web, stage / "web",
                        ignore=shutil.ignore_patterns(*EXCLUDE))

    # 附一份**默认**配置（不含任何人的存档路径），可以先改好再启动
    import json

    (stage / "config.json").write_text(
        json.dumps(eng.DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    files = sorted(p for p in stage.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in files)
    print(f"打包内容：{len(files)} 个文件，{total / 1048576:.1f} MB")

    zpath = out_dir / f"{name}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in files:
            z.write(p, Path(name) / p.relative_to(stage))
    shutil.rmtree(stage, ignore_errors=True)

    size = zpath.stat().st_size
    sha = hashlib.sha256(zpath.read_bytes()).hexdigest()
    print(f"\n已生成：{zpath}")
    print(f"大小：{size / 1048576:.1f} MB")
    print(f"SHA256：{sha}")
    (out_dir / "发布包信息.txt").write_text(
        f"文件名：{zpath.name}\n版本：v{version}\n大小：{size / 1048576:.1f} MB\n"
        f"SHA256：{sha}\n"
        f"内容：区块清理器.exe + 使用说明.txt + web（底图与全分辨率瓦片）\n"
        f"不含：config.json / save_index.json（使用者自己的运行数据）\n",
        encoding="utf-8")
    print(f"（信息写进了 {out_dir / '发布包信息.txt'}，Release 说明里可以贴）")
    return zpath


def main() -> int:
    ap = argparse.ArgumentParser(description="打 Release 附件 zip")
    ap.add_argument("--dist", default=str(ROOT / "dist"), help="成品目录（默认 dist）")
    ap.add_argument("--out", default=str(ROOT), help="zip 输出目录（默认仓库根目录）")
    args = ap.parse_args()
    build(Path(args.dist), Path(args.out), eng.APP_VERSION)
    return 0


if __name__ == "__main__":
    sys.exit(main())
