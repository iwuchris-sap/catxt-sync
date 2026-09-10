@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  CATXT Sync - First-Time Setup
echo ============================================================
echo.

rem ── Check Python is available ────────────────────────────────
where pythonw.exe >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found on PATH.
    echo.
    echo  Please install Python from https://www.python.org/downloads/
    echo  and make sure "Add Python to PATH" is checked during install.
    echo.
    pause & exit /b 1
)
echo  Found Python:
python --version
echo.

rem ── Install pip dependencies ─────────────────────────────────
echo [1/4] Installing Python dependencies...
pip install -r requirements_app.txt
if errorlevel 1 (
    echo ERROR: pip install failed.
    pause & exit /b 1
)

echo.
echo [2/4] Installing Playwright Edge driver...
playwright install msedge
if errorlevel 1 (
    echo WARNING: playwright install msedge failed.
    echo          The app will still work if Edge is already installed,
    echo          but the driver may need to be installed manually.
)

rem ── Copy config template if no config exists yet ─────────────
echo.
echo [2/4] Setting up config...
if not exist "config.json" (
    copy "config_template.json" "config.json" >nul
    echo       Created config.json from template.
    echo       Open config.json to add your WBS codes and project mappings.
) else (
    echo       config.json already exists — skipping.
)

rem ── Register MCP server as a logon startup task ────────────────
echo.
echo [3/4] Registering CATXT MCP Server as a startup task...
echo       (This lets Joule reach the server even when the tray app is closed)
schtasks /create /tn "CATXT MCP Server" /tr "pythonw.exe \"%SCRIPT_DIR%\catxt_mcp_server.py\"" /sc ONLOGON /rl LIMITED /f >nul 2>&1
if errorlevel 1 (
    echo       WARNING: Could not create scheduled task. You can start the MCP
    echo       server manually by running catxt_mcp_server.py, or it will start
    echo       automatically when the tray app launches.
) else (
    echo       Startup task created. The MCP server will start at next login.
    echo       To start it now without rebooting, run:
    echo         schtasks /run /tn "CATXT MCP Server"
)

rem ── Create Desktop shortcut ───────────────────────────────────
echo.
echo [4/4] Creating Desktop shortcut...
set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
powershell -NoProfile -Command ^
  "$ws = New-Object -ComObject WScript.Shell; ^
   $sc = $ws.CreateShortcut([Environment]::GetFolderPath('Desktop') + '\CATXT Sync.lnk'); ^
   $sc.TargetPath = 'wscript.exe'; ^
   $sc.Arguments = '\"%SCRIPT_DIR%\launch_catxt.vbs\"'; ^
   $sc.IconLocation = '%SCRIPT_DIR%\assets\catxt.ico'; ^
   $sc.Description = 'CATXT Sync Tray App'; ^
   $sc.Save()"
echo       Shortcut created on Desktop.

echo.
echo ============================================================
echo  Setup complete!
echo.
echo  Next steps:
echo    1. Open config.json and add your WBS codes / project mappings
echo    2. Double-click "CATXT Sync" on your Desktop to launch
echo    3. Right-click the tray icon to run a sync or open settings
echo ============================================================
echo.
pause
