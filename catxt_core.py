"""
catxt_core.py — CATXT Sync shared business logic
=================================================
Imported by catxt_app.py (GUI / tray app).
The original catxt_sync.py CLI continues to work unchanged from the project root.

Key differences from catxt_sync.py:
  - No sys.exit() — raises exceptions so callers can handle cleanly.
  - No logging.basicConfig — caller configures logging.
  - PERNR auto-detected via detect_pernr() after authentication.
  - _process_day() replaced by prepare_day() + post_day() so the GUI can
    sit between calendar fetch and posting (the review dialog).
  - sync_staffing() fills Tasklevel from staffing data per project.
  - prompt_for_mapping() (stdin) removed; unmapped events returned to caller.
  - Bug fix: authenticate_via_browser() closes via context.close(), not
    browser.close() (browser is undefined when persistent profile is used).
"""

import ctypes
import ctypes.wintypes
import json
import logging
import os
import subprocess
import threading
import time
import requests
from datetime import datetime, date, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Version
# ══════════════════════════════════════════════════════════════════════════════

APP_VERSION = "1.2.12"


def check_for_update(config: dict) -> dict:
    """Check whether a newer version of CATXT Sync is available.

    Reads ``update_check_url`` from config and fetches a JSON file of the form::

        {"version": "x.y.z", "download_url": "https://...", "changes": "..."}

    Returns a dict with:
        local_version    — installed version (APP_VERSION)
        update_available — True if remote version > local version
        latest_version   — version string from the remote file (or None on error)
        download_url     — link to download the update (or "")
        changes          — brief changelog from the remote file (or "")
        note / error     — present only when the check was skipped or failed
    """
    url = config.get("update_check_url", "").strip()
    base: dict = {
        "local_version":    APP_VERSION,
        "update_available": False,
        "latest_version":   None,
        "download_url":     "",
        "changes":          "",
    }
    if not url:
        base["note"] = (
            "update_check_url is not configured in config.json — "
            "version check disabled."
        )
        return base

    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        base["error"] = f"Could not reach update server: {exc}"
        return base

    remote_ver = str(data.get("version", "")).strip()
    if not remote_ver:
        base["error"] = "Remote version file did not contain a 'version' field."
        return base

    try:
        local_parts  = tuple(int(x) for x in APP_VERSION.split("."))
        remote_parts = tuple(int(x) for x in remote_ver.split("."))
        update_available = remote_parts > local_parts
    except ValueError:
        base["error"] = (
            f"Could not compare versions: '{APP_VERSION}' vs '{remote_ver}'."
        )
        return base

    return {
        "local_version":    APP_VERSION,
        "update_available": update_available,
        "latest_version":   remote_ver,
        "download_url":     data.get("download_url", ""),
        "changes":          data.get("changes", ""),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Windows-API browser window suppressor
# ══════════════════════════════════════════════════════════════════════════════

def _chrome_hwnds() -> set:
    """Return the set of Chrome_WidgetWin_1 window handles currently open."""
    user32  = ctypes.windll.user32
    handles: list = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )

    def _cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buf, 64)
        if "Chrome_WidgetWin_1" in buf.value:
            handles.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return set(handles)


def _suppress_new_browser_window(
    pre_pids: set,
    timeout: float = 15.0,
    hidden_out: list | None = None,
):
    """
    Background thread: find windows belonging to newly-started Edge processes
    (identified by PID diff from pre_pids) and hide them immediately.

    Uses GetWindowThreadProcessId which is more reliable than matching window
    class names — it works regardless of which class Edge assigns its windows.

    hidden_out — if provided, every hidden HWND is appended so the caller
                 can restore them later (e.g. if SSO needs user interaction).
    """
    user32   = ctypes.windll.user32
    SW_HIDE  = 0

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )

    deadline    = time.time() + timeout
    hidden:set  = set()
    target_pids = set()

    while time.time() < deadline:
        time.sleep(0.2)

        # Refresh which PIDs are ours (Edge spawns child processes over time)
        target_pids |= (_msedge_pids() - pre_pids)
        if not target_pids:
            continue

        found: list = []

        def _cb(hwnd, _lp):
            w_pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(w_pid))
            if w_pid.value in target_pids and hwnd not in hidden:
                found.append(hwnd)
            return True

        _proc = WNDENUMPROC(_cb)   # keep ref alive for duration of EnumWindows
        user32.EnumWindows(_proc, 0)

        for hwnd in found:
            user32.ShowWindow(hwnd, SW_HIDE)
            hidden.add(hwnd)
            if hidden_out is not None:
                hidden_out.append(hwnd)
            log.debug(f"Suppressed browser window hwnd={hwnd:#010x} pid-based")

        if hidden:
            deadline = min(deadline, time.time() + 5.0)


def _restore_browser_windows(hwnds: list) -> None:
    """Restore windows that were hidden by _suppress_new_browser_window."""
    user32 = ctypes.windll.user32
    for hwnd in hwnds:
        user32.ShowWindow(hwnd, 9)       # SW_RESTORE
        user32.SetForegroundWindow(hwnd)


def _close_new_browser_windows(pre_existing: set) -> None:
    """
    Close any Chrome/Edge windows that appeared since the pre-snapshot.
    Called after context.close() as a guaranteed cleanup in case Playwright
    left any windows open.
    """
    user32 = ctypes.windll.user32
    WM_CLOSE = 0x0010

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )
    new_handles: list = []

    def _cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buf, 64)
        if "Chrome_WidgetWin_1" in buf.value and hwnd not in pre_existing:
            new_handles.append(hwnd)
        return True

    _proc = WNDENUMPROC(_cb)
    user32.EnumWindows(_proc, 0)

    for hwnd in new_handles:
        log.debug(f"Closing lingering browser window hwnd={hwnd:#010x}")
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


# ── PID-based browser cleanup (guaranteed) ────────────────────────────────────

def _msedge_pids() -> set:
    """Return the current set of msedge.exe process IDs."""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        pids: set = set()
        for line in r.stdout.strip().splitlines():
            parts = line.strip('"').split('","')
            if len(parts) >= 2:
                try:
                    pids.add(int(parts[1]))
                except ValueError:
                    pass
        return pids
    except Exception:
        return set()


def _kill_new_edge_pids(pre_pids: set) -> None:
    """
    Force-kill any msedge.exe processes that started after pre_pids was
    taken.  /T kills the entire process tree (renderer, GPU helpers, etc.).
    Only targets NEW pids — leaves the user's running Edge untouched.
    """
    new_pids = _msedge_pids() - pre_pids
    for pid in new_pids:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=5,
            )
            log.debug(f"Killed Edge process tree PID={pid}")
        except Exception:
            pass

# ── Paths ──────────────────────────────────────────────────────────────────────
APP_DIR          = Path(__file__).parent
CONFIG_FILE      = APP_DIR / "config.json"
COOKIES_FILE     = APP_DIR / ".auth_cookies.json"
PROCESSED_FILE   = APP_DIR / ".processed_events.json"
LOG_FILE         = APP_DIR / "catxt_sync.log"
OWA_SESSION_FILE = APP_DIR / ".owa_session.json"   # cached OWA cookies + request template


# ── Minimal Edge profile copy ─────────────────────────────────────────────────

def _copy_edge_profile_minimal(src_profile: str, dst_profile: str) -> None:
    """
    Copy only the auth-essential files from an Edge profile to dst_profile.
    Copies ~a few MB instead of potentially several GB — makes the browser
    fallback start in seconds rather than minutes.

    Copied: Cookies (session), Login Data (credentials), Preferences,
            Secure Preferences, Web Data, Network/ subfolder, Local State,
            First Run.  Everything else (cache, extensions, history …) is skipped.
    """
    import shutil as _sh
    import sqlite3 as _sq

    def _cp(src: str, dst: str) -> None:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        name = os.path.basename(src)
        # SQLite-aware copy: read locked DBs without needing an exclusive lock
        if name in ("Cookies", "Login Data", "Login Data For Account", "Web Data"):
            try:
                uri = "file:" + src.replace("\\", "/") + "?mode=ro&immutable=1"
                sc  = _sq.connect(uri, uri=True)
                dc  = _sq.connect(dst)
                sc.backup(dc, pages=200)
                sc.close(); dc.close()
                return
            except Exception:
                pass
        try:
            _sh.copy2(src, dst)
        except OSError:
            pass

    def _cp_dir(src: str, dst: str) -> None:
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            s, d = os.path.join(src, name), os.path.join(dst, name)
            (_cp_dir if os.path.isdir(s) else _cp)(s, d)

    if os.path.exists(dst_profile):
        _sh.rmtree(dst_profile, ignore_errors=True)

    # Top-level profile files
    for name in ("Local State", "First Run"):
        s = os.path.join(src_profile, name)
        if os.path.isfile(s):
            _cp(s, os.path.join(dst_profile, name))

    # Auth-critical files from Default/
    for name in ("Cookies", "Login Data", "Login Data For Account",
                 "Preferences", "Secure Preferences", "Web Data"):
        s = os.path.join(src_profile, "Default", name)
        if os.path.isfile(s):
            _cp(s, os.path.join(dst_profile, "Default", name))

    # Network/ subfolder — contains HSTS + transport auth state (small)
    net_src = os.path.join(src_profile, "Default", "Network")
    if os.path.isdir(net_src):
        _cp_dir(net_src, os.path.join(dst_profile, "Default", "Network"))

# ── CATXT API constants ────────────────────────────────────────────────────────
BASE_URL = (
    "https://sapit-fulfillment-prod-zebra.launchpad.cfapps.eu10.hana.ondemand.com"
    "/44815ef2-21db-4bf3-9dbc-9fbd52e91a85.sapcomcatsxtv2catsxtui5.sapcomcatsxtv2"
    "/sap/opu/odata/sap/ZCATSXTMO"
)
LOGIN_URL = (
    "https://sapit-home-prod-004.launchpad.cfapps.eu10.hana.ondemand.com"
    "/site#MyTimesheet-Display?sap-ui-app-id-hint=beaeeca8-8c37-4630-8763-5e4205a781b3"
)
OWA_CALENDAR_URL = "https://outlook.office.com/calendar/view/day/{date}"

EXCLUDED_SUBJECTS = [
    "catxt", "cat entry", "time entry", "timesheet entry",
    "administrative tasks",
    "sick day", "sick leave", "half sick",
    "enter time into catsxt",
]

# ── Known task types (fallback if API fetch fails) ─────────────────────────────
# Sub-types are populated at runtime via sync_catxt_metadata(); this dict
# gives the correct labels even if the session is offline.
KNOWN_TASK_TYPES: dict[str, dict] = {
    "ADMI": {"label": "Administrative",            "subtypes": []},
    "BREA": {"label": "Break",                     "subtypes": []},
    "CFPP": {"label": "Cust. Facing Project Work", "subtypes": []},  # required for WBS/project entries
    "EDUC": {"label": "Personal education",        "subtypes": []},
    "ICON": {"label": "Internal projectwork",      "subtypes": []},
    "ICOS": {"label": "Strategic Internal Prjtwrk","subtypes": []},
    "MEET": {"label": "Meetings",                  "subtypes": []},
    "MENT": {"label": "Mentoring",                 "subtypes": []},
    "OPEN": {"label": "Open Activities",           "subtypes": []},
    "PREP": {"label": "Preparation for teaching",  "subtypes": []},
    "TOLO": {"label": "Time in Lieu of Overtime",  "subtypes": []},
}

def get_task_types(config: dict) -> dict:
    """Return the cached task-type dict, falling back to KNOWN_TASK_TYPES."""
    return config.get("_task_types") or KNOWN_TASK_TYPES

# ── PERNR / KOSTL ───────────────────────────────────────────────────────────────
# Fallback values; both overwritten by detect_pernr() after auth once the
# Userinfo endpoint is successfully called.  Left blank deliberately — if
# auto-detection fails the user gets a clear API error rather than silently
# posting entries to the wrong person's timesheet.
PERNR: str = ""
KOSTL: str = ""   # user's home cost centre (Skostl for WBS entries)


