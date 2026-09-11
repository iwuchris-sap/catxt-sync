@echo off
setlocal
cd /d "%~dp0"

rem  Output under user profile — less likely to be blocked by endpoint security
set BUILDROOT=%LOCALAPPDATA%\CATXT-build

echo ============================================================
echo  CATXT Sync - Windows App Build
echo ============================================================
echo.

echo [0/4] Killing any running CATXT instance and cleaning previous build...
taskkill /f /im CATXT.exe 2>nul
timeout /t 2 /nobreak >nul
rmdir /s /q "%BUILDROOT%" 2>nul
echo       Done.
echo.

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
    echo          The app will still work if Edge is installed, but
    echo          the driver may need to be installed manually.
)

echo.
echo [3/4] Running PyInstaller...
python -m PyInstaller CATXT.spec --noconfirm ^
  --distpath "%BUILDROOT%\dist" ^
  --workpath "%BUILDROOT%\work"
if errorlevel 1 (
    echo ERROR: PyInstaller failed.
    pause & exit /b 1
)

rem  Give endpoint security a moment to finish scanning before we touch the exe
timeout /t 5 /nobreak >nul

echo.
echo [4/4] Copying runtime data files...
if not exist "%BUILDROOT%\dist\CATXT" (
    echo ERROR: %BUILDROOT%\dist\CATXT not found — PyInstaller may have failed silently.
    pause & exit /b 1
)

rem  config.json ships as a template; don't overwrite if user already has one
if not exist "%BUILDROOT%\dist\CATXT\_internal\config.json" (
    copy "config.json" "%BUILDROOT%\dist\CATXT\_internal\config.json" >nul
    echo       Copied starter config.json
)

echo.
echo [4b] Unblocking exe (removes internet-zone marker)...
powershell -Command "Unblock-File -Path '$env:LOCALAPPDATA\CATXT-build\dist\CATXT\CATXT.exe'" 2>nul
echo       Done.

echo.
echo ============================================================
echo  Build complete!
echo  Executable: %BUILDROOT%\dist\CATXT\CATXT.exe
echo.
echo  To register a 5pm daily Task Scheduler job:
echo    schtasks /create /tn "CATXT Sync" ^
echo             /tr "%BUILDROOT%\dist\CATXT\CATXT.exe" ^
echo             /sc daily /st 17:00 /f
echo ============================================================
echo.
pause
