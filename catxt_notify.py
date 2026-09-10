"""
catxt_notify.py — Windows toast notifications for CATXT Sync
=============================================================
Tries plyer first (reliable with PyInstaller), falls back to a raw
PowerShell WinRT call.  All failures are silent — notifications are
best-effort and must never crash the sync process.
"""

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "CATXT Sync"
_ICON_PATH = str(Path(__file__).parent / "assets" / "catxt.ico")


def notify(title: str, message: str) -> None:
    """Show a Windows toast notification. Never raises."""
    try:
        _notify_plyer(title, message)
        return
    except Exception:
        pass
    try:
        _notify_powershell(title, message)
        return
    except Exception as exc:
        log.debug(f"Toast notification failed: {exc}")
    # Last-resort fallback — Win32 MessageBox, no Tk root needed
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)  # MB_ICONINFORMATION
    except Exception:
        pass


def _notify_plyer(title: str, message: str) -> None:
    from plyer import notification  # type: ignore
    icon = _ICON_PATH if Path(_ICON_PATH).exists() else ""
    notification.notify(
        title=title,
        message=message,
        app_name=APP_NAME,
        app_icon=icon,
        timeout=8,
    )


def _notify_powershell(title: str, message: str) -> None:
    """
    Fallback: fire a toast via PowerShell using Windows.UI.Notifications WinRT.
    Works on Windows 10/11 without any extra packages.
    """
    # Escape XML special characters
    def _esc(s: str) -> str:
        return (
            s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&apos;")
        )

    xml = (
        "<toast>"
        "<visual><binding template='ToastGeneric'>"
        f"<text>{_esc(title)}</text>"
        f"<text>{_esc(message)}</text>"
        "</binding></visual>"
        "</toast>"
    )

    # Use PowerShell's own registered AUMID — always works regardless of
    # whether CATXT is installed as a Start Menu app.
    AUMID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
    ps = f"""
$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime]
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('{xml.replace("'", "''")}')
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{AUMID}').Show($toast)
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
        capture_output=True,
        timeout=10,
    )
