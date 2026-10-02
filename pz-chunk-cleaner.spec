# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：区块清理器
用法： pyinstaller --noconfirm --clean 区块清理器.spec
"""
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# 嵌入地图预览要用：pywebview 自带的 WebView2 .NET 程序集 + pythonnet
datas = collect_data_files('webview')
hiddenimports = collect_submodules('webview') + ['clr', 'pythonnet']

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[],
    # 只打包网页本体；瓦片金字塔（web/tiles，约 25 MB）随文件夹分发，
    # 打进 exe 会让每次启动都多解压 25 MB，不值得
    datas=[('web/index.html', 'web'), ('web/map.jpg', 'web')] + datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['unittest', 'pydoc', 'doctest', 'test'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='区块清理器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 窗口模式：双击不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
