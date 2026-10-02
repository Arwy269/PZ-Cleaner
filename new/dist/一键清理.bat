@echo off
chcp 936 >nul
cd /d "%~dp0"
title 僵毁安全屋清理器
color 0B

echo.
echo   ======================================================
echo      僵毁安全屋清理器  -  删除安全屋以外的所有区块
echo   ======================================================
echo.
echo   运行前请务必：先关闭僵尸毁灭工程服务器！
echo.

if exist "%~dp0安全屋清理器.exe" goto runexe
where py >nul 2>nul
if %errorlevel%==0 goto runpy
where python >nul 2>nul
if %errorlevel%==0 goto runpython

echo   [错误] 找不到 安全屋清理器.exe，本机也没有安装 Python。
echo          请把 安全屋清理器.exe 放到本 bat 同一个文件夹里。
echo.
pause
exit /b 1

:runexe
"%~dp0安全屋清理器.exe" %*
set RC=%errorlevel%
goto done

:runpy
py -3 "%~dp0pz_safehouse_cleaner.py" %*
set RC=%errorlevel%
goto done

:runpython
python "%~dp0pz_safehouse_cleaner.py" %*
set RC=%errorlevel%
goto done

:done
echo.
if not "%RC%"=="0" echo   退出码：%RC%
pause
