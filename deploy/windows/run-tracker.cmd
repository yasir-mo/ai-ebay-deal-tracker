@echo off
REM Launcher for the deal tracker on Windows.
REM Adjust INSTALL_DIR if the repository lives elsewhere.

set "INSTALL_DIR=%~dp0..\.."
set "PYTHONPATH=%INSTALL_DIR%\src"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

cd /d "%INSTALL_DIR%"

REM Load .env if present (KEY=VALUE per line, # for comments)
if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    echo %%A | findstr /b "#" >nul || if not "%%A"=="" set "%%A=%%B"
  )
)

python -m tracker run -c "%INSTALL_DIR%\profiles.toml" >> "%INSTALL_DIR%\tracker.log" 2>&1
