#!/usr/bin/env python3
"""
catxt_sync.py — Outlook Calendar → CATXT Time Entry Sync
=========================================================
Reads today's qualifying calendar events from Outlook and creates
time entries in SAP CATXT (ZCATSXTMO OData service).

Runs nightly Mon–Fri. On first run (or when session expires) it will
open a browser window for SSO login, then store the session cookies
for subsequent runs.

Usage:
    python catxt_sync.py            # process today
    python catxt_sync.py --date 2026-07-28   # process a specific date
    python catxt_sync.py --list     # preview today's events without posting
"""

import sys
import json
import logging
import argparse
import re
import os
import time
import requests
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR    = Path(__file__).parent
CONFIG_FILE   = SCRIPT_DIR / "config.json"
COOKIES_FILE  = SCRIPT_DIR / ".auth_cookies.json"
PROCESSED_FILE= SCRIPT_DIR / ".processed_events.json"
LOG_FILE      = SCRIPT_DIR / "catxt_sync.log"

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
PERNR = "01854017"

OWA_CALENDAR_URL = "https://outlook.office.com/calendar/view/day/{date}"

# Event subjects (partial, case-insensitive) to always skip
EXCLUDED_SUBJECTS = [
    "catxt", "cat entry", "time entry", "timesheet entry",
    "administrative tasks",
    "sick day", "sick leave", "half sick",
    "enter time into catsxt",
]

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        log.error(f"Config file not found: {CONFIG_FILE}")
        log.error("Run the script once with --setup to create a default config.")
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def save_config(config: dict):
    CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# Processed-event tracking  (avoid re-submitting)
# ═══════════════════════════════════════════════════════════════════════════════

def load_processed() -> dict:
    if PROCESSED_FILE.exists():
        return json.loads(PROCESSED_FILE.read_text(encoding="utf-8"))
    return {}