def detect_pernr(session: requests.Session) -> str:
    """
    Fetch the authenticated user's profile from /Userinfo and cache key fields
    (Pernr, Kostl, Objnr) in config.json.  Updates the module-level PERNR and
    KOSTL variables.  Safe to call on every startup — returns cached value if
    already known.
    """
    global PERNR, KOSTL
    config = load_config()
    if config.get("_pernr"):
        PERNR = config["_pernr"]
        if config.get("_kostl"):
            KOSTL = config["_kostl"]
        log.debug(f"User profile from cache: PERNR={PERNR} KOSTL={KOSTL}")
        return PERNR
    try:
        r = session.get(f"{BASE_URL}/Userinfo", timeout=15)
        if r.status_code == 200:
            d = r.json().get("d") or {}
            # Userinfo returns a results collection, not a bare entity
            results = d.get("results", [])
            user = results[0] if results else d

            pernr = user.get("Pernr", "").strip()
            kostl = user.get("Kostl", "").strip()
            objnr = user.get("Objnr", "").strip()

            if pernr and pernr.lstrip("0"):
                PERNR = pernr
                config["_pernr"] = pernr
                log.info(f"PERNR auto-detected: {pernr}")
                if kostl:
                    KOSTL = kostl
                    config["_kostl"] = kostl
                    log.info(f"Cost centre auto-detected: {kostl} ({user.get('Kostx', '')})")
                if objnr:
                    config["_objnr"] = objnr
                save_config(config)
                return PERNR
    except Exception as exc:
        log.warning(f"Userinfo fetch failed: {exc}")
    log.warning(f"User profile not auto-detected — using fallbacks: PERNR={PERNR} KOSTL={KOSTL}")
    return PERNR


# ══════════════════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def save_config(config: dict) -> None:
    import traceback as _tb
    # Diagnostic: log keywords on every write so we can trace overwrites
    _kw_summary = {
        m.get("label", "?")[:30]: m.get("keywords", [])
        for m in config.get("wbs_mappings", [])
        if m.get("keywords")
    }
    log.debug(
        f"save_config called from:\n{''.join(_tb.format_stack(limit=4))}"
        f"  keywords present: {_kw_summary or '(none)'}"
    )
    CONFIG_FILE.write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Processed-event tracking  (avoids re-submitting on repeat runs)
# ══════════════════════════════════════════════════════════════════════════════

def load_processed() -> dict:
    if PROCESSED_FILE.exists():
        return json.loads(PROCESSED_FILE.read_text(encoding="utf-8"))
    return {}


def save_processed(processed: dict) -> None:
    PROCESSED_FILE.write_text(json.dumps(processed, indent=2), encoding="utf-8")


def clear_processed_for_range(start_date: "date", end_date: "date") -> int:
    """Remove processed event IDs for all business days in [start_date, end_date].
    Returns the number of days cleared.  Events that were never posted (e.g. failed
    entries) are also removed, making them re-appear in the next review dialog.
    Already-posted events are re-validated by the live CATXT check at post time, so
    there is no risk of double-posting.
    """
    from datetime import timedelta
    processed = load_processed()
    cleared = 0
    cur = start_date
    while cur <= end_date:
        if cur.weekday() < 5:   # business days only
            key = str(cur)
            if key in processed:
                del processed[key]
                cleared += 1
        cur += timedelta(days=1)
    save_processed(processed)
    log.info(
        f"Cleared processed cache for {cleared} day(s)  "
        f"({start_date} – {end_date})."
    )
    return cleared


# ══════════════════════════════════════════════════════════════════════════════
# Scheduled sync — date-range helpers
# ══════════════════════════════════════════════════════════════════════════════

def get_scheduled_sync_through(config: dict) -> "date | None":
    """Return the last date the scheduled sync was fully clean, or None."""
    val = config.get("_scheduled_sync_through")
    if val:
        try:
            return date.fromisoformat(val)
        except Exception:
            pass
    return None


def set_scheduled_sync_through(d: "date") -> None:
    """Advance _scheduled_sync_through to d and persist to config.json."""
    cfg = load_config()
    cfg["_scheduled_sync_through"] = d.isoformat()
    save_config(cfg)
    log.info(f"Scheduled sync: _scheduled_sync_through advanced to {d}.")


def get_scheduled_date_range(config: dict) -> "tuple[date, date]":
    """
    Compute (start_date, end_date) for a scheduled sync.

    Starts from the business day after _scheduled_sync_through, capped at
    max_scheduled_lookback_days business days in the past.  End date is today.
    On the very first scheduled run (no _scheduled_sync_through set), covers
    only today so we don't accidentally sweep months of history.
    """
    today    = date.today()
    max_lb   = int(config.get("max_scheduled_lookback_days", 10))
    through  = get_scheduled_sync_through(config)

    if through is None or through >= today:
        return today, today

    # Walk forward from `through` to find first business day after it
    start = through + timedelta(days=1)
    while start.weekday() >= 5:   # skip weekend
        start += timedelta(days=1)

    if start > today:
        return today, today

    # Clamp to max_lb business days ago
    earliest  = today
    bd_count  = 0
    cursor    = today
    while bd_count < max_lb:
        cursor -= timedelta(days=1)
        if cursor.weekday() < 5:
            bd_count += 1
            earliest  = cursor

    if start < earliest:
        log.debug(
            f"Scheduled sync: clamping start from {start} to {earliest} "
            f"(max lookback={max_lb} business days)."
        )
        start = earliest

    return start, today


def post_day_auto(
    session: "requests.Session",
    target_date: "date",
    mapped_entries: list,
    processed: dict,
) -> "tuple[list, list]":
    """
    Auto-post specifically-mapped entries for a scheduled sync.

    Unlike post_day(), this function only handles the `mapped` list (keyword /
    email / favorites matches).  Unmapped events (default-CC fallback) are NOT
    posted; the caller queues them for the review dialog.

    Returns:
        posted_ok    — list[(event, mapping)] successfully posted AND verified
        failed_pairs — list[(event, mapping)] that failed to post or were not
                       found in CATXT after verification; caller shows in dialog
    """
    if not mapped_entries:
        return [], []

    csrf_token = get_csrf_token(session)
    if not csrf_token:
        log.error("post_day_auto: could not obtain CSRF token — all entries queued for review.")
        return [], list(mapped_entries)

    existing    = get_existing_entries(session, target_date, csrf_token=csrf_token)
    existing_lc = {e.get("Ltxa1", "").strip().lower() for e in existing}
    existing_tc = {e.get("Taskcounter") for e in existing if e.get("Taskcounter")}

    date_key      = str(target_date)
    processed_ids = processed.setdefault(date_key, [])

    posted_ok:    list = []
    failed_pairs: list = []

    for event, mapping in mapped_entries:
        posted_text = (
            mapping.get("_ltxa1_override") or event["subject"]
        )[:40].strip().lower()

        if posted_text in existing_lc:
            log.info(f"  ↩  Already in CATXT: {event['subject'][:50]}")
            if event["id"] not in processed_ids:
                processed_ids.append(event["id"])
            posted_ok.append((event, mapping))
            continue

        if post_activity(session, csrf_token, event, mapping, target_date, existing_tc):
            posted_ok.append((event, mapping))
            processed_ids.append(event["id"])
        else:
            failed_pairs.append((event, mapping))

    # Post-posting verification: confirm every "OK" entry actually landed
    if posted_ok:
        try:
            verify    = get_existing_entries(session, target_date, csrf_token=csrf_token)
            verify_lc = {e.get("Ltxa1", "").strip().lower() for e in verify}
            confirmed: list = []
            for ev, mp in posted_ok:
                ptext = (
                    mp.get("_ltxa1_override") or ev["subject"]
                )[:40].strip().lower()
                if ptext in verify_lc:
                    confirmed.append((ev, mp))
                else:
                    log.error(
                        f"  ✗  {ev['subject'][:50]} — NOT found in CATXT post-verification "
                        f"(silently dropped). Queued for dialog review."
                    )
                    try:
                        processed_ids.remove(ev["id"])
                    except ValueError:
                        pass
                    failed_pairs.append((ev, mp))
            posted_ok = confirmed
        except Exception as ve:
            log.debug(f"post_day_auto: post-verification skipped: {ve}")

    log.info(
        f"post_day_auto {target_date}: "
        f"{len(posted_ok)} auto-posted OK, {len(failed_pairs)} queued for review."
    )
    return posted_ok, failed_pairs


# ══════════════════════════════════════════════════════════════════════════════
# OWA direct-API session (cached cookies + captured request template)
# ══════════════════════════════════════════════════════════════════════════════

_OWA_SESSION_TTL_HOURS = 8   # OWA session cookies typically valid for several hours


def load_owa_session() -> dict | None:
    """Load the saved OWA session if it is still within the TTL window."""
    if not OWA_SESSION_FILE.exists():
        return None
    try:
        data = json.loads(OWA_SESSION_FILE.read_text(encoding="utf-8"))
        saved = data.get("saved_at")
        if saved:
            age_h = (datetime.now() - datetime.fromisoformat(saved)).total_seconds() / 3600
            if age_h < _OWA_SESSION_TTL_HOURS:
                return data
    except Exception:
        pass
    return None


