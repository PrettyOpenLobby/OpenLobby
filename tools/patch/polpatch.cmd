@echo off
rem Double-click to make a .polpatch from a title installed on this PC.
rem You can also drag the title's install folder onto this file.
rem See docs\polpatch.md. The .polpatch is written next to this file unless
rem you choose another folder.
setlocal
set "PY="
set "CHECK=import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "%CHECK%" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    python -c "%CHECK%" >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo Python 3.9 or newer was not found on this PC.
    echo Install it from https://www.python.org/downloads/ and tick
    echo "Add python.exe to PATH" in the installer, then run this again.
    echo.
    pause
    exit /b 1
)
%PY% "%~dp0polpatch.py" wizard --out-dir "%~dp0." %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