def save_processed(processed: dict):
    PROCESSED_FILE.write_text(json.dumps(processed, indent=2), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# Outlook Calendar — via OWA Playwright interception
# No app registration required. Opens OWA in a browser (SSO completes
# automatically), intercepts the Graph API calendarView response, and
# extracts qualifying events for the target date.
# ═══════════════════════════════════════════════════════════════════════════════

def get_events_for_date(target_date: date) -> list[dict]:
    """
    Open Outlook Web App for the target date, intercept the calendar
    API response, and return qualifying events.
    """
    raw_events = []

    with sync_playwright() as p:
        context = None
        edge_profile    = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")
        edge_profile_bak = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data - CATXTSync")

        # ── Strategy 1: use the live Edge profile directly (works when Edge is closed)
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=edge_profile,
                channel="msedge",
                headless=False,
                args=["--window-size=1200,800", "--no-first-run",
                      "--no-default-browser-check", "--disable-sync"],
            )
            log.info("OWA browser: msedge (live profile)")
        except Exception:
            pass

        # ── Strategy 2: copy the profile to a separate folder so Edge can keep running
        if context is None:
            log.info("Edge profile is locked (Edge is running). Copying profile for Playwright...")
            try:
                import shutil

                import sqlite3

                def _copy_file_resilient(src: str, dst: str) -> bool:
                    """
                    Copy a single file. For SQLite databases (Cookies etc.) uses
                    the sqlite3 Online Backup API which works on live, locked files.
                    Falls back to shutil for everything else; silently skips on failure.
                    """
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    name = os.path.basename(src)
                    is_sqlite = (name in ("Cookies", "History", "Login Data",
                                          "Web Data", "Favicons")
                                 or src.endswith(".db"))
                    if is_sqlite:
                        try:
                            # immutable=1 skips all locking — safe for reading cookies
                            uri = "file:" + src.replace("\\", "/") + "?mode=ro&immutable=1"
                            src_con = sqlite3.connect(uri, uri=True)
                            dst_con = sqlite3.connect(dst)
                            src_con.backup(dst_con, pages=200)
                            src_con.close()
                            dst_con.close()
                            return True
                        except Exception as e:
                            log.debug(f"SQLite backup failed for {name}: {e}")
                    try:
                        shutil.copy2(src, dst)
                        return True
                    except OSError:
                        log.debug(f"Skipping locked file: {name}")
                        return False

                def _copy_tree_resilient(src_dir: str, dst_dir: str, ignore_pat):
                    """Recursive copy that handles locked files gracefully."""
                    os.makedirs(dst_dir, exist_ok=True)
                    ignored = shutil.ignore_patterns(*ignore_pat)(src_dir,
                                  os.listdir(src_dir))
                    for name in os.listdir(src_dir):
                        if name in ignored:
                            continue
                        s = os.path.join(src_dir, name)
                        d = os.path.join(dst_dir, name)
                        if os.path.isdir(s):
                            _copy_tree_resilient(s, d, ignore_pat)
                        else:
                            _copy_file_resilient(s, d)

                SKIP = ("Cache", "Code Cache", "GPUCache", "*.log", "*.tmp",
                        "CrashpadMetrics*", "extensions_crx_cache", "Crashpad")

                if os.path.exists(edge_profile_bak):
                    shutil.rmtree(edge_profile_bak, ignore_errors=True)

                for item in ("Default", "Local State", "First Run"):
                    src = os.path.join(edge_profile, item)
                    dst = os.path.join(edge_profile_bak, item)
                    if os.path.isdir(src):
                        _copy_tree_resilient(src, dst, SKIP)
                    elif os.path.isfile(src):
                        try:
                            shutil.copy2(src, dst)
                        except OSError:
                            _win32_copy(src, dst)

                context = p.chromium.launch_persistent_context(
                    user_data_dir=edge_profile_bak,
                    channel="msedge",
                    headless=False,
                    args=["--window-size=1200,800", "--no-first-run",
                          "--no-default-browser-check", "--disable-sync"],
                )
                log.info("OWA browser: msedge (profile copy — SSO should be automatic)")
            except Exception as copy_err:
                log.warning(f"Profile copy failed: {copy_err}")

        if context is None:
            log.error(
                "Could not open Edge with your profile.\n"
                "  Try closing Edge completely and re-running the script."
            )
            return []

        def on_response(response):
            """Capture calendarView API responses from Graph or OWA."""
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

                    # Log first 1000 chars of GetCalendarView for diagnostics
                    if "getcalendarview" in url_lower:
                        log.debug(f"GetCalendarView body (first 1000):\n{body_text[:1000]}")

                    try:
                        data = json.loads(body_text)
                    except Exception:
                        log.debug(f"Non-JSON response from calendar endpoint: {url[:80]}")
                        return

                    events = []

                    # ── Graph API format: {"value": [...]} ───────────────────
                    if "value" in data and isinstance(data["value"], list):
                        events = data["value"]

                    # ── OWA service.svc format: Body.Items ───────────────────
                    if not events:
                        owa_items = data.get("Body", {}).get("Items", [])
                        if isinstance(owa_items, list):
                            events.extend(owa_items)

                    # ── Log top-level keys if still empty ────────────────────
                    if not events:
                        log.debug(f"Calendar response top-level keys: {list(data.keys())[:10]}")

                    if events:
                        raw_events.extend(events)
                        log.info(f"Captured {len(events)} event(s) from: {url[:80]}")
            except Exception:
                pass

        context.on("response", on_response)
        page = context.new_page()

        date_str = target_date.strftime("%Y-%m-%d")
        owa_url  = OWA_CALENDAR_URL.format(date=date_str)
        log.info(f"Opening OWA calendar for {date_str}.")

        try:
            page.goto(owa_url, timeout=60_000, wait_until="domcontentloaded")
        except Exception as nav_err:
            log.warning(f"Navigation warning: {nav_err}")

        # Wait until calendar API data arrives — up to 3 minutes
        log.info("Waiting for calendar data (up to 3 minutes)...")
        deadline = time.time() + 180
        while time.time() < deadline:
            if raw_events:
                log.info("Calendar data received.")
                break
            try:
                page.wait_for_timeout(1_000)
            except Exception:
                # Page was closed (e.g. user closed the window or a redirect error)
                log.warning("Browser window was closed before calendar data was received.")
                break
        else:
            log.warning("Timed out waiting for calendar data.")

        # Small extra pause to catch any remaining batch responses
        if raw_events:
            try:
                page.wait_for_timeout(2_000)
            except Exception:
                pass

        try:
            context.close()
        except Exception:
            pass

    if not raw_events:
        log.warning("No calendar events captured from OWA.")
        return []

    return _filter_events(raw_events, target_date)


