@echo off
setlocal
cd /d "%~dp0"

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "VBS=%SCRIPT_DIR%\launch_catxt.vbs"
set "ICO=%SCRIPT_DIR%\assets\catxt.ico"

echo Creating CATXT Sync shortcuts...

:: Use VBScript to create the .lnk files (works without admin rights)
set "TMPVBS=%TEMP%\make_lnk.vbs"

:: ── Desktop shortcut ─────────────────────────────────────────────────────────
set "DESKTOP=%USERPROFILE%\Desktop"
if not exist "%DESKTOP%" set "DESKTOP=%USERPROFILE%\OneDrive - SAP SE\Desktop"
set "LNK=%DESKTOP%\CATXT Sync.lnk"

(
echo Set ws = CreateObject("WScript.Shell"^)
echo Set sc = ws.CreateShortcut("%LNK%"^)
echo sc.TargetPath = "wscript.exe"
echo sc.Arguments = Chr(34^) ^& "%VBS%" ^& Chr(34^)
echo sc.IconLocation = "%ICO%"
echo sc.Description = "CATXT Sync Tray App"
echo sc.Save
) > "%TMPVBS%"
cscript //nologo "%TMPVBS%"
if exist "%LNK%" (
    echo   [OK] Desktop shortcut created: %LNK%
) else (
    echo   [WARN] Could not create Desktop shortcut.
)

:: ── Startup folder entry ─────────────────────────────────────────────────────
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LNK=%STARTUP%\CATXT Sync.lnk"

(
echo Set ws = CreateObject("WScript.Shell"^)
echo Set sc = ws.CreateShortcut("%LNK%"^)
echo sc.TargetPath = "wscript.exe"
echo sc.Arguments = Chr(34^) ^& "%VBS%" ^& Chr(34^)
echo sc.IconLocation = "%ICO%"
echo sc.Description = "CATXT Sync Tray App"
echo sc.Save
) > "%TMPVBS%"
cscript //nologo "%TMPVBS%"
if exist "%LNK%" (
    echo   [OK] Startup entry created: %LNK%
) else (
    echo   [WARN] Could not create Startup entry.
)

del "%TMPVBS%" 2>nul

echo.
echo Done! CATXT Sync will now:
echo   - Launch from your Desktop shortcut
echo   - Start automatically every time you log into Windows
echo.
pause
