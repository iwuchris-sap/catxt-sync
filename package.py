"""
package.py — Build the CATXT Sync distributable zip (Option A: Python source).

Run from inside windows_app/:
    python package.py

Output: CATXT-Sync-<version>.zip in the current directory.

What's included:
    - All tray app Python source files
    - VBS launcher, setup script, shortcut helper
    - config_template.json (blank — never personal config.json)
    - requirements files
    - assets/ (icon)
    - catxt-sync.md (Joule skill file)
    - skills/catxt-sync/SKILL.md
    - README.md

What's excluded (personal data / build artefacts):
    - config.json, .auth_cookies.json, .owa_session.json
    - .processed_events.json, catxt_sync.log, catxt_app.pid
    - catxt_sync.py (original CLI — not needed by end users)
    - dist/, build/, __pycache__/, .git/
    - CATXT.spec, build.bat (Option B build files)
    - SUBMISSION_NARRATIVE.md, DEVLOG.md (internal docs)
"""

import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
# Single source of truth: APP_VERSION in catxt_core.py
sys.path.insert(0, str(HERE))
from catxt_core import APP_VERSION as VERSION  # noqa: E402

OUTPUT = HERE / f"CATXT-Sync-{VERSION}.zip"

# Files/dirs to include (relative to HERE)
INCLUDE_FILES = [
    "catxt_app.py",
    "catxt_core.py",
    "catxt_mcp_server.py",
    "catxt_mcp_launcher.py",
    "catxt_review.py",
    "catxt_settings.py",
    "catxt_wizard.py",
    "catxt_notify.py",
    "launch_catxt.vbs",
    "setup.bat",
    "create_shortcuts.bat",
    "config_template.json",
    "requirements_app.txt",
    "requirements_mcp.txt",
    "catxt-sync.md",
    "README.md",
    "SETUP-GUIDE.md",
]

INCLUDE_DIRS = [
    "assets",
    "skills",
]

# Confirm nothing personal slips through
NEVER_INCLUDE = {
    "config.json",
    ".auth_cookies.json",
    ".owa_session.json",
    ".processed_events.json",
    "catxt_sync.log",
    "catxt_app.pid",
    "catxt_sync.py",
    "SUBMISSION_NARRATIVE.md",
    "DEVLOG.md",
    "build.bat",
    "CATXT.spec",
    "package.py",
}

def should_exclude(path: Path) -> bool:
    parts = set(path.parts)
    if "__pycache__" in parts:
        return True
    if ".git" in parts:
        return True
    if path.name in NEVER_INCLUDE:
        return True
    if path.suffix in (".pyc", ".pyo", ".log", ".pid"):
        return True
    return False


def build_zip():
    added = []
    skipped = []

    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as zf:
        # Individual files
        for name in INCLUDE_FILES:
            p = HERE / name
            if not p.exists():
                print(f"  WARN: {name} not found — skipping")
                skipped.append(name)
                continue
            arc = f"CATXT-Sync/{name}"
            zf.write(p, arc)
            added.append(arc)

        # Directories (recursive)
        for dirname in INCLUDE_DIRS:
            d = HERE / dirname
            if not d.exists():
                print(f"  WARN: {dirname}/ not found — skipping")
                skipped.append(dirname)
                continue
            for f in sorted(d.rglob("*")):
                if f.is_file() and not should_exclude(f):
                    arc = f"CATXT-Sync/{f.relative_to(HERE).as_posix()}"
                    zf.write(f, arc)
                    added.append(arc)

    print(f"\nCATXT-Sync-{VERSION}.zip created")
    print(f"  {len(added)} file(s) included")
    if skipped:
        print(f"  {len(skipped)} file(s) skipped (not found)")
    print(f"\nContents:")
    for a in added:
        print(f"  {a}")
    print(f"\nOutput: {OUTPUT}")


if __name__ == "__main__":
    if OUTPUT.exists():
        OUTPUT.unlink()
        print(f"Removed old {OUTPUT.name}")
    build_zip()