def _filter_events(raw_events: list, target_date: date) -> list[dict]:
    """
    Apply inclusion/exclusion rules to raw event objects.
    Handles both Graph API (camelCase) and OWA service.svc (PascalCase) formats.
    """
    qualifying = []
    _logged_owa_keys = False  # log OWA event shape once for diagnostics

    for ev in raw_events:
        try:
            # ── Detect format: OWA uses PascalCase, Graph uses camelCase ─────
            is_owa = "Subject" in ev   # OWA CalendarItem has PascalCase keys

            # Log the full key list of the first OWA event for diagnostics
            if is_owa and not ev.get("IsAllDay") and not _logged_owa_keys:
                _logged_owa_keys = True
                log.debug(f"OWA CalendarItem keys: {sorted(ev.keys())}")

            # ── Subject ──────────────────────────────────────────────────────
            subject = (ev.get("Subject") if is_owa else ev.get("subject")) or ""

            # ── Skip all-day events ──────────────────────────────────────────
            # OWA uses "IsAllDay"; Graph uses "isAllDay"
            is_all_day = ev.get("IsAllDay") if is_owa else ev.get("isAllDay")
            if is_all_day:
                continue

            # ── Skip excluded keywords ───────────────────────────────────────
            if any(kw in subject.lower() for kw in EXCLUDED_SUBJECTS):
                log.debug(f"Excluded by keyword: {subject}")
                continue

            # ── Quick date pre-check before heavier parsing ──────────────────
            raw_start = ev.get("Start") or ev.get("start", {})
            if isinstance(raw_start, dict):
                raw_start = raw_start.get("dateTime", "")
            # Compare just the date portion (first 10 chars: YYYY-MM-DD)
            if raw_start[:10] != str(target_date):
                continue

            # ── Organiser email & timing ─────────────────────────────────────
            if is_owa:
                organiser_email = (
                    (ev.get("Organizer") or {})
                    .get("EmailAddress", {})
                    .get("Address", "")
                    .lower()
                )
                start_str = ev.get("Start", "")
                end_str   = ev.get("End",   "")
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

            # ── Duration ─────────────────────────────────────────────────────
            # OWA timestamps include timezone offsets (e.g. "2026-07-29T09:00:00-04:00")
            # datetime.fromisoformat() handles all variants: naive, UTC Z, and ±HH:MM
            def _parse_dt(s):
                s = s.rstrip("Z")  # remove trailing Z if present (handled below)
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                try:
                    return datetime.fromisoformat(s)
                except ValueError:
                    fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in s else "%Y-%m-%dT%H:%M:%S"
                    return datetime.strptime(s, fmt)

            start_dt = _parse_dt(start_str)
            end_dt   = _parse_dt(end_str)
            # Both datetimes are in the same timezone so subtraction is safe
            duration_hours = round((end_dt - start_dt).total_seconds() / 3600, 2)

            # OWA ResponseType: "Organizer" | "Accept" | "Tentative" | "Decline" | "None"
            response_type = (ev.get("ResponseType") or "").lower() if is_owa else \
                            (ev.get("responseStatus") or {}).get("response", "").lower()
            is_organizer = response_type == "organizer" or (ev.get("IsOrganizer") is True)

            qualifying.append({
                "id":              event_id,
                "subject":         subject,
                "start":           start_str,
                "duration_hours":  duration_hours,
                "organiser_email": organiser_email,
                "organised_by_me": is_organizer,
            })

        except Exception as exc:
            log.warning(f"Error processing calendar event: {exc}")

    return qualifying


# ═══════════════════════════════════════════════════════════════════════════════
# WBS Mapping
# ═══════════════════════════════════════════════════════════════════════════════

def find_wbs_mapping(event: dict, config: dict) -> dict | None:
    """Return the first matching WBS mapping, or the default mapping, or None."""
    subject_lower   = event["subject"].lower()
    organiser_lower = event["organiser_email"].lower()

    for mapping in config.get("wbs_mappings", []):
        for kw in mapping.get("keywords", []):
            if kw.lower() in subject_lower:
                return mapping
        for ep in mapping.get("email_patterns", []):
            if ep.lower() in organiser_lower:
                return mapping

    # Fall back to the default mapping (cost center) if configured
    default = config.get("default_mapping")
    if default:
        log.debug(
            f"No specific mapping for '{event['subject'][:50]}' "
            f"(organiser: {event['organiser_email'] or '(none)'}) — using default."
        )
        return default

    return None


