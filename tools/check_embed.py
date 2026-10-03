#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检查"地图能不能嵌进窗口"
========================
嵌入预览要凑齐三样东西，缺一样就会退回系统浏览器：

  1. pywebview（含 pythonnet / clr）—— 负责提供 WebView2 的 .NET 程序集
  2. WebView2 的 .NET 程序集 —— 随 pywebview 一起装
  3. **WebView2 运行时**（系统组件）—— 桌面版 Windows 自带；
     **Windows Server 默认没有，必须手动装**

用法（在服务器上，用跑程序的同一个 Python）：
    python tools\\check_embed.py

只读检查，不改任何东西。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

OK = "  [OK]   "
NO = "  [缺失] "
WARN = "  [注意] "

problems: list[str] = []


def line(tag: str, msg: str) -> None:
    print(tag + msg)


print("=" * 66)
print("  僵毁区块清理器 · 嵌入预览自检")
print("=" * 66)
print()

# ---------------------------------------------------------------- 1. Python
print("[1/4] 运行环境")
line(OK if sys.version_info >= (3, 9) else WARN, f"Python {sys.version.split()[0]}  ({sys.executable})")
line(OK, f"系统：{os.environ.get('OS', '?')} / {sys.platform}")
print()

# ---------------------------------------------------------------- 2. pywebview
print("[2/4] pywebview / pythonnet")
try:
    import webview  # noqa: F401
    ver = getattr(webview, "__version__", "?")
    line(OK, f"pywebview 已安装（{ver}）")
except Exception as e:  # noqa: BLE001
    line(NO, f"import webview 失败：{e}")
    problems.append("没装 pywebview。装它：pip install pywebview")

try:
    import clr  # noqa: F401
    line(OK, "pythonnet（clr）可用")
except Exception as e:  # noqa: BLE001
    line(NO, f"import clr 失败：{e}")
    problems.append("pythonnet 不可用，通常随 pywebview 一起装；可试 pip install pythonnet")
print()

# ------------------------------------------------------- 3. WebView2 .NET 程序集
print("[3/4] WebView2 的 .NET 程序集（随 pywebview 提供）")
asm_ok = False
try:
    from webview.util import interop_dll_path

    import clr

    for name in ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll"):
        p = interop_dll_path(name)
        clr.AddReference(p)
        line(OK, f"{name}")
    for plat in ("win-x64", "win-x86", "win-arm64"):
        d = Path(interop_dll_path(plat))
        if d.is_dir() and any(d.glob("WebView2Loader.dll")):
            line(OK, f"原生加载器：{plat}\\WebView2Loader.dll")
            asm_ok = True
            break
    if not asm_ok:
        line(NO, "没找到任何平台的 WebView2Loader.dll")
        problems.append("pywebview 装得不完整，重装一次：pip install --force-reinstall pywebview")
except Exception as e:  # noqa: BLE001
    line(NO, f"加载失败：{type(e).__name__}: {e}")
    problems.append("WebView2 程序集加载不了，先解决上面第 2 步")
print()

# ---------------------------------------------------------- 4. WebView2 运行时
print("[4/4] WebView2 运行时（系统组件 —— Server 上最容易缺的就是这个）")
GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
found_ver = None
try:
    import winreg

    for root, path in (
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{GUID}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{GUID}"),
        (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{GUID}"),
    ):
        try:
            with winreg.OpenKey(root, path) as k:
                found_ver = winreg.QueryValueEx(k, "pv")[0]
                break
        except OSError:
            continue
except Exception as e:  # noqa: BLE001
    line(WARN, f"读注册表失败：{e}")

for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
    if not base:
        continue
    d = Path(base) / "Microsoft" / "EdgeWebView" / "Application"
    if d.is_dir():
        vers = [p.name for p in d.iterdir() if p.is_dir() and p.name[0].isdigit()]
        if vers and not found_ver:
            found_ver = vers[0] + "（从安装目录推断）"
        break

if found_ver:
    line(OK, f"已安装，版本 {found_ver}")
else:
    line(NO, "没装 WebView2 运行时")
    problems.append(
        "缺 WebView2 运行时。Windows Server 默认不带，需要手动装：\n"
        "           下载 https://go.microsoft.com/fwlink/p/?LinkId=2124703\n"
        "           （Microsoft Edge WebView2 Evergreen Bootstrapper，约 2 MB，\n"
        "           双击安装，装完约 150 MB；装完重开程序即可）\n"
        "           注意选 x64 版，跟你的 Python 位数一致"
    )

# ------------------------------------------------------------------- 结论
print()
print("=" * 66)
if problems:
    print(f"  结论：有 {len(problems)} 处需要处理，处理完嵌入就能用")
    print()
    for i, p in enumerate(problems, 1):
        print(f"  {i}. {p}")
    print()
    print("  处理完重跑一次本脚本确认。")
else:
    print("  结论：三样都齐了，地图应该能正常嵌进窗口。")
    print()
    print("  如果程序里仍然退回浏览器，把运行日志里那句")
    print("  「嵌入地图失败：…」后面的内容发出来，那才是真正的报错。")
print("=" * 66)
