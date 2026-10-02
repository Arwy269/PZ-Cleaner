@echo off
chcp 936 >nul
cd /d "%~dp0"
title 打包 区块清理器

echo.
echo   正在用 PyInstaller 打包 区块清理器.exe ...
echo.

where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3) else (set PY=python)

%PY% -m PyInstaller --noconfirm --clean "%~dp0pz-chunk-cleaner.spec"
if errorlevel 1 (
  echo.
  echo   打包失败。请先执行： pip install pyinstaller
  pause
  exit /b 1
)

echo.
echo   打包完成： dist\区块清理器.exe
echo   分发时把整个 dist 文件夹拷过去（区块清理器.exe + web 文件夹）。
echo.
pause
