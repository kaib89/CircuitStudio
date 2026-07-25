@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM Console stays open: it is the stop button for the local server.
where uv >nul 2>nul
if %errorlevel%==0 (
    uv run python -m circuitstudio %*
) else (
    python -m circuitstudio %*
)

echo.
echo CircuitStudio has stopped.
pause