def prompt_for_mapping(event: dict, config: dict) -> dict | None:
    """
    Interactively ask the user which WBS to assign an unmapped event to.
    Optionally saves the mapping back to config for future runs.
    """
    print(f"\n{'─'*60}")
    print(f"No mapping for: \"{event['subject']}\"")
    print(f"  Organiser : {event['organiser_email']}")
    print(f"  Duration  : {event['duration_hours']}h")
    print()
    mappings = config.get("wbs_mappings", [])
    for i, m in enumerate(mappings, 1):
        print(f"  {i:2}. {m['label']}  ({m['wbs']})")
    print(f"   0. Skip this event")
    print()

    while True:
        raw = input("Enter number: ").strip()
        if raw == "0":
            return None
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(mappings):
                chosen = mappings[idx]
                # Offer to save the rule
                save_hint = input(
                    "Save mapping? Enter a keyword or email pattern to remember "
                    "(or press Enter to skip): "
                ).strip()
                if save_hint:
                    if "@" in save_hint or save_hint.startswith("*"):
                        chosen.setdefault("email_patterns", []).append(save_hint.lower())
                    else:
                        chosen.setdefault("keywords", []).append(save_hint.lower())
                    save_config(config)
                    log.info(f"Saved new rule: '{save_hint}' → {chosen['label']}")
                return chosen
        except ValueError:
            pass
        print("Invalid choice. Try again.")


# ═══════════════════════════════════════════════════════════════════════════════
# Authentication
# ═══════════════════════════════════════════════════════════════════════════════

def load_cookies() -> list | None:
    if COOKIES_FILE.exists():
        return json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
    return None


def save_cookies(cookies: list):
    COOKIES_FILE.write_text(json.dumps(cookies, indent=2), encoding="utf-8")


def authenticate_via_browser() -> list | None:
    """
    Open Edge/Chrome for SSO login and extract session cookies.
    A browser window will appear — complete the SSO login if prompted.
    Cookies are saved locally for subsequent runs.
    """
    log.info("Opening browser for CATXT authentication (SSO)...")
    print("\nA browser window will open. Please log in if prompted.")
    print("The window will close automatically once authenticated.\n")

    cookies = None
    with sync_playwright() as p:
        edge_profile = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")
        context = None

        # Use existing Edge profile so SSO is automatic
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=edge_profile,
                channel="msedge",
                headless=False,
                args=["--window-size=1200,800", "--no-first-run",
                      "--no-default-browser-check", "--disable-sync"],
            )
            log.info("Browser launched: msedge (existing profile)")
        except Exception as profile_err:
            log.info(f"Could not use Edge profile ({profile_err}) — launching fresh browser.")
            browser = None
            for channel in ("msedge", "chrome", None):
                try:
                    kwargs = dict(headless=False, args=["--window-size=1200,800", "--no-first-run"])
                    if channel:
                        kwargs["channel"] = channel
                    browser = p.chromium.launch(**kwargs)
                    log.info(f"Browser launched: {channel or 'bundled Chromium'} (fresh)")
                    break
                except Exception:
                    continue
            if browser is None:
                log.error("Could not launch any browser.")
                return None
            context = browser.new_context()

        page = context.new_page()

        try:
            page.goto(LOGIN_URL, timeout=60_000)
            # Wait for the launchpad to finish loading (network quiet for 3 s)
            page.wait_for_load_state("networkidle", timeout=120_000)
        except Exception as exc:
            log.warning(f"Navigation wait timed out ({exc}); proceeding anyway.")

        # Extract cookies for both the launchpad and fulfillment domains
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
        browser.close()

    if cookies:
        save_cookies(cookies)
        log.info(f"Authenticated successfully — {len(cookies)} cookies saved.")
    else:
        log.error("No cookies extracted. Authentication failed.")
    return cookies or None


def build_session(cookies: list) -> requests.Session:
    session = requests.Session()
    for c in cookies:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
    session.headers.update({
        "X-Requested-With":    "XMLHttpRequest",
        "Accept":              "application/json",
        "Accept-Language":     "en",
        "DataServiceVersion":  "2.0",
        "MaxDataServiceVersion": "2.0",
    })
    return session


def test_session(session: requests.Session) -> bool:
    """Quick liveness check — returns True if the session is still valid."""
    try:
        r = session.get(f"{BASE_URL}/Userinfo", timeout=15)
        return r.status_code == 200
    except Exception:
        return False


