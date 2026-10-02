# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：安全屋清理器
用法： pyinstaller --noconfirm --clean 安全屋清理器.spec
"""
import os

a = Analysis(
    ['pz_safehouse_cleaner.py'],
    pathex=[],
    binaries=[],
    datas=[('web', 'web')],          # 网页预览（内置，首次运行释放到 exe 同目录）
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc', 'doctest', 'email', 'http', 'xml'],
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
    name='安全屋清理器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
