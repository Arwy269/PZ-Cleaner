@echo off
chcp 936 >nul
cd /d "%~dp0"
title 打包 安全屋清理器

echo.
echo   正在用 PyInstaller 打包 安全屋清理器.exe ...
echo.

where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3) else (set PY=python)

%PY% -m PyInstaller --noconfirm --clean "%~dp0安全屋清理器.spec"
if errorlevel 1 (
  echo.
  echo   打包失败。请先执行： pip install pyinstaller
  pause
  exit /b 1
)

echo.
echo   打包完成： dist\安全屋清理器.exe
echo   分发时把 dist 里的 exe 和 一键清理.bat 放到同一个文件夹即可。
echo.
pause