def get_csrf_token(session: requests.Session) -> str | None:
    """Fetch a fresh CSRF token via a HEAD request."""
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


# ═══════════════════════════════════════════════════════════════════════════════
# CATXT API
# ═══════════════════════════════════════════════════════════════════════════════

def get_existing_entries(session: requests.Session, target_date: date,
                         csrf_token: str = "") -> list[dict]:
    """Fetch existing Activity entries for the target date via direct GET.

    Queries the full current week (Mon–Sun) and filters to target_date.
    Using a direct GET avoids OData $batch format issues with function imports.
    """
    week_start = (target_date - timedelta(days=target_date.weekday())).strftime("%Y%m%d")
    week_end   = (target_date + timedelta(days=6 - target_date.weekday())).strftime("%Y%m%d")
    date_str   = target_date.strftime("%Y%m%d")

    try:
        r = session.get(
            f"{BASE_URL}/Param(Pernr='{PERNR}',Datefrom='{week_start}',Dateto='{week_end}')/Activity",
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
            log.warning(f"Could not fetch existing entries (HTTP {r.status_code}): {r.text[:500]}")
            return []
        all_results = r.json().get("d", {}).get("results", [])
        return [e for e in all_results if e.get("Workdate", "") == date_str]
    except Exception as exc:
        log.warning(f"Error fetching existing entries: {exc}")
    return []


def post_activity(
    session: requests.Session,
    csrf_token: str,
    event: dict,
    mapping: dict,
    target_date: date,
) -> bool:
    """POST a new Activity (time entry) to CATXT via ActivityHeader deep-insert."""
    workdate = target_date.strftime("%Y%m%d")
    short_text = event["subject"][:40]  # CATXT Ltxa1 is max 40 chars

    rproj  = mapping.get("rproj", "")
    rkostl = mapping.get("rkostl", "")
    rkdauf = mapping.get("rkdauf", "")
    rkdpos = mapping.get("rkdpos", "") if rkdauf else ""

    # Tasktype: use mapping value, or infer from account assignment type.
    # WBS/project work → CFPP; overhead/cost-centre → MEET
    tasktype = mapping.get("tasktype", "")
    if not tasktype:
        tasktype = "CFPP" if rproj else "MEET"

    # Taskcomponent: WORKHRS for project hours, WORKSTAT for overhead/admin
    taskcomponent = mapping.get("taskcomponent", "")
    if not taskcomponent:
        taskcomponent = "WORKHRS" if rproj else "WORKSTAT"

    # Skostl (sending cost center): required for WBS entries; blank for CC entries
    skostl = "0800080808" if rproj else ""

    # Rproj for CC-only entries must be 24 zeros, not empty string
    rproj_val = rproj if rproj else "000000000000000000000000"

    # Obart / Objnr — object type and internal object number.
    # These appear in all existing records and likely required for correct cost-object posting.
    # WBS (project): Obart="PR", Objnr="PR" + last 8 chars of the 24-char Rproj internal number
    # Cost centre:   Obart="KS", Objnr="KS" + "0001" (COAREA) + rkostl
    if rproj:
        obart = "PR"
        objnr = "PR" + rproj[-8:]          # e.g. "PR01386783"
    else:
        obart = "KS"
        objnr  = "KS0001" + rkostl         # e.g. "KS00010800080808"

    # Zcpr fields: Cloud Project Resource identifiers for CPR-based WBS entries.
    # wbs in config is the Zcpr_objgextid (e.g. "CPS.40012580.00002").
    # Zcpr_extid is the project root (everything before the last segment).
    wbs = mapping.get("wbs", "")
    if rproj and wbs:
        zcpr_objgextid = wbs                                  # e.g. "CPS.40012580.00002"
        zcpr_extid     = wbs.rsplit(".", 1)[0] if "." in wbs else wbs  # e.g. "CPS.40012580"
        zcpr_objtype   = "TTO"
    else:
        zcpr_objgextid = ""
        zcpr_extid     = ""
        zcpr_objtype   = ""

    # Activity line — mirrors the shape of existing records from the service
    activity_line = {
        "Taskcounter":   "",
        "Counter":       "",
        "Tmp_key":       "000000000001",
        "Pernr":         PERNR,
        "Workdate":      workdate,
        "Tasktype":      tasktype,
        "Taskcomponent": taskcomponent,
        "Catsquantity":  f"{event['duration_hours']:.3f}",
        "Unit":          "H",
        "Status":        "10",
        "Ltxa1":         short_text,
        "Skostl":        skostl,
        "Rproj":         rproj_val,
        "Rkostl":        rkostl,
        "Rkdauf":        rkdauf,
        "Rkdpos":        rkdpos if rkdauf else "000000",
        "Raufnr":        "",
        "Rnplnr":        "",
        "Raufpl":        "0000000000",
        "Raplzl":        "00000000",
        "Vornr":         "",
        "Lstnr":         "",
        "Pflag":         "",
        "Waers":         "",
        "Catsamount":    "0.00",
        # Tasklevel required for WBS/CFPP entries. Default "G3" (matches existing records).
        # Override per-mapping via "tasklevel" key in config.
        "Tasklevel":     mapping.get("tasklevel", "G3") if rproj else mapping.get("tasklevel", ""),
        # Zz_location required for WBS/CFPP entries. Default "R" (Remote).
        # Override per-mapping via "zz_location" key in config.
        "Zz_location":   mapping.get("zz_location", "R") if rproj else "",
        "Obart":         obart,
        "Objnr":         objnr,
        "Zcpr_extid":    zcpr_extid,
        "Zcpr_objgextid": zcpr_objgextid,
        "Zcpr_objtype":  zcpr_objtype,
    }

    # ActivityHeader wraps the Activity line as a deep-insert navigation property.
    # This is how the CATXT app saves entries — direct POST to Activity is not supported.
    header_payload = {
        "Taskcounter": "",
        "Testrun":     "",
        "Version":     "",
        "Activity":    [activity_line],
    }

    payload_json = json.dumps(header_payload)
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
        # Batch returns 200/202; check inner HTTP status for the changeset result
        if r.status_code in (200, 202):
            resp_text = r.text
            # Look for HTTP 2xx in the batch response body
            if "HTTP/1.1 201" in resp_text or "HTTP/1.1 200" in resp_text:
                # Determine inner status for diagnostics (201=Created, 200=OK/updated)
                inner_status = "201" if "HTTP/1.1 201" in resp_text else "200"
                log.debug(f"      Response (inner {inner_status}): {resp_text[:1500]}")
                # Extract and log Msgtxt from ActivityHeader response
                try:
                    json_start = resp_text.find('{"d"')
                    if json_start >= 0:
                        hdr    = json.loads(resp_text[json_start:resp_text.rfind('}')+1])
                        d      = hdr.get("d", {})
                        msgtxt = d.get("Msgtxt", "")
                        msgno  = d.get("Msgno", "")
                        if msgtxt:
                            log.debug(f"      Msgtxt: [{msgno}] {msgtxt}")
                        # Treat empty Activity results as failure
                        activity_results = d.get("Activity", {}).get("results", None)
                        if activity_results is not None and len(activity_results) == 0:
                            log.error(f"  ✗  Activity line was dropped by backend: {msgtxt}")
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
                log.error(f"      ActivityLine: {json.dumps(activity_line)}")
                log.error(f"      Batch response: {resp_text[:1000]}")
                return False
        else:
            log.error(f"  ✗  Failed: {event['subject'][:50]} | HTTP {r.status_code}")
            log.error(f"      ActivityLine: {json.dumps(activity_line)}")
            log.error(f"      Response: {r.text[:800]}")
            return False
    except Exception as exc:
        log.error(f"  ✗  Exception posting entry: {exc}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def sync_staffing(session: requests.Session, config: dict, csrf_token: str = "") -> int:
    """
    Query the CATXT Staffing endpoint and add any new/unknown projects to config.json.
    Existing entries (matched by rproj) are left untouched so keywords are preserved.
    Returns the number of new projects added.
    """
    today     = date.today()
    # Use a 6-month window centred on today — matches what the CATXT app sends
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
            log.warning(f"Staffing sync: HTTP {r.status_code} — skipping. Response: {r.text[:500]}")
            return 0

        all_entries = r.json().get("d", {}).get("results", [])
        if not all_entries:
            log.warning("Staffing sync: empty response — skipping.")
            return 0

    except Exception as exc:
        log.warning(f"Staffing sync error: {exc} — skipping.")
        return 0

    # De-duplicate: each project appears once per day in the response
    seen, unique = set(), []
    for entry in all_entries:
        objnr = entry.get("Objnr", "")
        if objnr and objnr not in seen:
            seen.add(objnr)
            unique.append(entry)

    log.info(f"Staffing sync: {len(unique)} active project(s) found.")

    # Build set of rproj values already in config
    existing_rprojs = {m.get("rproj", "") for m in config.get("wbs_mappings", [])}

    added = 0
    for entry in unique:
        rproj    = entry.get("Rproj", "")
        wbs      = entry.get("Objnr_f", "")
        info     = entry.get("Objnr_info", "")
        customer = entry.get("Sold2Party", "")
        endda    = entry.get("Endda", "")

        # Skip entries without a real WBS number.
        # SAP returns "000000000000000000000000" (24 zeros) for sales-order and
        # cost-centre staffing entries — these are not WBS projects.
        if not rproj or rproj == "0" * 24 or rproj in existing_rprojs:
            continue  # already tracked, no key, or non-WBS entry

        parts = [p for p in [customer, info] if p]
        label = " — ".join(parts) if parts else entry.get("Objnr", rproj)

        config.setdefault("wbs_mappings", []).append({
            "label":          label,
            "wbs":            wbs,
            "rproj":          rproj,
            "keywords":       [],
            "email_patterns": [],
            "tasktype":       "",
            "tasklevel":      "",
            "_endda":         endda,
            "_note":          "Auto-added by staffing sync — add keywords/email_patterns to enable auto-mapping.",
        })
        existing_rprojs.add(rproj)
        added += 1
        log.info(f"  + New project: {label} ({wbs or rproj})")

    if added:
        save_config(config)
        log.info(f"Staffing sync: {added} new project(s) added to config.json.")
    else:
        log.info("Staffing sync: config is already up to date.")

    return added


def parse_args():
    p = argparse.ArgumentParser(description="Sync Outlook calendar to CATXT")
    p.add_argument("--date",          metavar="YYYY-MM-DD",
                   help="Process a specific date (default: today)")
    p.add_argument("--from",          dest="date_from", metavar="YYYY-MM-DD",
                   help="First date of a range to process (Mon–Fri only). "
                        "Pair with --to, or omits --to to default to today.")
    p.add_argument("--to",            dest="date_to", metavar="YYYY-MM-DD",
                   help="Last date of range for --from (default: today)")
    p.add_argument("--list",          action="store_true",
                   help="Preview events without posting")
    p.add_argument("--reauth",        action="store_true",
                   help="Force re-authentication")
    p.add_argument("--sync-staffing", action="store_true",
                   help="Sync staffing from CATXT and exit")
    return p.parse_args()


def _process_day(session, csrf_token, target_date, config, processed, list_mode):
    """Process a single day. Returns (ok, skipped, failed)."""
    log.info(f"{'='*60}")
    log.info(f"CATXT Sync  —  {target_date.strftime('%A, %B %d %Y')}")
    if list_mode:
        log.info("(Preview mode — no entries will be posted)")
    log.info(f"{'='*60}")

    # ── Read calendar via OWA ─────────────────────────────────────────────────
    events = get_events_for_date(target_date)
    log.info(f"Found {len(events)} qualifying event(s).")

    if not events:
        log.info("Nothing to do.")
        return 0, 0, 0

    # ── Filter already-processed ──────────────────────────────────────────────
    date_key   = str(target_date)
    done_ids   = set(processed.get(date_key, []))
    new_events = [e for e in events if e["id"] not in done_ids]
    log.info(f"{len(new_events)} event(s) not yet submitted.")

    if not new_events:
        log.info("All events already submitted.")
        return 0, 0, 0

    # ── Map to WBS ────────────────────────────────────────────────────────────
    to_submit = []
    for event in new_events:
        mapping = find_wbs_mapping(event, config)
        if not mapping:
            if sys.stdin.isatty():
                mapping = prompt_for_mapping(event, config)
            else:
                log.warning(
                    f"No WBS mapping for \"{event['subject']}\" "
                    f"(organiser: {event['organiser_email']}) — skipping. "
                    f"Add a rule to config.json."
                )
        if mapping:
            to_submit.append((event, mapping))

    if not to_submit:
        log.info("No events with a WBS mapping to submit.")
        return 0, 0, 0

    # ── Preview mode ──────────────────────────────────────────────────────────
    if list_mode:
        print(f"\n{'─'*60}")
        print(f"{'SUBJECT':<45} {'HOURS':>5}  WBS MAPPING")
        print(f"{'─'*60}")
        for event, mapping in to_submit:
            print(
                f"{event['subject'][:44]:<45} "
                f"{event['duration_hours']:>5.2f}  "
                f"{mapping['label']}"
            )
        print(f"{'─'*60}")
        print(f"Total: {sum(e['duration_hours'] for e, _ in to_submit):.2f}h\n")
        return 0, 0, 0

    # ── Refresh CSRF token per day ────────────────────────────────────────────
    day_csrf = get_csrf_token(session)
    if not day_csrf:
        log.error("Could not obtain CSRF token — skipping day.")
        return 0, 0, len(to_submit)

    # ── Check existing entries in CATXT (dedup) ───────────────────────────────
    existing    = get_existing_entries(session, target_date, csrf_token=day_csrf)
    existing_lc = {e.get("Ltxa1", "").lower() for e in existing}
    log.info(f"Found {len(existing)} existing CATXT entr(ies) for today.")

    # ── Post entries ──────────────────────────────────────────────────────────
    processed_ids = processed.setdefault(date_key, [])
    ok, skipped   = 0, 0

    for event, mapping in to_submit:
        if event["subject"][:40].lower() in existing_lc:
            log.info(f"  ↩  Already in CATXT: {event['subject'][:50]}")
            processed_ids.append(event["id"])
            skipped += 1
            continue

        if post_activity(session, day_csrf, event, mapping, target_date):
            processed_ids.append(event["id"])
            ok += 1

    failed = len(to_submit) - ok - skipped
    log.info(f"{'='*60}")
    log.info(f"Done — {ok} posted, {skipped} already existed, {failed} failed.")
    return ok, skipped, failed


def _business_days(start: date, end: date) -> list:
    """Return Mon–Fri dates between start and end inclusive."""
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def main():
    args = parse_args()

    # ── Determine target date(s) ──────────────────────────────────────────────
    today = date.today()

    if args.date_from:
        start = datetime.strptime(args.date_from, "%Y-%m-%d").date()
        end   = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else today
        if start > end:
            log.error(f"--from {start} is after --to {end}.")
            sys.exit(1)
        target_dates = _business_days(start, end)
        if not target_dates:
            log.info("No business days in the specified range.")
            return
        log.info(f"Processing {len(target_dates)} business day(s): "
                 f"{target_dates[0]} → {target_dates[-1]}")
    elif args.date:
        target_dates = [datetime.strptime(args.date, "%Y-%m-%d").date()]
    else:
        if today.weekday() >= 5:
            log.info(f"Today is {today.strftime('%A')} — weekend, skipping.")
            return
        target_dates = [today]

    # ── Load config and processed events ─────────────────────────────────────
    config    = load_config()
    processed = load_processed()

    # ── Authenticate (once) ───────────────────────────────────────────────────
    cookies = None if args.reauth else load_cookies()
    session = None

    if cookies:
        session = build_session(cookies)
        if not test_session(session):
            log.info("Saved session expired — re-authenticating.")
            cookies, session = None, None

    if not cookies:
        cookies = authenticate_via_browser()
        if not cookies:
            log.error("Authentication failed. Exiting.")
            sys.exit(1)
        session = build_session(cookies)

    # ── CSRF token (once, for staffing sync) ──────────────────────────────────
    csrf_token_early = get_csrf_token(session)
    if not csrf_token_early:
        log.error("Could not obtain CSRF token. Exiting.")
        sys.exit(1)

    # ── Sync staffing assignments (once) ──────────────────────────────────────
    sync_staffing(session, config, csrf_token_early)

    if getattr(args, "sync_staffing", False):
        log.info("Staffing sync complete.")
        return

    # ── Per-day loop ──────────────────────────────────────────────────────────
    total_ok = total_skipped = total_failed = 0

    for target_date in target_dates:
        ok, skipped, failed = _process_day(
            session, csrf_token_early, target_date, config, processed,
            list_mode=args.list,
        )
        total_ok      += ok
        total_skipped += skipped
        total_failed  += failed
        save_processed(processed)   # persist after each day so progress survives errors

    if len(target_dates) > 1:
        log.info(f"{'='*60}")
        log.info(
            f"Range complete — {total_ok} posted, {total_skipped} already existed, "
            f"{total_failed} failed across {len(target_dates)} day(s)."
        )


if __name__ == "__main__":
    main()
