@echo off
setlocal
cd /d "%~dp0"
set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

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
    echo  Make sure "Add Python to PATH" is checked during install.
    echo.
    pause & exit /b 1
)
echo  Found Python:
python --version
echo.

rem ── Install pip dependencies ─────────────────────────────────
echo [1/5] Installing Python dependencies...
pip install -r requirements_app.txt
if errorlevel 1 (
    echo ERROR: pip install failed.
    pause & exit /b 1
)
pip install -r requirements_mcp.txt
if errorlevel 1 (
    echo ERROR: pip install (MCP server deps) failed.
    pause & exit /b 1
)

echo.
echo [2/5] Installing Playwright Edge driver...
playwright install msedge
if errorlevel 1 (
    echo WARNING: playwright install msedge failed.
    echo          The app will still work if Edge is already installed,
    echo          but the driver may need to be installed manually.
)

rem ── Copy config template if no config exists yet ─────────────
echo.
echo [3/5] Setting up config...
if not exist "config.json" (
    copy "config_template.json" "config.json" >nul
    echo       Created config.json from template.
) else (
    echo       config.json already exists — skipping.
)

rem ── Register MCP server as a logon startup task ──────────────
echo.
echo [4/5] Registering CATXT MCP Server as a startup task...
echo       (Lets Joule reach the server even when the tray app GUI is closed)
schtasks /create /tn "CATXT MCP Server" /tr "pythonw.exe \"%SCRIPT_DIR%\catxt_mcp_server.py\"" /sc ONLOGON /rl LIMITED /f >nul 2>&1
if errorlevel 1 (
    echo       WARNING: Could not create scheduled task.
    echo       The MCP server will still start automatically when the tray app launches.
) else (
    echo       Done. MCP server will start at next login.
)

rem ── Create Desktop shortcut + Startup folder entry ───────────
echo.
echo [5/5] Creating Desktop shortcut and startup entry...
set "TMPVBS=%TEMP%\catxt_make_lnk.vbs"

rem ── Desktop shortcut ─────────────────────────────────────────
set "DESKTOP=%USERPROFILE%\Desktop"
if not exist "%DESKTOP%" set "DESKTOP=%USERPROFILE%\OneDrive - SAP SE\Desktop"
set "LNK=%DESKTOP%\CATXT Sync.lnk"

(
echo Set ws = CreateObject("WScript.Shell"^)
echo Set sc = ws.CreateShortcut("%LNK%"^)
echo sc.TargetPath = "wscript.exe"
echo sc.Arguments = Chr(34^) ^& "%SCRIPT_DIR%\launch_catxt.vbs" ^& Chr(34^)
echo sc.IconLocation = "%SCRIPT_DIR%\assets\catxt.ico"
echo sc.Description = "CATXT Sync Tray App"
echo sc.Save
) > "%TMPVBS%"
cscript //nologo "%TMPVBS%"
if exist "%LNK%" (
    echo       Desktop shortcut created.
) else (
    echo       WARNING: Could not create Desktop shortcut.
    echo       Run create_shortcuts.bat manually to create it.
)

rem ── Startup folder entry ─────────────────────────────────────
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LNK=%STARTUP%\CATXT Sync.lnk"

(
echo Set ws = CreateObject("WScript.Shell"^)
echo Set sc = ws.CreateShortcut("%LNK%"^)
echo sc.TargetPath = "wscript.exe"
echo sc.Arguments = Chr(34^) ^& "%SCRIPT_DIR%\launch_catxt.vbs" ^& Chr(34^)
echo sc.IconLocation = "%SCRIPT_DIR%\assets\catxt.ico"
echo sc.Description = "CATXT Sync Tray App"
echo sc.Save
) > "%TMPVBS%"
cscript //nologo "%TMPVBS%"
if exist "%LNK%" (
    echo       Startup entry created. CATXT Sync will launch at next login.
) else (
    echo       WARNING: Could not create Startup entry.
    echo       Run create_shortcuts.bat manually to create it.
)

del "%TMPVBS%" 2>nul

echo.
echo ============================================================
echo  Setup complete!
echo.
echo  Next steps:
echo    1. Double-click "CATXT Sync" on your Desktop to launch
echo    2. The setup wizard will run on first launch to configure
echo       your WBS projects and cost centre
echo    3. In Joule Work Desktop, go to Settings - Extensions and
echo       add: http://127.0.0.1:7432/mcp  (name it CATXT Sync)
echo    4. Install the catxt-sync skill (drag catxt-sync.md onto
echo       the Skills panel in Joule, or use Settings - Skills)
echo    5. CATXT Sync will auto-start at next Windows login
echo ============================================================
echo.
pause
