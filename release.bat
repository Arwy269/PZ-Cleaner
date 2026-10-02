@echo off
chcp 936 >nul
cd /d "%~dp0"
title 打包并生成发布包

echo.
echo   ============================================
echo     僵毁区块清理器 — 打包 + 生成 Release 附件
echo   ============================================
echo.

rem exe 正在运行会锁住文件，先查一下
tasklist /FI "IMAGENAME eq 区块清理器.exe" 2>nul | find "区块清理器.exe" >nul
if not errorlevel 1 (
  echo   [错误] 区块清理器正在运行，请先关掉它再打包。
  echo          ^(exe 被占用时没法覆盖^)
  echo.
  pause
  exit /b 1
)

where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3) else (set PY=python)

echo   [1/3] 打包 exe ...
%PY% -m PyInstaller --noconfirm --clean "%~dp0pz-chunk-cleaner.spec"
if errorlevel 1 (
  echo.
  echo   打包失败。若提示缺模块，先执行： pip install pyinstaller
  pause
  exit /b 1
)

echo.
echo   [2/3] 同步说明文件 ...
copy /y "%~dp0使用说明.txt" "%~dp0dist\使用说明.txt" >nul

echo.
echo   [3/3] 生成 Release 附件 zip ...
%PY% "%~dp0tools\make_release.py"
if errorlevel 1 (
  pause
  exit /b 1
)

echo.
echo   完成。把仓库根目录下的那个 zip 拖到 GitHub Release 页面上传即可。
echo.
pause
