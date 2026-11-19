@echo off
setlocal
cd /d "%~dp0"

where pyw >nul 2>&1
if %errorlevel%==0 (
    start "" pyw -3 "%~dp0local_agent.py"
    exit /b 0
)

where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw "%~dp0local_agent.py"
    exit /b 0
)

where py >nul 2>&1
if %errorlevel%==0 (
    py -3 "%~dp0local_agent.py"
    goto :done
)

where python >nul 2>&1
if %errorlevel%==0 (
    python "%~dp0local_agent.py"
    goto :done
)

echo.
echo Python 3 was not found on this PC.
echo Either download the standalone LocalAgent-Windows.exe from GitHub Releases,
echo or install Python 3 from https://www.python.org/downloads/ (check "Add to PATH").
echo.
pause

:done
if %errorlevel% neq 0 pause