def save_owa_session(data: dict) -> None:
    data["saved_at"] = datetime.now().isoformat()
    OWA_SESSION_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_events_via_owa_direct(target_date: date, owa_session: dict) -> list | None:
    """
    Replay the captured OWA GetCalendarView request for target_date directly
    via requests — no Playwright browser needed.

    Returns a list of raw OWA CalendarItem dicts on success, or None on failure.
    None signals the caller to fall back to the Playwright browser.
    """
    template = owa_session.get("request_template")
    if not template:
        return None
    try:
        import copy as _copy
        start_str = f"{target_date.strftime('%Y-%m-%d')}T00:00:00"
        end_str   = f"{(target_date + timedelta(days=1)).strftime('%Y-%m-%d')}T00:00:00"

        session = requests.Session()
        for c in owa_session.get("cookies", []):
            session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

        # Drop transport-level headers that change per-connection
        SKIP_HDRS = {"host", "content-length", "connection", "cookie",
                     "accept-encoding", "user-agent"}
        base_headers = {k: v for k, v in template.get("headers", {}).items()
                        if k.lower() not in SKIP_HDRS}

        # ── Strategy 1: OWA REST calendarview (GET, explicit date params) ──────
        # The OWA SOAP GetCalendarView uses a pre-registered payload (n= sequence)
        # that Playwright cannot capture, so the POST body approach is unreliable.
        # The REST endpoint accepts date parameters directly in the URL and works
        # with the same OWA session cookies.
        try:
            rest_url = (
                "https://outlook.office.com/api/v2.0/me/calendarview"
                f"?startdatetime={start_str}&enddatetime={end_str}&$top=100"
            )
            rest_headers = {"Accept": "application/json; odata.metadata=none"}
            # Include OWA auth headers if available
            for hdr in ("authorization", "x-req-source", "x-owa-urlpostdata"):
                if hdr in {k.lower() for k in base_headers}:
                    rest_headers[hdr] = next(
                        v for k, v in base_headers.items() if k.lower() == hdr
                    )
            r_rest = session.get(rest_url, headers=rest_headers, timeout=15)
            if r_rest.status_code == 200:
                data = r_rest.json()
                items = data.get("value")
                if isinstance(items, list):
                    log.info(
                        f"Calendar via OWA REST API: {len(items)} event(s) "
                        f"for {target_date}"
                    )
                    return items
                log.debug(f"OWA REST: unexpected structure: {list(data.keys())[:5]}")
            else:
                log.debug(f"OWA REST: HTTP {r_rest.status_code} — trying SOAP fallback.")
        except Exception as _re:
            log.debug(f"OWA REST error: {_re} — trying SOAP fallback.")

        # ── Strategy 2: OWA SOAP GetCalendarView (POST) ─────────────────────────
        # Fall back to the original approach — works when the body was captured.
        template_body = template.get("body")
        if not template_body:
            log.debug("OWA direct: no SOAP body template available — skipping SOAP.")
            return None

        body = _copy.deepcopy(json.loads(template_body))
        if "Body" in body:
            body["Body"]["StartDate"] = start_str
            body["Body"]["EndDate"]   = end_str

        r = session.post(template["url"], json=body, headers=base_headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            items = data.get("Body", {}).get("Items")
            if items is not None:
                log.info(f"Calendar via direct OWA API: {len(items)} event(s) for {target_date}")
                return items
            log.debug(f"OWA direct: unexpected response keys: {list(data.keys())}")
        elif r.status_code in (401, 403):
            log.info(f"OWA direct: session expired (HTTP {r.status_code}) — will reopen browser.")
        else:
            log.debug(f"OWA direct: HTTP {r.status_code}")
    except Exception as exc:
        log.debug(f"OWA direct API error: {exc}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Outlook Calendar — OWA Playwright interception
# ══════════════════════════════════════════════════════════════════════════════

def get_events_for_date(target_date: date) -> list[dict]:
    """
    Return qualifying calendar events for target_date.

    Fast path (no browser): replays a cached OWA GetCalendarView request
    if a valid session file exists (written by the previous browser run).
    Falls back to Playwright + Edge profile when the session is missing or
    expired (first run, or after ~8 hours).
    """
    # ── Fast path: direct OWA API — no Playwright needed ──────────────────────
    owa_session = load_owa_session()
    if owa_session:
        direct = get_events_via_owa_direct(target_date, owa_session)
        if direct is not None:
            return _filter_events(direct, target_date)
        log.info("Saved OWA session expired or failed — opening browser.")

    raw_events: list = []

    with sync_playwright() as p:
        context = None
        edge_profile     = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")
        edge_profile_bak = os.path.expandvars(
            r"%LOCALAPPDATA%\Microsoft\Edge\User Data - CATXTSync"
        )

        # ── Strategy 1: live Edge profile (works when Edge is closed) ─────────
        _pre_pids = _msedge_pids()
        try:
            _pre = _chrome_hwnds()
            context = p.chromium.launch_persistent_context(
                user_data_dir=edge_profile,
                channel="msedge",
                headless=True,
                timeout=10_000,   # fail fast if profile is locked by a running Edge
                service_workers="block",  # force network requests; bypasses SW cache
                args=["--no-first-run", "--no-default-browser-check",
                      "--disable-sync", "--no-restore-last-session"],
            )
            log.info("OWA browser: msedge (live profile, headless)")
        except Exception:
            pass

        # ── Strategy 2: minimal profile copy so Edge can keep running ────────────
        if context is None:
            log.info("Edge profile locked — copying auth files for Playwright...")
            try:
                _copy_edge_profile_minimal(edge_profile, edge_profile_bak)
                _pre_pids = _msedge_pids()   # retake snapshot for this strategy
                _pre = _chrome_hwnds()
                context = p.chromium.launch_persistent_context(
                    user_data_dir=edge_profile_bak,
                    channel="msedge",
                    headless=True,
                    service_workers="block",  # force network requests; bypasses SW cache
                    args=["--no-first-run", "--no-default-browser-check",
                          "--disable-sync", "--no-restore-last-session"],
                )
                log.info("OWA browser: msedge (minimal profile copy, headless)")
            except Exception as copy_err:
                log.warning(f"Profile copy failed: {copy_err}")

        if context is None:
            log.error(
                "Could not open Edge with your profile.\n"
                "  Try closing Edge completely and re-running."
            )
            return []

        def on_response(response):
            try:
                url       = response.url
                url_lower = url.lower()
                if response.status == 200 and any(h in url_lower for h in [
                    "graph.microsoft.com", "substrate.office.com",
                    "outlook.office.com", "outlook.office365.com",
                ]):
                    log.debug(f"API response: [{response.status}] {url[:120]}")
                if response.status != 200:
                    return
                is_calendar = (
                    "calendarview" in url_lower
                    or "getcalendarview" in url_lower
                    or ("calendar" in url_lower and "events" in url_lower)
                )
                is_known_host = any(h in url_lower for h in [
                    "graph.microsoft.com", "substrate.office.com",
                    "outlook.office.com", "outlook.office365.com",
                ])
                if is_calendar and is_known_host:
                    try:
                        body_text = response.text()
                    except Exception as e:
                        log.debug(f"Could not read response body: {e}")
                        return
                    if "getcalendarview" in url_lower:
                        log.debug(f"GetCalendarView body (first 1000):\n{body_text[:1000]}")
                    try:
                        data = json.loads(body_text)
                    except Exception:
                        return
                    events: list = []
                    if "value" in data and isinstance(data["value"], list):
                        events = data["value"]
                    if not events:
                        owa_items = data.get("Body", {}).get("Items", [])
                        if isinstance(owa_items, list):
                            events.extend(owa_items)
                    if not events:
                        log.debug(
                            f"Calendar response top-level keys: {list(data.keys())[:10]}"
                        )
                    if events:
                        raw_events.extend(events)
                        log.info(f"Captured {len(events)} event(s) from: {url[:80]}")
                        # Backup: capture request template via response.request.
                        # on_request can miss cache-served requests; this catches them.
                        if not _captured_req:
                            try:
                                _req = response.request
                                if (_req.method == "POST"
                                        and "service.svc" in _req.url.lower()):
                                    _body = _req.post_data
                                    if _body:
                                        _captured_req.update({
                                            "url": (
                                                "https://outlook.office.com/owa/service.svc"
                                                "?action=GetCalendarView&app=Calendar"
                                            ),
                                            "headers": dict(_req.headers),
                                            "body":    _body,
                                        })
                                        log.debug(
                                            "OWA: request template captured "
                                            "via response.request."
                                        )
                            except Exception:
                                pass
            except Exception:
                pass

        # Use context.route() to intercept the GetCalendarView request before
        # it is sent — this is the only reliable way to get the POST body in
        # Playwright's sync API (context.on("request") post_data is often None
        # for requests served from the browser's cache).
        _captured_req: dict = {}

        def handle_route(route):
            req   = route.request
            url_l = req.url.lower()
            if ("getcalendarview" in url_l and "service.svc" in url_l
                    and not _captured_req):
                try:
                    _body    = req.post_data   # None when OWA uses pre-registered payload
                    _headers = dict(req.headers)
                    _captured_req.update({
                        "url": ("https://outlook.office.com/owa/service.svc"
                                "?action=GetCalendarView&app=Calendar"),
                        "headers": _headers,
                        "body":    _body,   # may be None; direct API will construct it
                    })
                    if _body:
                        log.debug("OWA: request template captured (with body).")
                    else:
                        log.debug(
                            f"OWA: request headers captured ({len(_headers)} keys); "
                            f"body will be constructed for direct API call."
                        )
                except Exception as _re:
                    log.debug(f"OWA route capture error: {_re}")
            route.continue_()

        context.route("**/service.svc**", handle_route)
        context.on("response", on_response)
        page = context.new_page()
        date_str = target_date.strftime("%Y-%m-%d")
        owa_url  = OWA_CALENDAR_URL.format(date=date_str)
        log.info(f"Opening OWA calendar for {date_str}.")
        try:
            page.goto(owa_url, timeout=60_000, wait_until="domcontentloaded")
        except Exception as nav_err:
            log.warning(f"Navigation warning: {nav_err}")

        log.info("Waiting for calendar data (up to 3 minutes)...")
        deadline = time.time() + 180
        while time.time() < deadline:
            if raw_events:
                log.info("Calendar data received.")
                break
            try:
                page.wait_for_timeout(1_000)
            except Exception:
                log.warning("Browser window closed before data was received.")
                break
        else:
            log.warning("Timed out waiting for calendar data.")

        if raw_events:
            try:
                page.wait_for_timeout(2_000)
            except Exception:
                pass
            # Persist OWA session so future syncs can skip the browser entirely
            if _captured_req:
                try:
                    owa_cookies = [
                        {"name": c["name"], "value": c["value"],
                         "domain": c["domain"], "path": c.get("path", "/")}
                        for c in context.cookies()
                        if any(d in c.get("domain", "")
                               for d in ("outlook.office", "office365.com",
                                         "microsoft.com", "live.com"))
                    ]
                    save_owa_session({
                        "request_template": _captured_req,
                        "cookies":          owa_cookies,
                    })
                    log.info(f"OWA session cached ({len(owa_cookies)} cookies) "
                             f"— next sync will use direct API.")
                except Exception as exc:
                    log.debug(f"Could not save OWA session: {exc}")
        try:
            context.close()
        except Exception:
            pass
        _close_new_browser_windows(_pre)
        _kill_new_edge_pids(_pre_pids)

    if not raw_events:
        log.warning("No calendar events captured from OWA.")
        return []

    filtered = _filter_events(raw_events, target_date)

    # If the browser captured events but none fall on target_date, OWA served
    # a cached week (usually today's).  Replay the saved session via direct
    # API — it patches StartDate/EndDate explicitly so any historical date works.
    if not filtered and _captured_req:
        _owa_now = load_owa_session()
        if _owa_now:
            _direct = get_events_via_owa_direct(target_date, _owa_now)
            if _direct is not None:
                log.info(
                    f"OWA served wrong-week cache — direct API retrieved "
                    f"{len(_direct)} event(s) for {target_date}."
                )
                return _filter_events(_direct, target_date)
        log.warning(
            f"OWA returned wrong-week data for {target_date} and direct "
            f"API unavailable — no events will be shown for this date."
        )

    return filtered



def get_events_for_date_range(dates: list) -> dict:
    """
    Return qualifying calendar events for each date in the list.

    Fast path: tries the direct OWA API for all dates before opening a browser.
    If all dates succeed, the browser is never launched.
    Falls back to a single Playwright session (one browser open for all dates)
    when the saved session is missing, expired, or returns an error.
    """
    if not dates:
        return {}

    # ── Fast path: direct OWA API for every date ──────────────────────────────
    owa_session = load_owa_session()
    if owa_session:
        results: dict = {}
        for d in dates:
            direct = get_events_via_owa_direct(d, owa_session)
            if direct is None:
                log.info("Direct OWA API failed mid-range — falling back to browser for all dates.")
                results = {}
                break
            results[d] = _filter_events(direct, d)
        if results:
            log.info(f"Range calendar via direct OWA API: {len(dates)} day(s), no browser needed.")
            return results

    all_raw: list        = []
    responded: dict      = {d: False for d in dates}
    cur                  = [dates[0]]          # mutable for on_response closure
    _captured_req_range: dict = {}             # pre-defined so post-browser code can check it

    with sync_playwright() as p:
        context      = None
        _pre_pids    = _msedge_pids()
        edge_profile     = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")
        edge_profile_bak = os.path.expandvars(
            r"%LOCALAPPDATA%\Microsoft\Edge\User Data - CATXTSync"
        )

        # Strategy 1: live profile
        try:
            _pre = _chrome_hwnds()
            context = p.chromium.launch_persistent_context(
                user_data_dir=edge_profile,
                channel="msedge",
                headless=True,
                timeout=10_000,   # fail fast if profile is locked by a running Edge
                service_workers="block",  # force network requests; bypasses SW cache
                args=["--no-first-run", "--no-default-browser-check", "--disable-sync",
                      "--no-restore-last-session"],
            )
            log.info("OWA browser (range): msedge live profile (headless)")
        except Exception:
            pass

        # Strategy 2: minimal profile copy so Edge can keep running
        if context is None:
            log.info("Edge profile locked — copying auth files for range sync...")
            try:
                _copy_edge_profile_minimal(edge_profile, edge_profile_bak)
                _pre_pids = _msedge_pids()
                _pre      = _chrome_hwnds()
                context   = p.chromium.launch_persistent_context(
                    user_data_dir=edge_profile_bak,
                    channel="msedge",
                    headless=True,
                    service_workers="block",  # force network requests; bypasses SW cache
                    args=["--no-first-run", "--no-default-browser-check", "--disable-sync",
                          "--no-restore-last-session"],
                )
                log.info("OWA browser (range): msedge minimal profile copy (headless)")
            except Exception as e:
                log.warning(f"Profile copy failed: {e}")

        if context is None:
            log.error("Could not open Edge for range calendar fetch.")
            return {d: [] for d in dates}

        def on_response(response):
            try:
                url = response.url.lower()
                if response.status != 200:
                    return
                is_cal  = ("calendarview" in url or "getcalendarview" in url or
                           ("calendar" in url and "events" in url))
                is_host = any(h in url for h in [
                    "graph.microsoft.com", "substrate.office.com",
                    "outlook.office.com", "outlook.office365.com",
                ])
                if not (is_cal and is_host):
                    return
                responded[cur[0]] = True
                try:
                    data = json.loads(response.text())
                except Exception:
                    return
                events = []
                if "value" in data and isinstance(data["value"], list):
                    events = data["value"]
                if not events:
                    owa_items = data.get("Body", {}).get("Items", [])
                    if isinstance(owa_items, list):
                        events.extend(owa_items)
                if events:
                    all_raw.extend(events)
                    log.info(f"Captured {len(events)} event(s) for {cur[0]}")
                    # Backup: capture request template via response.request so the
                    # post-browser direct-API path works even when on_request_range
                    # didn't fire (OWA cache served the response without a network req).
                    if not _captured_req_range:
                        try:
                            _req = response.request
                            if (_req.method == "POST"
                                    and "service.svc" in _req.url.lower()):
                                _body = _req.post_data
                                if _body:
                                    _captured_req_range.update({
                                        "url": (
                                            "https://outlook.office.com/owa/service.svc"
                                            "?action=GetCalendarView&app=Calendar"
                                        ),
                                        "headers": dict(_req.headers),
                                        "body":    _body,
                                    })
                                    log.debug(
                                        "OWA range: request template captured "
                                        "via response.request."
                                    )
                        except Exception:
                            pass
            except Exception:
                pass

        # Use context.route() to capture the GetCalendarView request body before
        # it is sent — more reliable than context.on("request") for cached responses.
        _captured_req_range: dict = {}

        def handle_route_range(route):
            req   = route.request
            url_l = req.url.lower()
            if ("getcalendarview" in url_l and "service.svc" in url_l
                    and not _captured_req_range):
                try:
                    _body    = req.post_data
                    _headers = dict(req.headers)
                    _captured_req_range.update({
                        "url": ("https://outlook.office.com/owa/service.svc"
                                "?action=GetCalendarView&app=Calendar"),
                        "headers": _headers,
                        "body":    _body,
                    })
                    if _body:
                        log.debug("OWA range: request template captured (with body).")
                    else:
                        log.debug(
                            f"OWA range: headers captured ({len(_headers)} keys); "
                            f"body will be constructed for direct API call."
                        )
                except Exception as _re:
                    log.debug(f"OWA range route capture error: {_re}")
            route.continue_()

        context.route("**/service.svc**", handle_route_range)
        context.on("response", on_response)
        page = context.new_page()

        for i, target_date in enumerate(dates):
            cur[0] = target_date
            date_str = target_date.strftime("%Y-%m-%d")
            owa_url  = OWA_CALENDAR_URL.format(date=date_str)
            log.info(f"OWA range: fetching {date_str} ({i+1}/{len(dates)})...")

            try:
                page.goto(owa_url, timeout=60_000, wait_until="domcontentloaded")
            except Exception as nav_err:
                log.warning(f"Navigation ({date_str}): {nav_err}")

            # Longer timeout for first date (cold start), shorter for warm browser
            max_wait = 180 if i == 0 else 60
            deadline = time.time() + max_wait
            while time.time() < deadline:
                if responded[target_date]:
                    log.info(f"Calendar data received for {date_str}.")
                    break
                try:
                    page.wait_for_timeout(1_000)
                except Exception:
                    log.warning(f"Browser closed early for {date_str}.")
                    break
            else:
                log.warning(f"Timed out for {date_str}.")

            # Brief trailing pause
            if responded[target_date]:
                try:
                    page.wait_for_timeout(1_500)
                except Exception:
                    pass

        # Persist OWA session for future direct-API fast path
        if all_raw and _captured_req_range:
            try:
                owa_cookies = [
                    {"name": c["name"], "value": c["value"],
                     "domain": c["domain"], "path": c.get("path", "/")}
                    for c in context.cookies()
                    if any(d in c.get("domain", "")
                           for d in ("outlook.office", "office365.com",
                                     "microsoft.com", "live.com"))
                ]
                save_owa_session({
                    "request_template": _captured_req_range,
                    "cookies":          owa_cookies,
                })
                log.info(f"OWA session cached ({len(owa_cookies)} cookies) "
                         f"— future syncs will use direct API.")
            except Exception as exc:
                log.debug(f"Could not save OWA session: {exc}")

        try:
            context.close()
        except Exception:
            pass
        _close_new_browser_windows(_pre)
        _kill_new_edge_pids(_pre_pids)

    # Post-browser: the browser may only capture current-week events (OWA loads
    # today's week from cache and never fires a new GetCalendarView for past dates).
    # If we captured a fresh OWA session, replay it via direct API — it explicitly
    # sets StartDate/EndDate so it correctly handles any week including past ones.
    if _captured_req_range:
        _owa_fresh = load_owa_session()
        if _owa_fresh:
            _direct_results: dict = {}
            for _d in dates:
                _direct = get_events_via_owa_direct(_d, _owa_fresh)
                if _direct is None:
                    log.debug(f"Direct OWA API unavailable for {_d} — using browser results.")
                    _direct_results = {}
                    break
                _direct_results[_d] = _filter_events(_direct, _d)
                log.info(
                    f"Direct OWA (post-browser) {_d}: "
                    f"{len(_direct_results[_d])} qualifying event(s)."
                )
            if len(_direct_results) == len(dates):
                log.info(f"Post-browser: direct OWA API succeeded for all {len(dates)} date(s).")
                return _direct_results

    return {d: _filter_events(all_raw, d) for d in dates}


def _parse_dt(s: str) -> datetime:
    """Parse an ISO 8601 datetime string (with or without timezone offset)."""
    s = s.rstrip("Z")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in s else "%Y-%m-%dT%H:%M:%S"
        return datetime.strptime(s, fmt)


def _filter_events(raw_events: list, target_date: date) -> list[dict]:
    """Apply inclusion/exclusion rules; handle both Graph (camelCase) and OWA (PascalCase)."""
    qualifying: list = []
    seen_ids: set    = set()   # dedup guard — OWA can fire GetCalendarView multiple times
    _logged_owa_keys  = False
    _logged_start_fmt = False   # one-shot debug for Start field format
    include_tentative = load_config().get("include_tentative", True)

    for ev in raw_events:
        try:
            is_owa  = "Subject" in ev
            subject = (ev.get("Subject") if is_owa else ev.get("subject")) or ""

            if is_owa and not ev.get("IsAllDay") and not _logged_owa_keys:
                _logged_owa_keys = True
                log.debug(f"OWA CalendarItem keys: {sorted(ev.keys())}")

            is_all_day = ev.get("IsAllDay") if is_owa else ev.get("isAllDay")
            if is_all_day:
                continue

            if any(kw in subject.lower() for kw in EXCLUDED_SUBJECTS):
                log.debug(f"Excluded by keyword: {subject}")
                continue

            raw_start = ev.get("Start") or ev.get("start", {})
            if isinstance(raw_start, dict):
                # OWA may use PascalCase {"DateTime": "..."} or Graph camelCase {"dateTime": "..."}
                raw_start = raw_start.get("dateTime") or raw_start.get("DateTime") or ""
            if not isinstance(raw_start, str):
                raw_start = str(raw_start) if raw_start else ""
            if not _logged_start_fmt:
                _logged_start_fmt = True
                log.debug(f"_filter_events[{target_date}]: Start sample → {raw_start[:60]!r}")
            if raw_start[:10] != str(target_date):
                continue

            if is_owa:
                organiser_email = (
                    (ev.get("Organizer") or {})
                    .get("EmailAddress", {})
                    .get("Address", "")
                    .lower()
                )
                _s = ev.get("Start", "")
                start_str = (_s.get("dateTime") or _s.get("DateTime") or "") if isinstance(_s, dict) else (_s or "")
                _e = ev.get("End", "")
                end_str   = (_e.get("dateTime") or _e.get("DateTime") or "") if isinstance(_e, dict) else (_e or "")
                event_id  = str((ev.get("ItemId") or {}).get("Id", subject + start_str))
            else:
                organiser_email = (
                    (ev.get("organizer") or {})
                    .get("emailAddress", {})
                    .get("address", "")
                    .lower()
                )
                start_str = (ev.get("start") or {}).get("dateTime", "")
                end_str   = (ev.get("end")   or {}).get("dateTime", "")
                event_id  = ev.get("id", subject + start_str)


            start_dt       = _parse_dt(start_str)
            end_dt         = _parse_dt(end_str)
            duration_hours = round((end_dt - start_dt).total_seconds() / 3600, 2)

            response_type = (
                (ev.get("ResponseType") or "").lower()
                if is_owa
                else (ev.get("responseStatus") or {}).get("response", "").lower()
            )
            is_organizer = response_type == "organizer" or (ev.get("IsOrganizer") is True)

            # ── Response-status filters ───────────────────────────────────────
            if response_type in ("decline", "declined"):
                log.debug(f"Skipping declined event: {subject[:50]}")
                continue
            if response_type == "tentative" and not include_tentative:
                log.debug(f"Skipping tentative event (include_tentative=false): {subject[:50]}")
                continue

            if event_id in seen_ids:
                log.debug(f"Skipping duplicate event: {subject[:50]} ({event_id[:20]}...)")
                continue
            seen_ids.add(event_id)

            qualifying.append({
                "id":              event_id,
                "subject":         subject,
                "start":           start_str,
                "duration_hours":  duration_hours,
                "organiser_email": organiser_email,
                "organised_by_me": is_organizer,
                "response_type":   response_type,   # "tentative", "accept", "organizer", etc.
            })
        except Exception as exc:
            log.warning(f"Error processing calendar event: {exc}")

    return qualifying


# ══════════════════════════════════════════════════════════════════════════════
# WBS Mapping
# ══════════════════════════════════════════════════════════════════════════════

def find_wbs_mapping(event: dict, config: dict) -> dict | None:
    """Return first keyword/email match, then default_mapping, then None."""
    mapping = _find_specific_mapping(event, config)
    if mapping:
        return mapping
    return config.get("default_mapping") or None


def _find_specific_mapping(event: dict, config: dict) -> dict | None:
    """Match only wbs_mappings rules — does NOT fall back to default_mapping."""
    subject_lower   = event["subject"].lower()
    organiser_lower = event["organiser_email"].lower()
    for mapping in config.get("wbs_mappings", []):
        for kw in mapping.get("keywords", []):
            if kw.lower() in subject_lower:
                return mapping
        for ep in mapping.get("email_patterns", []):
            if ep.lower() in organiser_lower:
                return mapping

    # ── Auto-mappings (user-defined in Settings → Auto-Mappings tab) ──────────
    # Checked after WBS rules, before CATXT favorites.  Minimum 3 chars to
    # avoid accidental matches on trivially short keywords.
    for am in config.get("auto_mappings", []):
        kw = (am.get("keyword") or "").strip().lower()
        if len(kw) >= 3 and kw in subject_lower:
            log.debug(
                f"Auto-mapped via auto-mapping: '{am.get('keyword')}'"
                f" ← '{event['subject'][:50]}'"
            )
            return {
                "label":     am.get("label") or am.get("keyword", ""),
                "tasktype":  am.get("tasktype", ""),
                "zzsubtype": am.get("zzsubtype", ""),
                "rkostl":    am.get("rkostl", ""),
                "rproj":     am.get("rproj", ""),
                "ltxa1":     am.get("ltxa1", ""),
                "wbs":       "",
            }

    # Fall-through: try to match against saved CATXT favorites.
    # If a favorite's description (ltxa1) is a substring of the event subject,
    # use the favorite's tasktype/subtype/cost-center/project as the mapping.
    # Minimum 5 chars to avoid spurious matches on very short descriptions.
    for fav in config.get("_favorites", []):
        ltxa1 = (fav.get("ltxa1") or "").strip().lower()
        if len(ltxa1) >= 5 and ltxa1 in subject_lower:
            log.debug(f"Auto-mapped via favorite: '{fav.get('ltxa1')}' ← '{event['subject'][:50]}'")
            return {
                "label":     fav.get("label") or fav.get("ltxa1", ""),
                "tasktype":  fav.get("tasktype", ""),
                "zzsubtype": fav.get("zzsubtype", ""),
                "rkostl":    fav.get("rkostl", ""),
                "rproj":     fav.get("rproj", ""),
                "wbs":       "",
            }
    return None


def _find_all_wbs_mappings(event: dict, config: dict) -> list[dict]:
    """Return ALL wbs_mappings entries that match this event.

    Unlike _find_specific_mapping this does not short-circuit on the first hit,
    so callers can detect when more than one mapping matches (ambiguity).
    Does NOT check auto_mappings or _favorites — those are always single-pick.
    """
    subject_lower   = event["subject"].lower()
    organiser_lower = event["organiser_email"].lower()
    results: list[dict] = []
    for mapping in config.get("wbs_mappings", []):
        matched = False
        for kw in mapping.get("keywords", []):
            if kw.lower() in subject_lower:
                matched = True
                break
        if not matched:
            for ep in mapping.get("email_patterns", []):
                if ep.lower() in organiser_lower:
                    matched = True
                    break
        if matched:
            results.append(mapping)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Authentication
# ══════════════════════════════════════════════════════════════════════════════

def load_cookies() -> list | None:
    if COOKIES_FILE.exists():
        return json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
    return None


def save_cookies(cookies: list) -> None:
    COOKIES_FILE.write_text(json.dumps(cookies, indent=2), encoding="utf-8")


def authenticate_via_browser(headless_only: bool = False) -> list | None:
    """Open Edge for SSO login, extract session cookies, save and return them.

    headless_only=True: attempt SSO silently using the live Edge profile only.
    Returns None immediately if the Edge profile is locked or if SSO requires
    manual interaction (redirected to login page).  Never opens a visible
    browser window.  Use this for background re-authentication.
    """
    log.info(
        "Opening browser for CATXT authentication (SSO%s)...",
        ", headless-only" if headless_only else "",
    )
    cookies = None

    with sync_playwright() as p:
        edge_profile = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")
        context  = None
        _tmp_dir = None   # temp profile copy used when live profile is locked

        _hidden_hwnds: list = []
        _pre_pids     = _msedge_pids()
        _pre          = _chrome_hwnds()
        _was_headless = False

        # ── Strategy 1: headless with live profile (silent — no visible window) ─
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=edge_profile,
                channel="msedge",
                headless=True,
                timeout=10_000,   # fail fast if profile is locked by a running Edge
                args=["--no-first-run", "--no-default-browser-check",
                      "--disable-sync", "--no-restore-last-session"],
            )
            _was_headless = True
            log.info("Auth browser: msedge (live profile, headless)")
        except Exception as profile_err:
            if headless_only:
                # ── Strategy 1b: headless with a temp copy of the cookie store ──
                # The live Edge profile is locked while Edge is running, so
                # Playwright can't open it directly.  We copy only the minimal
                # files needed for SSO (Local State + Cookies) to a fresh temp
                # directory.  The copy is not locked, so Playwright can launch
                # headlessly from it.  If the SAP SSO session is still valid in
                # those cookies, authentication completes silently with no visible
                # browser window.
                import shutil, tempfile
                _edge_src = Path(os.path.expandvars(
                    r"%LOCALAPPDATA%\Microsoft\Edge\User Data"
                ))
                _tmp_dir = Path(tempfile.mkdtemp(prefix="catxt_edge_"))
                try:
                    (_tmp_dir / "Default").mkdir(parents=True)
                    _ls = _edge_src / "Local State"
                    if _ls.exists():
                        shutil.copy2(_ls, _tmp_dir / "Local State")
                    for _csrc, _crel in [
                        (_edge_src / "Default" / "Cookies",
                         "Default/Cookies"),
                        (_edge_src / "Default" / "Network" / "Cookies",
                         "Default/Network/Cookies"),
                    ]:
                        if _csrc.exists():
                            _cdst = _tmp_dir / _crel
                            _cdst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(_csrc, _cdst)
                    context = p.chromium.launch_persistent_context(
                        user_data_dir=str(_tmp_dir),
                        channel="msedge",
                        headless=True,
                        timeout=15_000,
                        args=["--no-first-run", "--no-default-browser-check",
                              "--disable-sync", "--no-restore-last-session"],
                    )
                    _was_headless = True
                    log.info("Auth browser: msedge (temp profile copy, headless)")
                except Exception as tmp_err:
                    shutil.rmtree(_tmp_dir, ignore_errors=True)
                    _tmp_dir = None
                    log.debug(
                        f"Headless-only auth: live profile locked and temp-profile "
                        f"also failed ({tmp_err}) — giving up."
                    )
                    return None
            else:
                log.info(f"Could not use Edge live profile ({profile_err}) — trying fresh browser.")

        # ── Strategy 2 (fallback): visible fresh browser ───────────────────────
        if context is None:
            browser = None
            for channel in ("msedge", "chrome", None):
                try:
                    kwargs: dict = dict(
                        headless=False,
                        args=["--window-size=1200,800", "--no-first-run"],
                    )
                    if channel:
                        kwargs["channel"] = channel
                    browser = p.chromium.launch(**kwargs)
                    log.info(f"Auth browser launched: {channel or 'bundled Chromium'} (fresh, visible)")
                    break
                except Exception:
                    continue
            if browser is None:
                log.error("Could not launch any browser.")
                return None
            context = browser.new_context()
            threading.Thread(
                target=_suppress_new_browser_window,
                args=(_pre_pids,),
                kwargs={"hidden_out": _hidden_hwnds},
                daemon=True,
            ).start()

        page = context.new_page()
        try:
            page.goto(LOGIN_URL, timeout=60_000)
        except Exception as exc:
            log.warning(f"Auth navigation warning ({exc}); proceeding.")

        # Check whether SSO auto-completed or the user landed on a login page.
        time.sleep(1.0)
        _LOGIN_DOMAINS = (
            "login.microsoftonline.com",
            "login.microsoft.com",
            "login.live.com",
        )
        try:
            on_login_page = any(d in page.url.lower() for d in _LOGIN_DOMAINS)
            if on_login_page:
                if _was_headless:
                    if headless_only:
                        # Caller requested headless-only — don't open a visible
                        # browser.  SSO has expired; user interaction is needed.
                        log.debug(
                            "Headless-only auth: SSO incomplete (landed on login page) "
                            "— giving up."
                        )
                        try:
                            context.close()
                        except Exception:
                            pass
                        if _tmp_dir is not None:
                            import shutil as _sh
                            _sh.rmtree(_tmp_dir, ignore_errors=True)
                        return None
                    # SSO didn't complete silently — close headless context and
                    # relaunch a visible browser so the user can log in manually.
                    log.info(
                        "SSO incomplete in headless mode — relaunching visible browser "
                        "for manual login."
                    )
                    try:
                        context.close()
                    except Exception:
                        pass
                    _was_headless = False
                    _vis_browser = None
                    for channel in ("msedge", "chrome", None):
                        try:
                            kw: dict = dict(headless=False,
                                            args=["--window-size=1200,800", "--no-first-run"])
                            if channel:
                                kw["channel"] = channel
                            _vis_browser = p.chromium.launch(**kw)
                            log.info(f"Auth browser relaunched: {channel or 'bundled Chromium'} (visible)")
                            break
                        except Exception:
                            continue
                    if _vis_browser is None:
                        log.error("Could not launch visible browser for login.")
                        return None
                    context = _vis_browser.new_context()
                    threading.Thread(
                        target=_suppress_new_browser_window,
                        args=(_pre_pids,),
                        kwargs={"hidden_out": _hidden_hwnds},
                        daemon=True,
                    ).start()
                    page = context.new_page()
                    try:
                        page.goto(LOGIN_URL, timeout=60_000)
                    except Exception as exc:
                        log.warning(f"Auth navigation warning ({exc}); proceeding.")
                    time.sleep(1.0)
                    _restore_browser_windows(_hidden_hwnds)
                else:
                    log.info("SSO did not auto-complete — restoring browser for manual login.")
                    _restore_browser_windows(_hidden_hwnds)
        except Exception:
            pass

        try:
            page.wait_for_load_state("networkidle", timeout=120_000)
        except Exception as exc:
            log.warning(f"Auth load-state wait timed out ({exc}); proceeding anyway.")

        raw_cookies = context.cookies()
        cookies = [
            {
                "name":   c["name"],
                "value":  c["value"],
                "domain": c["domain"],
                "path":   c.get("path", "/"),
            }
            for c in raw_cookies
            if "hana.ondemand.com" in c.get("domain", "")
        ]

        # Close via context — works for both persistent and fresh context types
        try:
            context.close()
        except Exception:
            pass
        if _tmp_dir is not None:
            import shutil as _sh
            _sh.rmtree(_tmp_dir, ignore_errors=True)
        time.sleep(0.5)   # allow Edge time to process post-close window teardown
        _close_new_browser_windows(_pre)
        time.sleep(0.3)
        _kill_new_edge_pids(_pre_pids)

    if cookies:
        save_cookies(cookies)
        log.info(f"Authenticated — {len(cookies)} cookies saved.")
    else:
        log.error("No cookies extracted. Authentication failed.")
    return cookies or None


def build_session(cookies: list) -> requests.Session:
    session = requests.Session()
    for c in cookies:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
    session.headers.update({
        "X-Requested-With":      "XMLHttpRequest",
        "Accept":                "application/json",
        "Accept-Language":       "en",
        "DataServiceVersion":    "2.0",
        "MaxDataServiceVersion": "2.0",
    })
    return session


def test_session(session: requests.Session) -> bool:
    try:
        r = session.get(f"{BASE_URL}/Userinfo", timeout=15)
        return r.status_code == 200
    except Exception:
        return False


def get_csrf_token(session: requests.Session) -> str | None:
    try:
        r = session.head(
            f"{BASE_URL}/",
            headers={"x-csrf-token": "Fetch"},
            timeout=20,
        )
        token = r.headers.get("x-csrf-token", "")
        if token and token.lower() != "required":
            return token
    except Exception as exc:
        log.error(f"CSRF fetch error: {exc}")
    log.error("Failed to obtain CSRF token.")
    return None


def get_or_refresh_session(
    cookies_only: bool = False,
    headless_only: bool = False,
) -> requests.Session | None:
    """
    Return an authenticated requests.Session, re-authenticating if needed.
    Returns None if authentication fails.

    cookies_only=True: return None instead of opening a browser when no
    valid session exists.  Use this for background/silent operations that
    should not pop up a browser window.

    headless_only=True: attempt a silent SSO re-auth using the live Edge
    profile (headless).  Returns None if the profile is locked or if SSO
    requires manual interaction.  Never opens a visible browser window.
    Implies cookies_only=False — a headless browser attempt IS made.
    """
    cookies = load_cookies()
    session = None
    if cookies:
        session = build_session(cookies)
        if not test_session(session):
            log.info("Saved session expired — re-authenticating.")
            session = None
    if session is None:
        if cookies_only:
            log.debug("get_or_refresh_session(cookies_only): no valid session — skipping.")
            return None
        cookies = authenticate_via_browser(headless_only=headless_only)
        if not cookies:
            return None
        session = build_session(cookies)
    return session


# ══════════════════════════════════════════════════════════════════════════════
# CATXT API
# ══════════════════════════════════════════════════════════════════════════════

def get_existing_entries(
    session: requests.Session, target_date: date, csrf_token: str = ""
) -> list[dict]:
    """Fetch existing Activity entries for target_date (queries full week, filters to date)."""
    week_start = (
        target_date - timedelta(days=target_date.weekday())
    ).strftime("%Y%m%d")
    week_end = (
        target_date + timedelta(days=6 - target_date.weekday())
    ).strftime("%Y%m%d")
    date_str = target_date.strftime("%Y%m%d")

    try:
        r = session.get(
            f"{BASE_URL}/Param(Pernr='{PERNR}',"
            f"Datefrom='{week_start}',Dateto='{week_end}')/Activity",
            headers={
                "Accept":                "application/json",
                "Accept-Language":       "en",
                "DataServiceVersion":    "2.0",
                "MaxDataServiceVersion": "2.0",
                "sap-cancel-on-close":   "true",
                "sap-contextid-accept":  "header",
                "x-requested-with":      "XMLHttpRequest",
            },
            timeout=30,
        )
        if r.status_code != 200:
            log.warning(
                f"Could not fetch existing entries (HTTP {r.status_code}): {r.text[:500]}"
            )
            return []
        all_results = r.json().get("d", {}).get("results", [])
        return [e for e in all_results if e.get("Workdate", "") == date_str]
    except Exception as exc:
        log.warning(f"Error fetching existing entries: {exc}")
    return []


def get_existing_hours_per_day(
    session: requests.Session, dates: list,
) -> dict:
    """
    Fetch already-posted CATXT hours for a list of dates in one API call.
    Returns dict keyed by 'YYYYMMDD' string → total posted hours (float).
    Errors are logged and an empty dict returned so callers degrade gracefully.
    """
    if not dates:
        return {}
    min_d   = min(dates)
    max_d   = max(dates)
    w_start = (min_d - timedelta(days=min_d.weekday())).strftime("%Y%m%d")
    w_end   = (max_d + timedelta(days=6 - max_d.weekday())).strftime("%Y%m%d")
    try:
        r = session.get(
            f"{BASE_URL}/Param(Pernr='{PERNR}',"
            f"Datefrom='{w_start}',Dateto='{w_end}')/Activity",
            headers=_odata_headers(),
            timeout=30,
        )
        if r.status_code != 200:
            log.warning(f"get_existing_hours_per_day: HTTP {r.status_code}")
            return {}
        totals: dict = {}
        for entry in r.json().get("d", {}).get("results", []):
            wd = entry.get("Workdate", "")
            if not wd or entry.get("Unit") != "H":
                continue
            qty = float(entry.get("Catsquantity", "0") or "0")
            totals[wd] = totals.get(wd, 0.0) + qty
        return totals
    except Exception as exc:
        log.warning(f"get_existing_hours_per_day error: {exc}")
        return {}


def _odata_headers() -> dict:
    return {
        "Accept":                "application/json",
        "Accept-Language":       "en",
        "DataServiceVersion":    "2.0",
        "MaxDataServiceVersion": "2.0",
        "sap-cancel-on-close":   "true",
        "sap-contextid-accept":  "header",
        "x-requested-with":      "XMLHttpRequest",
    }


def fetch_task_types(session: requests.Session) -> list[dict]:
    """Fetch available task types from CATXT API. Returns raw result list."""
    try:
        r = session.get(
            f"{BASE_URL}/Tasktype",
            headers=_odata_headers(), timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("d", {}).get("results", [])
    except Exception as exc:
        log.debug(f"fetch_task_types: {exc}")
    return []


def fetch_subtypes(
    session: requests.Session, objnr: str, tasktype: str,
) -> list[dict]:
    """Fetch sub-types for a given tasktype / object number via direct GET."""
    try:
        r = session.get(
            f"{BASE_URL}/Object(Objnr='{objnr}',Tasktype='{tasktype}')/Subtype",
            headers=_odata_headers(),
            timeout=20,
        )
        if r.status_code == 200:
            results = r.json().get("d", {}).get("results", [])
            log.debug(f"fetch_subtypes({tasktype}): {len(results)} result(s)")
            return results
        log.debug(f"fetch_subtypes({tasktype}) HTTP {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        log.debug(f"fetch_subtypes({tasktype}): {exc}")
    return []


def fetch_favorites(session: requests.Session, csrf_token: str = "") -> list[dict]:
    """Fetch the user's saved CATXT favorites."""
    try:
        hdrs = {**_odata_headers()}
        if csrf_token:
            hdrs["x-csrf-token"] = csrf_token
        r = session.get(
            f"{BASE_URL}/Favorite",
            headers=hdrs, timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("d", {}).get("results", [])
    except Exception as exc:
        log.debug(f"fetch_favorites: {exc}")
    return []


_METADATA_TTL_HOURS = 6   # re-fetch at most every 6 hours


def sync_catxt_metadata(session: requests.Session, config: dict,
                        csrf_token: str = "", force: bool = False) -> bool:
    """
    Fetch task types, sub-types, and favorites from the CATXT API and cache
    them in config["_task_types"] / config["_favorites"].
    Skipped automatically if the cache is less than 6 hours old (pass
    force=True to bypass, e.g. when triggered from the Sync Metadata menu item).
    Returns True if config was updated.
    """
    if not force:
        last = config.get("_metadata_synced_at")
        if last:
            try:
                age_h = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 3600
                if age_h < _METADATA_TTL_HOURS:
                    log.debug(f"CATXT metadata: cache is {age_h:.1f}h old — skipping API calls.")
                    return False
            except Exception:
                pass

    default = config.get("default_mapping", {})
    # Prefer the Userinfo-detected values; fall back to config then module fallback
    rkostl  = default.get("rkostl", "") or config.get("_kostl", KOSTL)
    objnr   = config.get("_objnr", "") or f"KS0001{rkostl}"   # CC object for sub-type lookup

    # ── Task types ────────────────────────────────────────────────────────────
    # The unfiltered /Tasktype endpoint returns every task type in the system.
    # Intersect with KNOWN_TASK_TYPES so the app only shows types the user
    # actually sees in their CATXT UI (same 10 confirmed from the UI screenshot).
    raw_tt = fetch_task_types(session)
    task_types: dict = {}
    for tt in raw_tt:
        code  = tt.get("Tasktype", "")
        label = (tt.get("Tasktypetext") or tt.get("Description") or code).strip()
        if code and code in KNOWN_TASK_TYPES:          # ← filter to known list
            task_types[code] = {"label": label or KNOWN_TASK_TYPES[code]["label"],
                                 "subtypes": []}
    # Fill in any known types the API didn't return (or if API failed entirely)
    for code, info in KNOWN_TASK_TYPES.items():
        if code not in task_types:
            task_types[code] = {"label": info["label"], "subtypes": []}

    # ── CSRF (needed for fetch_favorites POST-protected endpoint) ─────────────
    if not csrf_token:
        csrf_token = get_csrf_token(session) or ""


    # ── Sub-types (one API call per task type that has them) ──────────────────
    NO_SUBTYPE_TYPES = {"BREA", "ICOS", "TOLO"}
    for code in list(task_types.keys()):
        if code in NO_SUBTYPE_TYPES:
            continue
        raw_st = fetch_subtypes(session, objnr, code)
        task_types[code]["subtypes"] = [
            {"code": s.get("Stype", ""), "label": s.get("Stypetext", "").strip()}
            for s in raw_st
            if s.get("Stype")
        ]

    # ── Favorites ─────────────────────────────────────────────────────────────
    raw_fav = fetch_favorites(session, csrf_token=csrf_token)
    favorites = [
        {
            "label":     (f.get("Ltxa1") or f.get("Tasktype", "")).strip(),
            "tasktype":  f.get("Tasktype", ""),
            "zzsubtype": f.get("Zzsubtype", ""),
            "ltxa1":     f.get("Ltxa1", "").strip(),
            "rkostl":    f.get("Rkostl", ""),
            "rproj":     f.get("Rproj", ""),
        }
        for f in raw_fav
    ]

    changed = (
        config.get("_task_types") != task_types
        or config.get("_favorites") != favorites
    )
    if changed:
        config["_task_types"] = task_types
        config["_favorites"]  = favorites
        log.info(
            f"CATXT metadata: {len(task_types)} task types, "
            f"{sum(len(v['subtypes']) for v in task_types.values())} subtypes, "
            f"{len(favorites)} favorites"
        )
    # Load-modify-save: reload fresh from disk so we never overwrite keywords
    # or other user edits made in the Settings UI while the sync was running.
    _meta_fresh = load_config()
    if changed:
        _meta_fresh["_task_types"] = task_types
        _meta_fresh["_favorites"]  = favorites
    _meta_fresh["_metadata_synced_at"] = datetime.now().isoformat()
    save_config(_meta_fresh)
    return changed


def post_activity(
    session: requests.Session,
    csrf_token: str,
    event: dict,
    mapping: dict,
    target_date: date,
    existing_taskcounters: set = None,
) -> bool:
    """POST a time entry to CATXT via ActivityHeader deep-insert batch."""
    workdate   = target_date.strftime("%Y%m%d")
    # Allow a per-row description override (set by review dialog / favorites)
    short_text = (mapping.get("_ltxa1_override") or event["subject"])[:40]

    rproj  = mapping.get("rproj", "")
    # CATXT stores "no project assigned" as 24 zeros — treat as empty so we
    # fall through to cost-centre mode instead of posting to an invalid WBS.
    if rproj and rproj.strip("0") == "":
        rproj = ""
    rkostl = mapping.get("rkostl", "")
    rkdauf = mapping.get("rkdauf", "")
    rkdpos = mapping.get("rkdpos", "") if rkdauf else ""

    # Sales-Document mode: Rkdauf present but no WBS/Rproj (e.g. Clorox, Waters)
    is_sd = bool(rkdauf and not rproj)

    tasktype = mapping.get("tasktype", "")
    if rproj:
        # Project entries must always use CFPP regardless of what the review
        # dialog dropdown shows — override here as the authoritative safety net.
        tasktype = "CFPP"
    elif is_sd:
        # SD entries are customer-billable time — CFPP is correct.
        # Honour any explicit mapping override, but default to CFPP.
        if not tasktype:
            tasktype = "CFPP"
    elif not tasktype:
        tasktype = "MEET"

    taskcomponent = mapping.get("taskcomponent", "")
    if not taskcomponent:
        if rproj or is_sd:
            # WBS and SD project entries use WORKHRS — confirmed correct from
            # production HAR captures and live posting results.
            taskcomponent = "WORKHRS"
        elif tasktype == "ICON":
            # ICON ("Internal projectwork") specifically requires WORKHRS and
            # silently rejects WORKSTAT (ZCATSXT-225).
            taskcomponent = "WORKHRS"
        else:
            # CC (cost centre) entries use WORKSTAT — confirmed from Fiori UI
            # HAR capture (Sep 18 2026).  Using WORKHRS for CC causes the backend
            # to silently drop the entry (returns [009] with empty Activity).
            # The original code incorrectly overrode this to WORKHRS universally.
            taskcomponent = "WORKSTAT"

    # WBS and SD entries need the user's home cost centre as the sender object.
    skostl = KOSTL if (rproj or is_sd) else ""
    # HAR confirmed: CC entries send Rproj="" not 24-zeros
    rproj_val = rproj

    if rproj:
        obart = "PR"
        objnr = "PR" + rproj[-8:]
    elif is_sd:
        # Sales Document receiving object: Obart="VB" (Vertriebsbeleg),
        # Objnr = "VB" + 10-digit order (rkdauf) + 6-digit item (rkdpos).
        # HAR capture (Sep 18 2026) confirmed "VB" — the previous "SD" value
        # caused silent drops on all Sales Order entries.
        obart = "VB"
        objnr = "VB" + rkdauf + rkdpos
    else:
        obart = "KS"
        objnr = "KS0001" + rkostl

    wbs = mapping.get("wbs", "")
    if rproj and wbs:
        zcpr_objgextid = wbs
        zcpr_extid     = wbs.rsplit(".", 1)[0] if "." in wbs else wbs
        zcpr_objtype   = "TTO"
    else:
        # SD and CC entries don't carry CPS project reference fields.
        zcpr_objgextid = zcpr_extid = zcpr_objtype = ""

    # Sub-type (Zzsubtype) — optional, "" is valid
    zzsubtype = mapping.get("zzsubtype", "")

    activity_line = {
        "Taskcounter":    "0",
        "Counter":        "0",
        "Tmp_key":        "",
        "Pernr":          PERNR,
        "Workdate":       workdate,
        "Tasktype":       tasktype,
        "Taskcomponent":  taskcomponent,
        "Catsquantity":   f"{event['duration_hours']:.3f}",
        "Unit":           "H",
        "Status":         "",           # HAR: "" not "10"
        "Ltxa1":          short_text,
        "Skostl":         skostl,
        "Rproj":          rproj_val,    # HAR: "" for CC, not 24 zeros
        "Rkostl":         rkostl,
        "Rkdauf":         rkdauf,
        "Rkdpos":         rkdpos,       # HAR: "" when no sales order
        "Raufnr":         "",
        "Rnplnr":         "",
        "Raufpl":         "",           # HAR: "" not "0000000000"
        "Raplzl":         "",           # HAR: "" not "00000000"
        "Vornr":          "",
        "Lstnr":          "",
        "Pflag":          "I",          # HAR: "I" (Insert) not ""
        "Waers":          "",
        "Catsamount":     "0.00",
        "Tasklevel":      (
            # WBS / SD entries: use mapping's tasklevel, default "G3".
            mapping.get("tasklevel", "G3") if (rproj or is_sd)
            # CC entries: ICON requires "K1" (ZCATSXT-225); other types accept "".
            # "NONE" was the old fallback but is rejected by the backend — never send it.
            else (mapping.get("tasklevel") or ("K1" if tasktype == "ICON" else ""))
        ),
        "Zz_location":    mapping.get("zz_location", "R") if (rproj or is_sd) else "",
        "Zzsubtype":      zzsubtype,    # HAR: new field, sub-type code e.g. "TEAMMEET"
        "Obart":          obart,
        "Objnr":          objnr,
        "Zcpr_extid":     zcpr_extid,
        "Zcpr_objgextid": zcpr_objgextid,
        "Zcpr_objtype":   zcpr_objtype,
        "Newo2c":         "X",          # HAR: new field
        "LongtextLine":   [],           # HAR: new field
        "Essposting":     "",
        "Trv_rkdauf":     "",
        "Trv_rkdpos":     "",
        "Zz_rel_obj":     "",
        "Trv_Rproj":      "",
        "Zzbyd":          "",
        "Zzcontpers":     mapping.get("_zzcontpers", ""),
        "Zz_projn":       "",
        "Zzpsptxt":       "",
        "Zcpr_guid":      "",
        "Zcpr_objguid":   "",
        "Msgno":          "",
        "Msgtxt":         "",
    }

    header_payload = {
        "Taskcounter": "",
        "Testrun":     "",
        "Version":     "",
        "Msgno":       "UI5",           # HAR: present in header
        "Msgtxt":      "",
        "Activity":    [activity_line],
    }
    payload_json = json.dumps(header_payload)
    log.info(
        f"  →  Posting: {event['subject'][:50]} | "
        f"tasktype={tasktype} zzsubtype={zzsubtype} | "
        f"{'WBS ' + rproj[-8:] if rproj else ('SO ' + rkdauf.lstrip('0') + '/' + rkdpos.lstrip('0') if is_sd else 'CC ' + rkostl)} | "
        f"{event['duration_hours']}h"
    )
    cs  = "changeset_catxt"
    bat = "batch_catxtwrite"

    batch_body = (
        f"--{bat}\r\n"
        f"Content-Type: multipart/mixed; boundary={cs}\r\n\r\n"
        f"--{cs}\r\n"
        "Content-Type: application/http\r\n"
        "Content-Transfer-Encoding: binary\r\n"
        "Content-ID: 1\r\n\r\n"
        "POST ActivityHeader HTTP/1.1\r\n"
        "Content-Type: application/json\r\n"
        "Accept: application/json\r\n"
        "sap-cancel-on-close: true\r\n"
        "sap-contextid-accept: header\r\n"
        "DataServiceVersion: 2.0\r\n"
        "MaxDataServiceVersion: 2.0\r\n"
        f"Content-Length: {len(payload_json.encode())}\r\n\r\n"
        f"{payload_json}\r\n"
        f"--{cs}--\r\n"
        f"--{bat}--\r\n"
    )

    try:
        r = session.post(
            f"{BASE_URL}/$batch",
            data=batch_body,
            headers={
                "Content-Type": f"multipart/mixed;boundary={bat}",
                "x-csrf-token": csrf_token,
            },
            timeout=30,
        )
        if r.status_code in (200, 202):
            resp_text = r.text
            if "HTTP/1.1 201" in resp_text or "HTTP/1.1 200" in resp_text:
                inner_status = "201" if "HTTP/1.1 201" in resp_text else "200"
                log.debug(f"      Response (inner {inner_status}): {resp_text[:3000]}")
                try:
                    json_start = resp_text.find('{"d"')
                    if json_start >= 0:
                        hdr    = json.loads(resp_text[json_start : resp_text.rfind("}") + 1])
                        d      = hdr.get("d", {})
                        msgtxt = d.get("Msgtxt", "")
                        msgno  = d.get("Msgno", "")
                        if msgtxt:
                            log.debug(f"      Msgtxt: [{msgno}] {msgtxt}")
                        activity_results = d.get("Activity", {}).get("results", None)
                        if activity_results is not None and len(activity_results) == 0:
                            log.error(
                                f"  ✗  {event['subject'][:50]} — silently dropped "
                                f"(backend accepted batch but created no entry)"
                            )
                            if msgtxt:
                                log.error(f"      Backend said: [{msgno}] {msgtxt}")
                            log.error(f"      ActivityLine: {json.dumps(activity_line)}")
                            return False
                        # Guard against false-positive: SAP sometimes returns an
                        # existing entry in Activity.results even when the new entry
                        # was not created (seen with OPEN tasktype). If the Taskcounter
                        # in the response belongs to a pre-existing entry, the post
                        # silently failed.
                        if activity_results and existing_taskcounters:
                            resp_tc = activity_results[0].get("Taskcounter", "")
                            if resp_tc and resp_tc in existing_taskcounters:
                                # Only flag as a true duplicate if the returned entry's
                                # content matches what we just sent.  The CATXT backend
                                # sometimes echoes a pre-existing Taskcounter in the
                                # response for an unrelated new post — this happens when
                                # multiple entries are posted in the same SAP session and
                                # the backend reuses the active ActivityHeader.
                                resp_ltxa1 = (activity_results[0].get("Ltxa1") or "")[:40]
                                resp_date  = activity_results[0].get("Workdate", "")
                                resp_qty   = activity_results[0].get("Catsquantity", "")
                                is_true_dup = (
                                    resp_ltxa1 == activity_line.get("Ltxa1", "")
                                    and resp_date == workdate
                                    and resp_qty  == activity_line.get("Catsquantity", "")
                                )
                                if is_true_dup:
                                    log.error(
                                        f"  ✗  {event['subject'][:50]} — response returned "
                                        f"existing entry (Taskcounter={resp_tc}); "
                                        f"new entry was NOT created."
                                    )
                                    if msgtxt:
                                        log.error(f"      Backend said: [{msgno}] {msgtxt}")
                                    log.error(f"      ActivityLine: {json.dumps(activity_line)}")
                                    return False
                                else:
                                    # Content differs → the backend is echoing a
                                    # pre-existing entry in results[0] but the new
                                    # entry WAS still created (confirmed in production
                                    # on 2026-09-21: TechEd + NDL Team Meeting appeared
                                    # in CATXT even though results[0] was TC=0313955242).
                                    # Treat as success; log a warning for visibility.
                                    log.warning(
                                        f"  ⚠  {event['subject'][:50]} — backend echoed "
                                        f"pre-existing Taskcounter={resp_tc} but content "
                                        f"differs (resp='{resp_ltxa1}'/{resp_date}, "
                                        f"sent='{activity_line.get('Ltxa1','')[:40]}'/{workdate})"
                                        f" — treating as successful post (entry is created)."
                                    )
                                    if msgtxt:
                                        log.warning(f"      Backend said: [{msgno}] {msgtxt}")
                                    return True

                except Exception:
                    pass

                # Catch silent validation rejections: SAP returns Zzmoberrflag="V"
                # on the echoed-back entry when it refuses to create the new one
                # (e.g. invalid tasktype/subtype for the target cost centre or project).
                # This fires even when existing_taskcounters is None — no pre-fetch needed.
                try:
                    if activity_results:
                        mob_flag   = activity_results[0].get("Zzmoberrflag", "")
                        resp_tc    = activity_results[0].get("Taskcounter", "")
                        resp_ltxa1 = activity_results[0].get("Ltxa1", "")[:40]
                        resp_type  = activity_results[0].get("Tasktype", "")
                        if mob_flag == "V":
                            log.error(
                                f"  ✗  {event['subject'][:50]} — silently rejected "
                                f"(Zzmoberrflag=V); backend returned existing entry "
                                f"(Taskcounter={resp_tc}, '{resp_ltxa1}', {resp_type}). "
                                "Likely invalid tasktype/subtype for this CC or project."
                            )
                            if msgtxt:
                                log.error(f"      Backend said: [{msgno}] {msgtxt}")
                            log.error(f"      ActivityLine: {json.dumps(activity_line)}")
                            return False
                except Exception:
                    pass

                log.info(
                    f"  ✓  {event['subject'][:50]}  "
                    f"({event['duration_hours']}h)  →  {mapping['label']}"
                )
                return True
            else:
                log.error(f"  ✗  Failed: {event['subject'][:50]}")
                log.error(f"      Batch response: {resp_text[:1000]}")
                return False
        elif r.status_code == 403:
            # CSRF token likely expired — fetch a fresh one and retry once
            log.warning(f"  ↻  HTTP 403 on batch POST — refreshing CSRF token and retrying...")
            new_csrf = get_csrf_token(session)
            if new_csrf and new_csrf != csrf_token:
                r2 = session.post(
                    f"{BASE_URL}/$batch",
                    data=batch_body,
                    headers={
                        "Content-Type": f"multipart/mixed;boundary={bat}",
                        "x-csrf-token": new_csrf,
                    },
                    timeout=30,
                )
                if r2.status_code in (200, 202) and ("HTTP/1.1 201" in r2.text or "HTTP/1.1 200" in r2.text):
                    log.info(
                        f"  ✓  {event['subject'][:50]}  "
                        f"({event['duration_hours']}h)  →  {mapping['label']}  [after CSRF refresh]"
                    )
                    return True
            log.error(f"  ✗  Failed: {event['subject'][:50]} | HTTP 403 (CSRF retry failed)")
            return False
        else:
            log.error(f"  ✗  Failed: {event['subject'][:50]} | HTTP {r.status_code}")
            log.error(f"      Response: {r.text[:800]}")
            return False
    except Exception as exc:
        log.error(f"  ✗  Exception posting entry: {exc}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Day orchestration  (split for GUI review step between them)
# ══════════════════════════════════════════════════════════════════════════════

def prepare_day(
    target_date: date, config: dict, processed: dict,
    prefetched_events: list | None = None,
) -> tuple[list, list]:
    """
    Separate calendar events for target_date by mapping status.

    prefetched_events — pass the list from get_events_for_date_range() to avoid
                        opening a new browser session (used by range sync).
                        When None the function fetches events itself via OWA.

    Returns (mapped, unmapped) — see get_events_for_date_range() for details.
    Already-submitted event IDs (from the processed cache) are excluded.
    """
    if prefetched_events is not None:
        events = prefetched_events
        log.info(f"Using {len(events)} pre-fetched event(s) for {target_date}.")
    else:
        events = get_events_for_date(target_date)
        log.info(f"Found {len(events)} qualifying event(s) for {target_date}.")

    if not events:
        return [], []

    date_key   = str(target_date)
    done_ids   = set(processed.get(date_key, []))
    new_events = [e for e in events if e["id"] not in done_ids]
    log.info(f"{len(new_events)} event(s) not yet submitted.")

    mapped: list   = []
    unmapped: list = []
    for event in new_events:
        specific = _find_specific_mapping(event, config)
        if specific:
            mapped.append((event, specific))
        else:
            unmapped.append(event)

    log.info(f"  {len(mapped)} mapped,  {len(unmapped)} unmapped.")
    return mapped, unmapped


def post_day(
    session: requests.Session,
    target_date: date,
    to_submit: list,
    processed: dict,
) -> tuple[int, int, int, list]:
    """
    Post a confirmed list of (event, mapping) pairs to CATXT for target_date.
    Fetches a fresh CSRF token and checks for existing entries before posting.

    Returns (ok, skipped, failed, failed_entries) — failed_entries is a list of
    human-readable strings naming each entry that failed to post.
    """
    if not to_submit:
        return 0, 0, 0, []

    csrf_token = get_csrf_token(session)
    if not csrf_token:
        log.error("Could not obtain CSRF token.")
        return 0, 0, len(to_submit), [f"{ev['subject'][:35]} ({target_date.strftime('%a %d %b')})" for ev, _ in to_submit]

    existing    = get_existing_entries(session, target_date, csrf_token=csrf_token)
    # Strip whitespace from stored descriptions — CATXT trims trailing spaces on save
    existing_lc = {e.get("Ltxa1", "").strip().lower() for e in existing}
    existing_tc = {e.get("Taskcounter") for e in existing if e.get("Taskcounter")}
    log.info(f"Found {len(existing)} existing CATXT entr(ies) for {target_date}.")

    date_key      = str(target_date)
    processed_ids = processed.setdefault(date_key, [])
    ok = skipped  = 0
    failed_entries: list = []
    posted_entries: list = []   # (event, mapping) pairs actually submitted this run

    for event, mapping in to_submit:
        # Use the actual text that would be sent (respects any description override)
        posted_text = (mapping.get("_ltxa1_override") or event["subject"])[:40].strip().lower()
        if posted_text in existing_lc:
            log.info(f"  ↩  Already in CATXT: {event['subject'][:50]}")
            processed_ids.append(event["id"])
            skipped += 1
            continue
        if post_activity(session, csrf_token, event, mapping, target_date, existing_tc):
            processed_ids.append(event["id"])
            ok += 1
            posted_entries.append((event, mapping))
        else:
            failed_entries.append(
                f"{event['subject'][:35]} ({target_date.strftime('%a %d %b')})"
            )

    # Post-day verification: re-fetch CATXT and check that each posted entry's
    # description actually landed. CATXT can silently drop entries (returns 201 but
    # creates no record). If something is missing, remove it from processed_ids so
    # it will be retried on the next sync.
    if posted_entries:
        try:
            verify_existing = get_existing_entries(session, target_date, csrf_token=csrf_token)
            verify_lc = {e.get("Ltxa1", "").strip().lower() for e in verify_existing}
            for ev, mp in posted_entries:
                ptext = (mp.get("_ltxa1_override") or ev["subject"])[:40].strip().lower()
                if ptext not in verify_lc:
                    log.error(
                        f"  ✗  {ev['subject'][:50]} — NOT found in CATXT after posting "
                        f"(silently dropped). Will be retried next sync."
                    )
                    try:
                        processed_ids.remove(ev["id"])
                    except ValueError:
                        pass
                    ok -= 1
                    failed_entries.append(
                        f"{ev['subject'][:35]} ({target_date.strftime('%a %d %b')}) [verification failed]"
                    )
        except Exception as _ve:
            log.debug(f"Post-day verification query failed: {_ve}")

    failed = len(to_submit) - ok - skipped
    log.info(
        f"{'=' * 60}\n"
        f"Done — {ok} posted, {skipped} already existed, {failed} failed."
    )
    return ok, skipped, failed, failed_entries


# ══════════════════════════════════════════════════════════════════════════════
# Staffing sync
# ══════════════════════════════════════════════════════════════════════════════

_STAFFING_TTL_HOURS = 1   # re-fetch at most every hour


def _starter_keywords(customer_name: str) -> list[str]:
    """Generate initial keyword suggestions from a staffing customer name.

    Rules:
    - Always includes the full name (lowercased) so the most specific match works.
    - Also includes the first word if it is >= 5 characters and the name has
      more than one word — gives a useful short-form (e.g. 'keurig' from
      'Keurig Dr Pepper', 'lincoln' from 'Lincoln Electric').
    - Single-word names (e.g. 'Clorox', 'Solventum') are returned as-is.
    - Generic short first words (SAP, The, etc.) are skipped by the >= 5 rule.

    Only applied to brand-new projects; never overwrites existing keywords.
    """
    name = customer_name.strip().lower()
    if not name:
        return []
    keywords: list[str] = [name]
    words = name.split()
    if len(words) > 1 and len(words[0]) >= 5:
        keywords.append(words[0])
    # Deduplicate preserving order
    seen: set[str] = set()
    result: list[str] = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            result.append(kw)
    return result


def sync_staffing(
    session: requests.Session, config: dict, csrf_token: str = "",
    force: bool = False,
) -> int:
    """
    Query CATXT Staffing endpoint, add new projects to config.json.
    Skipped automatically if the cache is less than 1 hour old (pass
    force=True to bypass, e.g. when triggered from the Sync Staffing menu item).
    Also backfills Tasklevel on existing entries where it was blank.
    Returns count of new projects added.
    """
    if not force:
        last = config.get("_staffing_synced_at")
        if last:
            try:
                age_h = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 3600
                if age_h < _STAFFING_TTL_HOURS:
                    log.debug(f"Staffing sync: cache is {age_h:.1f}h old — skipping API call.")
                    return 0
            except Exception:
                pass

    today     = date.today()
    date_from = today.replace(day=1).strftime("%Y%m%d")
    date_to   = (today + timedelta(days=180)).strftime("%Y%m%d")
    log.info("Syncing staffing assignments from CATXT...")

    try:
        r = session.get(
            f"{BASE_URL}/Param(Pernr='',Datefrom='{date_from}',Dateto='{date_to}')/Staffing",
            headers={
                "Accept":                "application/json",
                "Accept-Language":       "en",
                "DataServiceVersion":    "2.0",
                "MaxDataServiceVersion": "2.0",
                "sap-cancel-on-close":   "true",
                "sap-contextid-accept":  "header",
                "x-requested-with":      "XMLHttpRequest",
            },
            timeout=30,
        )
        if r.status_code != 200:
            log.warning(
                f"Staffing sync: HTTP {r.status_code} — skipping. {r.text[:500]}"
            )
            return 0
        all_entries = r.json().get("d", {}).get("results", [])
        if not all_entries:
            log.warning("Staffing sync: empty response — skipping.")
            return 0
    except Exception as exc:
        log.warning(f"Staffing sync error: {exc} — skipping.")
        return 0

    # De-duplicate: each project appears once per day in the response
    seen: set  = set()
    unique: list = []
    for entry in all_entries:
        objnr = entry.get("Objnr", "")
        if objnr and objnr not in seen:
            seen.add(objnr)
            unique.append(entry)
    log.info(f"Staffing sync: {len(unique)} active project(s) found.")

    existing_rprojs = {m.get("rproj", "") for m in config.get("wbs_mappings", [])}
    config_changed  = False
    added           = 0

    for entry in unique:
        rproj     = entry.get("Rproj", "")
        wbs       = entry.get("Objnr_f", "")
        info      = entry.get("Objnr_info", "")
        customer  = entry.get("Sold2Party", "")
        endda     = entry.get("Endda", "")
        # Tasklevel field name in Staffing response — try both common field names
        tasklevel = (
            entry.get("Tasklevel")
            or entry.get("Jgrup")
            or entry.get("Grade")
            or ""
        )

        # Skip non-WBS entries (24 zeros = cost-centre / sales-order)
        if not rproj or rproj == "0" * 24:
            continue

        # Backfill tasklevel on existing entries that were added without it
        for existing_m in config.get("wbs_mappings", []):
            if existing_m.get("rproj") == rproj and not existing_m.get("tasklevel") and tasklevel:
                existing_m["tasklevel"] = tasklevel
                log.info(f"  ↺  Backfilled tasklevel '{tasklevel}' for {existing_m.get('label', rproj)}")
                config_changed = True

        if rproj in existing_rprojs:
            continue  # already tracked

        parts = [p for p in [customer, info] if p]
        label = " — ".join(parts) if parts else entry.get("Objnr", rproj)

        starter_kws = _starter_keywords(customer)
        config.setdefault("wbs_mappings", []).append({
            "label":          label,
            "wbs":            wbs,
            "rproj":          rproj,
            "keywords":       starter_kws,
            "email_patterns": [],
            "tasktype":       "",
            "tasklevel":      tasklevel,
            "_endda":         endda,
            "_note": (
                f"Auto-added by staffing sync — starter keywords from customer name: "
                f"{starter_kws}. Add more keywords/email_patterns via Joule or Settings."
                if starter_kws else
                "Auto-added by staffing sync — add keywords/email_patterns "
                "to enable auto-mapping."
            ),
        })
        existing_rprojs.add(rproj)
        added += 1
        config_changed = True
        kw_str = f", starter keywords: {starter_kws}" if starter_kws else " (no customer name — add keywords manually)"
        log.info(f"  + New project: {label} ({wbs or rproj}){kw_str}")

    if config_changed:
        if added:
            log.info(f"Staffing sync: {added} new project(s) added to config.json.")
        else:
            log.info("Staffing sync: tasklevel(s) backfilled.")
    else:
        log.info("Staffing sync: config is already up to date.")

    # Load-modify-save: reload fresh from disk so we never overwrite keywords
    # or other user edits made in the Settings UI while the sync was running.
    _staff_fresh = load_config()
    # 1. Propagate tasklevel backfills onto the fresh config
    _fresh_by_rproj = {m.get("rproj"): m for m in _staff_fresh.get("wbs_mappings", [])}
    for _m in config.get("wbs_mappings", []):
        _rp = _m.get("rproj")
        if _rp and _rp in _fresh_by_rproj:
            if _m.get("tasklevel") and not _fresh_by_rproj[_rp].get("tasklevel"):
                _fresh_by_rproj[_rp]["tasklevel"] = _m["tasklevel"]
    # 2. Append any new mappings added during this sync run
    _fresh_rprojs = {m.get("rproj") for m in _staff_fresh.get("wbs_mappings", [])}
    for _m in config.get("wbs_mappings", []):
        if _m.get("rproj") not in _fresh_rprojs:
            _staff_fresh.setdefault("wbs_mappings", []).append(_m)
            _fresh_rprojs.add(_m.get("rproj"))
    # 3. Stamp timestamp
    _staff_fresh["_staffing_synced_at"] = datetime.now().isoformat()
    save_config(_staff_fresh)
    return added


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def business_days(start: date, end: date) -> list[date]:
    """Return Mon–Fri dates between start and end inclusive."""
    days: list = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days
