@echo off
rem ============================================================
rem  CF IP Pool one-click launcher (Windows)
rem  Termux / Linux / macOS : use ./run
rem ------------------------------------------------------------
rem  Usage:
rem    run.bat                 auto-detect and start
rem    run.bat check           only self-check the push chain
rem    run.bat schedule install  install the scheduled task
rem    run.bat target 3        pass args through to main.py
rem    run.bat win             explicit Windows (same as no arg)
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"

rem allow "./run win" style, just swallow it
if /i "%~1"=="win" shift
if /i "%~1"=="windows" shift

rem map bare "check" / "init" / "schedule" to the real flags
if /i "%~1"=="check" goto :runcheck
if /i "%~1"=="init"  goto :runinit
if /i "%~1"=="schedule" goto :runsched

where python >nul 2>nul
if errorlevel 1 (
    echo [FAIL] 没找到 python，请先安装 Python 3.9+ 并勾选 Add to PATH
    echo        https://www.python.org/downloads/
    pause
    exit /b 127
)

echo.
python -V
echo   entry : %~dp0main.py
if not "%~1"=="" echo   args  : %*
echo.

python main.py %*
set RC=%errorlevel%
if not "%RC%"=="0" (
    echo.
    echo [FAIL] 退出码 %RC%，上面有具体原因
    pause
)
exit /b %RC%

:runcheck
python main.py --check
set RC=%errorlevel%
if not "%RC%"=="0" pause
exit /b %RC%

:runinit
python main.py --init
set RC=%errorlevel%
if not "%RC%"=="0" pause
exit /b %RC%

:runsched
rem NOTE: %* always expands to the ORIGINAL argument list, so after
rem       "shift" it still contains "schedule". Rebuild it by hand.
shift
set ARGS=
:runsched_collect
if "%~1"=="" goto :runsched_go
set ARGS=%ARGS% %1
shift
goto :runsched_collect
:runsched_go
python main.py --schedule %ARGS%
set RC=%errorlevel%
if not "%RC%"=="0" pause
exit /b %RC%
