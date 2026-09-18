"""
catxt_mcp_launcher.py — Watchdog launcher for catxt_mcp_server.py.

This is what the Windows Scheduled Task runs (instead of catxt_mcp_server.py
directly).  If the MCP server exits for any reason — crash, unhandled
exception, port conflict — the launcher waits and restarts it automatically.

Restart behaviour:
  - Clean exit (code 0)   → restart immediately (e.g. deliberate shutdown/update)
  - Crash (non-zero code) → restart with exponential back-off, capped at 60 s
  - Repeated failures     → back-off increases to avoid hammering a broken state

All events are logged to catxt_mcp.log in the same directory.
"""

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE          = Path(__file__).parent
LOG_FILE      = HERE / "catxt_mcp.log"
SERVER        = HERE / "catxt_mcp_server.py"
BASE_DELAY    = 10   # seconds between restart attempts
MAX_DELAY     = 60   # cap on back-off delay


def _log(msg: str) -> None:
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [LAUNCHER] {msg}\n"
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass  # nothing we can do if the log file itself is unavailable


def main() -> None:
    _log("=" * 60)
    _log("Watchdog launcher started.")
    consecutive_failures = 0

    while True:
        _log(f"Starting catxt_mcp_server.py...")
        try:
            result = subprocess.run(
                [sys.executable, str(SERVER)],
                cwd=str(HERE),
            )
            exit_code = result.returncode
        except Exception as exc:
            _log(f"Failed to launch server process: {exc}")
            exit_code = -1

        if exit_code == 0:
            _log("Server exited cleanly (code 0).")
            consecutive_failures = 0
            delay = BASE_DELAY
        else:
            consecutive_failures += 1
            _log(
                f"Server exited with code {exit_code} "
                f"(consecutive failures: {consecutive_failures})."
            )
            # Exponential back-off so we don't hammer a broken state.
            delay = min(BASE_DELAY * consecutive_failures, MAX_DELAY)

        _log(f"Restarting in {delay}s...")
        time.sleep(delay)


if __name__ == "__main__":
    main()
