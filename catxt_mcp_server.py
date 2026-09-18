"""
catxt_mcp_server.py — MCP Connector for CATXT Sync
====================================================
Implements the MCP streamable-HTTP transport using starlette + uvicorn
directly.  No FastMCP dependency — works with any mcp package version.

Exposes twelve tools Joule can call:
  get_mappings            — list all configured WBS project rules
  suggest_mapping         — match a meeting subject to a project
  get_existing_entries    — what is already posted in CATXT for a date
  get_staffing_assignments — live project list from the CATXT Staffing API
  post_time_entry         — post a single time entry to CATXT
  add_keyword             — add a keyword or email pattern to a project mapping
  remove_keyword          — remove a keyword or email pattern from a project mapping
  get_sync_status         — which days in a range have been processed by the tray app
  clear_sync_history      — wipe processed-event cache so a day can be re-synced
  get_tray_status         — check whether the CATXT Sync tray app is running
  start_tray_app          — launch the CATXT Sync tray app if it is not running
  get_app_info            — return local version and check for available updates

Standalone mode
---------------
This server can run independently of the tray app so Joule can call
get_tray_status / start_tray_app even when the tray app GUI is closed.
Add a Windows Task Scheduler entry via setup.bat to start this server at logon:

    pythonw.exe "<path>\\catxt_mcp_server.py"

The tray app detects port 7432 is already bound and skips starting its own copy.

Authentication:  piggybacks on the tray app's .auth_cookies.json.
The tray app must be running and authenticated for API calls to work.

Run standalone:  python catxt_mcp_server.py
Via tray app:    started automatically on port 7432
"""

import asyncio
import json
import logging
import sys
from datetime import date
from pathlib import Path

# Make catxt_core importable when this file is run from any working directory.
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import catxt_core as core

try:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route
    import uvicorn
except ImportError:
    raise ImportError(
        "starlette and uvicorn are required. "
        "Install them with:  pip install starlette uvicorn"
    )

DEFAULT_PORT = 7432
log = logging.getLogger(__name__)

# CATXT backend acquires an exclusive record lock per PERNR during each batch
# write.  Concurrent POSTs to the same PERNR are rejected with LR(093).
# This lock serialises all post_time_entry calls server-side so Joule can call
# the tool in parallel without hitting backend lock contention.
_post_lock = asyncio.Lock()
# Seconds to wait after each successful post before releasing the lock —
# gives the CATXT backend time to commit and release its own record lock.
_POST_SLEEP_S = 1.5

# Tools that must hold _post_lock (CATXT write operations)
_SERIALISED_TOOLS = {"post_time_entry"}


# ══════════════════════════════════════════════════════════════════════════════
# Auth helper
# ══════════════════════════════════════════════════════════════════════════════

def _get_session():
    """Return a live requests.Session or raise a descriptive RuntimeError.

    Tries saved cookies first (fast path).  If expired, attempts a silent
    headless SSO re-auth using the live Edge profile — this succeeds invisibly
    when corporate SSO is still active, so callers rarely see an error.
    Only raises if both the cookie check and headless re-auth fail.
    """
    # 1. Fast path: saved cookies
    session = core.get_or_refresh_session(cookies_only=True)
    if session is not None:
        return session
    # 2. Cookies expired — try silent headless SSO re-auth
    log.info("_get_session: cookies expired — attempting silent SSO re-auth.")
    session = core.get_or_refresh_session(headless_only=True)
    if session is not None:
        log.info("_get_session: silent SSO re-auth succeeded.")
        return session
    raise RuntimeError(
        "No active CATXT session — open the CATXT Sync tray app and "
        "ensure it is authenticated (SAP icon in the system tray)."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Tool implementations
# ══════════════════════════════════════════════════════════════════════════════

def _tool_get_mappings(args: dict) -> str:
    config = core.load_config()
    result = []
    for m in config.get("wbs_mappings", []):
        entry: dict = {
            "label":          m.get("label", ""),
            "wbs":            m.get("wbs", ""),
            "rproj":          m.get("rproj", ""),
            "keywords":       m.get("keywords", []),
            "email_patterns": m.get("email_patterns", []),
            "tasktype":       m.get("tasktype", ""),
        }
        # Expose Sales-Document fields so the model knows this is an SO project
        rkdauf = m.get("rkdauf", "")
        if rkdauf:
            entry["rkdauf"]        = rkdauf
            entry["rkdpos"]        = m.get("rkdpos", "")
            entry["receiving_type"] = "sales_order"
        elif m.get("rproj") and m.get("rproj", "").strip("0"):
            entry["receiving_type"] = "wbs_project"
        else:
            entry["receiving_type"] = "cost_centre"
        result.append(entry)
    # Also expose auto_mappings (CC-only keyword rules) so the model can see
    # and use them — these are the entries that suggest_mapping can return but
    # that previously had no corresponding project_label in post_time_entry.
    auto = [
        {
            "label":          am.get("label", am.get("keyword", "")),
            "keyword":        am.get("keyword", ""),
            "tasktype":       am.get("tasktype", ""),
            "subtype":        am.get("zzsubtype", ""),
            "rkostl":         am.get("rkostl", ""),
            "ltxa1":          am.get("ltxa1", ""),
            "receiving_type": "cost_centre",
        }
        for am in config.get("auto_mappings", [])
    ]
    return json.dumps({
        "mappings":          result,
        "auto_mappings":     auto,
        "excluded_keywords": config.get("_excluded_keywords", []),
    }, indent=2)


def _is_excluded(subject: str, config: dict) -> tuple[bool, str]:
    """Return (True, matched_keyword) if the subject matches any excluded keyword."""
    subject_lc = subject.lower()
    for kw in config.get("_excluded_keywords", []):
        if kw.lower() in subject_lc:
            return True, kw
    return False, ""


def _tool_get_app_info(_args: dict) -> str:
    config = core.load_config()
    result = core.check_for_update(config)
    return json.dumps(result)


def _mapping_to_result(mapping: dict) -> dict:
    """Build the suggest_mapping result dict from a single mapping entry."""
    rproj_raw  = mapping.get("rproj", "")
    rkdauf     = mapping.get("rkdauf", "")
    rproj_real = rproj_raw if (rproj_raw and rproj_raw.strip("0")) else ""
    if rkdauf:
        receiving_type = "sales_order"
    elif rproj_real:
        receiving_type = "wbs_project"
    else:
        receiving_type = "cost_centre"
    result: dict = {
        "label":          mapping.get("label", ""),
        "project_label":  mapping.get("label", ""),  # pass this to post_time_entry
        "receiving_type": receiving_type,
        "tasktype":       mapping.get("tasktype", ""),
        "subtype":        mapping.get("zzsubtype", ""),   # zzsubtype for post_time_entry
        "rkostl":         mapping.get("rkostl", ""),
        "rproj":          rproj_real,   # empty string when CC-only, not 24 zeros
        "wbs":            mapping.get("wbs", ""),
        "ltxa1":          mapping.get("ltxa1", ""),  # canonical description override
    }
    if rkdauf:
        result["rkdauf"] = rkdauf
        result["rkdpos"] = mapping.get("rkdpos", "")
    if receiving_type == "cost_centre":
        result["note"] = (
            "This is a cost-centre entry (no WBS).  "
            "Use project_label exactly as shown and pass subtype to post_time_entry."
        )
    return result


def _tool_suggest_mapping(args: dict) -> str:
    subject         = args.get("subject", "")
    organiser_email = args.get("organiser_email", "")
    config = core.load_config()

    # Check exclusion list first — excluded events should never be posted
    excluded, matched_kw = _is_excluded(subject, config)
    if excluded:
        return json.dumps({
            "excluded":        True,
            "matched_keyword": matched_kw,
            "message": (
                f"This event matches the excluded keyword '{matched_kw}' "
                "and should not be posted to CATXT."
            ),
        })

    event = {"subject": subject, "organiser_email": organiser_email}

    # ── Ambiguity check: collect ALL matching wbs_mappings entries ──────────
    # More than one match means the event is ambiguous — two WBS lines share
    # the same keyword (e.g. duplicate staffing rows for the same customer).
    # We must ask the user which one to use rather than silently picking first.
    all_wbs = core._find_all_wbs_mappings(event, config)

    if len(all_wbs) > 1:
        candidates = [_mapping_to_result(m) for m in all_wbs]
        # Strip the cost-centre note from candidates — it clutters the list
        for c in candidates:
            c.pop("note", None)
        return json.dumps({
            "excluded":   False,
            "matched":    True,
            "ambiguous":  True,
            "message": (
                "Multiple WBS projects match this event. "
                "Ask the user which one to use before posting."
            ),
            "candidates": candidates,
        })

    if len(all_wbs) == 1:
        result = _mapping_to_result(all_wbs[0])
        result["excluded"] = False
        result["matched"]  = True
        return json.dumps(result)

    # ── 0 wbs_mappings matched — fall through to auto_mappings / favorites ──
    mapping = core._find_specific_mapping(event, config)
    if mapping is None:
        default = config.get("default_mapping", {})
        return json.dumps({
            "excluded": False,
            "matched":  False,
            "fallback": "default_cost_centre",
            "label":    default.get("label", "Default Cost Centre"),
            "rkostl":   default.get("rkostl", ""),
        })
    result = _mapping_to_result(mapping)
    result["excluded"] = False
    result["matched"]  = True
    return json.dumps(result)


def _tool_get_existing_entries(args: dict) -> str:
    target_date = args.get("target_date", "")
    session = _get_session()
    d       = date.fromisoformat(target_date)
    entries = core.get_existing_entries(session, d)
    total_h = round(sum(float(e.get("Catsquantity", 0)) for e in entries), 2)
    return json.dumps({
        "date":        target_date,
        "total_hours": total_h,
        "entry_count": len(entries),
        "entries": [
            {
                "description":    e.get("Ltxa1", ""),
                "hours":          float(e.get("Catsquantity", 0)),
                "tasktype":       e.get("Tasktype", ""),
                "project":        e.get("Rproj", "") or e.get("Rkostl", "") or e.get("Rkdauf", ""),
                "sales_order":    e.get("Rkdauf", ""),   # non-empty for SO entries
                "status":         e.get("Status", ""),
            }
            for e in entries
        ],
    }, indent=2)


def _tool_get_staffing_assignments(args: dict) -> str:
    session = _get_session()
    config  = core.load_config()
    csrf    = core.get_csrf_token(session)
    core.sync_staffing(session, config, csrf or "", force=True)
    fresh = core.load_config()
    return json.dumps({
        "projects": [
            {
                "label":        m.get("label", ""),
                "wbs":          m.get("wbs", ""),
                "rproj":        m.get("rproj", ""),
                "tasklevel":    m.get("tasklevel", ""),
                "end_date":     m.get("_endda", ""),
                "has_keywords": bool(m.get("keywords") or m.get("email_patterns")),
            }
            for m in fresh.get("wbs_mappings", [])
            if m.get("rproj")
        ]
    }, indent=2)


def _tool_post_time_entry(args: dict) -> str:
    target_date       = args.get("target_date", "")
    project_label     = args.get("project_label", "")
    hours             = float(args.get("hours", 1.0))
    description       = args.get("description", "")
    tasktype_override = args.get("tasktype", "")
    subtype           = args.get("subtype", "")
    # Optional: Outlook calendar event ID. When provided and the post succeeds,
    # the event is marked as processed so the tray app's scheduled sync won't
    # re-present it in the review dialog.
    calendar_event_id = args.get("calendar_event_id", "")

    session = _get_session()
    config  = core.load_config()
    csrf    = core.get_csrf_token(session)
    if not csrf:
        return json.dumps({"success": False, "error": "Could not obtain CSRF token."})

    d = date.fromisoformat(target_date)

    # Resolve mapping: wbs_mappings → auto_mappings → _favorites → default
    mapping = None
    if project_label.lower() == "default":
        mapping = config.get("default_mapping", {})
    else:
        pl = project_label.lower()

        # 1. Exact label match in wbs_mappings (WBS / SO projects)
        for m in config.get("wbs_mappings", []):
            if m.get("label", "").lower() == pl:
                mapping = m
                break
        # 2. Partial label match in wbs_mappings
        if mapping is None:
            for m in config.get("wbs_mappings", []):
                if pl in m.get("label", "").lower():
                    mapping = m
                    break
        # 3. Exact label match in auto_mappings (CC-only keyword rules)
        if mapping is None:
            for am in config.get("auto_mappings", []):
                am_label = (am.get("label") or am.get("keyword", "")).lower()
                if am_label == pl:
                    mapping = dict(am)
                    mapping.setdefault("_ltxa1_override", am.get("ltxa1", ""))
                    break
        # 4. Partial label match in auto_mappings
        if mapping is None:
            for am in config.get("auto_mappings", []):
                am_label = (am.get("label") or am.get("keyword", "")).lower()
                if pl in am_label or am_label in pl:
                    mapping = dict(am)
                    mapping.setdefault("_ltxa1_override", am.get("ltxa1", ""))
                    break
        # 5. Exact label match in _favorites (saved CATXT entries)
        if mapping is None:
            for fav in config.get("_favorites", []):
                fav_label = (fav.get("label") or fav.get("ltxa1", "")).lower()
                if fav_label == pl:
                    mapping = dict(fav)
                    mapping.setdefault("_ltxa1_override", fav.get("ltxa1", ""))
                    break

    if mapping is None:
        return json.dumps({
            "success": False,
            "error": (
                f"No mapping found for '{project_label}'. "
                "Call get_mappings to see available project labels "
                "(check both 'mappings' and 'auto_mappings' in the response)."
            ),
        })

    # Apply any per-call overrides
    if tasktype_override or subtype:
        mapping = dict(mapping)
        if tasktype_override:
            mapping["tasktype"] = tasktype_override
        if subtype:
            mapping["zzsubtype"] = subtype

    # post_activity expects an event dict with at least subject and duration_hours
    event = {
        "subject":         description,
        "organiser_email": "",
        "duration_hours":  hours,
    }

    # Pre-fetch existing entries so post_activity can detect "returned existing entry"
    # false-positives even when Zzmoberrflag is not "V".
    try:
        existing = core.get_existing_entries(session, d)
        existing_tcs = {e.get("Taskcounter", "") for e in existing if e.get("Taskcounter")}
    except Exception:
        existing_tcs = set()

    ok = core.post_activity(session, csrf, event, mapping, d,
                            existing_taskcounters=existing_tcs)
    if ok:
        # Mark the calendar event as processed so the tray app's scheduled
        # sync doesn't re-present it in the review dialog.
        if calendar_event_id:
            try:
                processed = core.load_processed()
                day_key   = str(d)
                ids       = processed.setdefault(day_key, [])
                if calendar_event_id not in ids:
                    ids.append(calendar_event_id)
                    core.save_processed(processed)
                    log.debug(
                        f"post_time_entry: marked {calendar_event_id!r} "
                        f"as processed for {day_key}"
                    )
            except Exception as exc:
                log.warning(f"post_time_entry: could not update processed cache: {exc}")

        return json.dumps({
            "success":     True,
            "date":        target_date,
            "project":     mapping.get("label", ""),
            "hours":       hours,
            "description": description,
        })
    return json.dumps({
        "success": False,
        "error":   "CATXT rejected the entry — check the tray app log for details.",
    })


def _resolve_mapping(config: dict, project_label: str) -> dict | None:
    """Find a wbs_mapping by exact label then partial match. Returns the mapping dict (live reference)."""
    pl = project_label.lower()
    for m in config.get("wbs_mappings", []):
        if m.get("label", "").lower() == pl:
            return m
    for m in config.get("wbs_mappings", []):
        if pl in m.get("label", "").lower():
            return m
    return None


def _tool_add_keyword(args: dict) -> str:
    project_label = args.get("project_label", "")
    keyword       = (args.get("keyword") or "").strip()
    ktype         = (args.get("type") or "keyword").lower()
    pl            = project_label.strip().lower()   # normalised label for matching

    if not keyword:
        return json.dumps({"success": False, "error": "keyword cannot be empty."})
    if ktype not in ("keyword", "email_pattern"):
        return json.dumps({"success": False, "error": "type must be 'keyword' or 'email_pattern'."})

    config  = core.load_config()
    mapping = _resolve_mapping(config, project_label)

    # ── auto_mappings fallback ────────────────────────────────────────────────
    # auto_mappings use a single 'keyword' field per entry (not a keywords list).
    # "Adding a keyword" here means creating a new entry that inherits the
    # tasktype/subtype/rkostl/ltxa1 from the matched template entry.
    if mapping is None:
        auto = config.get("auto_mappings", [])
        template = None
        for m in auto:
            if m.get("label", "").lower() == pl:
                template = m
                break
        if template is None:
            for m in auto:
                if pl in m.get("label", "").lower():
                    template = m
                    break

        if template is not None:
            # Duplicate check — keyword already in any entry under this label
            if any(
                m.get("keyword", "").lower() == keyword.lower()
                for m in auto
                if m.get("label", "").lower() == template.get("label", "").lower()
            ):
                return json.dumps({
                    "success":        False,
                    "already_exists": True,
                    "message": f"'{keyword}' is already mapped under '{template['label']}'.",
                })
            new_entry = {
                "label":     template.get("label", ""),
                "keyword":   keyword,
                "tasktype":  template.get("tasktype", ""),
                "zzsubtype": template.get("zzsubtype", ""),
                "rkostl":    template.get("rkostl", ""),
                "rproj":     template.get("rproj", "000000000000000000000000"),
                "ltxa1":     template.get("ltxa1", template.get("label", "")),
            }
            auto.append(new_entry)
            core.save_config(config)
            log.info(f"add_keyword: new auto_mapping entry '{keyword}' under '{template['label']}'")
            return json.dumps({
                "success": True,
                "project": template["label"],
                "added":   keyword,
                "type":    "auto_mapping_entry",
                "note":    "auto_mappings use one entry per keyword — a new entry was created.",
            })

        return json.dumps({
            "success": False,
            "error": (
                f"No mapping found for '{project_label}'. "
                "Call get_mappings to see available project labels and cc_entries."
            ),
        })

    # ── wbs_mappings path — append to keywords / email_patterns list ─────────
    field    = "keywords" if ktype == "keyword" else "email_patterns"
    existing = mapping.setdefault(field, [])

    if any(k.lower() == keyword.lower() for k in existing):
        return json.dumps({
            "success":        False,
            "already_exists": True,
            "message": f"'{keyword}' is already in {field} for '{mapping['label']}'.",
        })

    existing.append(keyword)
    core.save_config(config)
    log.info(f"add_keyword: added '{keyword}' to {field} for '{mapping['label']}'")
    return json.dumps({
        "success": True,
        "project": mapping["label"],
        "added":   keyword,
        "type":    ktype,
        field:     existing,
    })


def _tool_remove_keyword(args: dict) -> str:
    project_label = args.get("project_label", "")
    keyword       = (args.get("keyword") or "").strip()
    ktype         = (args.get("type") or "").lower()  # optional — auto-detect if omitted

    if not keyword:
        return json.dumps({"success": False, "error": "keyword cannot be empty."})

    config  = core.load_config()
    mapping = _resolve_mapping(config, project_label)
    if mapping is None:
        return json.dumps({
            "success": False,
            "error": (
                f"No mapping found for '{project_label}'. "
                "Call get_mappings to see available project labels."
            ),
        })

    removed_from = None

    # Try keywords first (or if explicitly requested)
    if ktype in ("", "keyword"):
        kws     = mapping.get("keywords", [])
        new_kws = [k for k in kws if k.lower() != keyword.lower()]
        if len(new_kws) < len(kws):
            mapping["keywords"] = new_kws
            removed_from = "keywords"

    # Try email_patterns if not found yet
    if removed_from is None and ktype in ("", "email_pattern"):
        eps     = mapping.get("email_patterns", [])
        new_eps = [e for e in eps if e.lower() != keyword.lower()]
        if len(new_eps) < len(eps):
            mapping["email_patterns"] = new_eps
            removed_from = "email_patterns"

    if removed_from is None:
        return json.dumps({
            "success": False,
            "error": (
                f"'{keyword}' not found in keywords or email_patterns "
                f"for '{mapping['label']}'."
            ),
        })

    core.save_config(config)
    log.info(f"remove_keyword: removed '{keyword}' from {removed_from} for '{mapping['label']}'")
    return json.dumps({
        "success":   True,
        "project":   mapping["label"],
        "removed":   keyword,
        "from":      removed_from,
        "remaining": mapping.get(removed_from, []),
    })


def _tool_get_sync_status(args: dict) -> str:
    start_date_str = args.get("start_date", "")
    end_date_str   = args.get("end_date") or start_date_str

    start = date.fromisoformat(start_date_str)
    end   = date.fromisoformat(end_date_str)

    processed = core.load_processed()

    days = []
    cur  = start
    while cur <= end:
        if cur.weekday() < 5:  # business days only
            key        = str(cur)
            event_ids  = processed.get(key, [])
            days.append({
                "date":        key,
                "processed":   key in processed,
                "event_count": len(event_ids),
            })
        cur += date.fromordinal(cur.toordinal() + 1)

    total        = len(days)
    synced       = sum(1 for d in days if d["processed"])
    unsynced     = total - synced

    return json.dumps({
        "start_date":    start_date_str,
        "end_date":      end_date_str,
        "business_days": total,
        "synced":        synced,
        "unsynced":      unsynced,
        "days":          days,
    }, indent=2)


def _tool_clear_sync_history(args: dict) -> str:
    start_date_str = args.get("start_date", "")
    end_date_str   = args.get("end_date") or start_date_str

    start = date.fromisoformat(start_date_str)
    end   = date.fromisoformat(end_date_str)

    n = core.clear_processed_for_range(start, end)
    log.info(f"clear_sync_history: cleared {n} day(s) ({start} – {end})")
    return json.dumps({
        "success":     True,
        "days_cleared": n,
        "start_date":  start_date_str,
        "end_date":    end_date_str,
        "message": (
            f"Cleared sync history for {n} business day(s) "
            f"({start_date_str} – {end_date_str}). "
            "Run a sync to re-process those days."
        ),
    })


def _tray_app_running() -> bool:
    """Return True if catxt_app.py (or catxt.exe) is currently running.

    Primary check: attempt to acquire the msvcrt byte-range lock on
    catxt_app.pid.  catxt_app.py holds this lock for its entire lifetime —
    the OS releases it automatically on exit *or* crash, so it can never go
    stale.  If we can acquire the lock the tray is not running; if we cannot
    (OSError) the tray holds it.

    This replaces the old tasklist/PID approach, which had two bugs:
      - tasklist always returns non-empty output (even "INFO: No tasks…"),
        so ``if out:`` was always True — causing false positives on stale files.
      - The catxt.exe fallback only covered compiled builds, not the Python
        source build, causing false negatives on normal installs.

    Fallback: tasklist check for catxt.exe (PyInstaller compiled build only).
    """
    import msvcrt

    # ── 1. msvcrt lock check (Python source build) ────────────────────────────
    pid_file = _HERE / "catxt_app.pid"
    if pid_file.exists():
        try:
            with open(pid_file, "r+b") as fh:
                try:
                    # Non-blocking exclusive lock on byte 0.
                    # OSError → lock is held by the tray → it is running.
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    # Acquired the lock → tray is NOT running; release immediately.
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                    return False
                except OSError:
                    return True   # lock held → tray IS running
        except Exception:
            pass   # can't open file → fall through to exe check

    # ── 2. Compiled exe fallback (catxt.exe / PyInstaller build) ─────────────
    import subprocess
    try:
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="ignore").lower()
        return "catxt.exe" in out
    except Exception:
        return False


def _tool_get_tray_status(args: dict) -> str:
    running = _tray_app_running()
    return json.dumps({
        "running":    running,
        "status":     "running" if running else "stopped",
        "mcp_server": "running",   # if this tool is callable, the MCP server is up
    })


def _tool_start_tray_app(args: dict) -> str:
    if _tray_app_running():
        return json.dumps({
            "success": False,
            "already_running": True,
            "message": "CATXT Sync is already running — check the system tray.",
        })

    import subprocess
    _HERE = Path(__file__).parent
    vbs   = _HERE / "launch_catxt.vbs"
    py    = _HERE / "catxt_app.py"

    try:
        if vbs.exists():
            # Preferred: use the VBS launcher (has single-instance guard, no console)
            subprocess.Popen(["wscript.exe", str(vbs)], close_fds=True)
            method = "launch_catxt.vbs"
        elif py.exists():
            # Fallback: launch catxt_app.py directly with pythonw (no console)
            subprocess.Popen(
                ["pythonw.exe", str(py)],
                cwd=str(_HERE),
                close_fds=True,
            )
            method = "catxt_app.py (pythonw)"
        else:
            return json.dumps({
                "success": False,
                "error": (
                    f"Could not find catxt_app.py or launch_catxt.vbs in {_HERE}. "
                    "Make sure the tray app is installed correctly."
                ),
            })
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})

    log.info(f"start_tray_app: launched via {method}")
    return json.dumps({
        "success": True,
        "method":  method,
        "message": (
            "CATXT Sync is starting — it should appear in the system tray "
            "within a few seconds."
        ),
    })


# ══════════════════════════════════════════════════════════════════════════════
# Tool registry
# ══════════════════════════════════════════════════════════════════════════════

_TOOLS = {
    "get_mappings": {
        "fn": _tool_get_mappings,
        "description": (
            "Return all project and cost-centre mappings configured in CATXT Sync. "
            "Response contains three keys: "
            "'mappings' (WBS and Sales Order projects), "
            "'auto_mappings' (cost-centre-only keyword rules — ICON/MEET/EDUC entries "
            "such as 'AI Transformation', 'NxL Knowledge Share', team meetings), and "
            "'excluded_keywords' (events that should never be posted). "
            "Always check BOTH 'mappings' and 'auto_mappings' when looking for a project label."
        ),
        "inputSchema": {
            "type": "object", "properties": {}, "required": [],
        },
    },
    "suggest_mapping": {
        "fn": _tool_suggest_mapping,
        "description": (
            "Given a meeting subject and optional organiser email, return the "
            "best matching project mapping. Matches against WBS projects, Sales Order "
            "projects, AND cost-centre-only keyword rules (auto_mappings — e.g. "
            "'AI Transformation', 'NxL Knowledge Share', ICON/MEET/EDUC entries). "
            "Returns excluded:true if the event matches an exclusion keyword. "
            "Returns the default cost centre if nothing matches."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "Meeting or calendar event subject line.",
                },
                "organiser_email": {
                    "type": "string",
                    "description": "Organiser email address (optional).",
                },
            },
            "required": ["subject"],
        },
    },
    "get_existing_entries": {
        "fn": _tool_get_existing_entries,
        "description": (
            "Return time entries already posted in CATXT for a given date, "
            "including total hours posted and a per-entry breakdown."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_date": {
                    "type": "string",
                    "description": "Date in YYYY-MM-DD format, e.g. '2026-09-10'.",
                },
            },
            "required": ["target_date"],
        },
    },
    "get_staffing_assignments": {
        "fn": _tool_get_staffing_assignments,
        "description": (
            "Fetch your current WBS project staffing assignments live from the "
            "CATXT Staffing API.  Returns all active projects and flags which "
            "ones have keyword rules configured."
        ),
        "inputSchema": {
            "type": "object", "properties": {}, "required": [],
        },
    },
    "add_keyword": {
        "fn": _tool_add_keyword,
        "description": (
            "Add a keyword or email pattern to an existing project mapping in CATXT Sync config. "
            "Keywords match anywhere in a meeting subject (case-insensitive). "
            "Email patterns match anywhere in the organiser's email address. "
            "Use get_mappings first to confirm the exact project_label."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_label": {
                    "type": "string",
                    "description": "Project label from get_mappings (partial match accepted).",
                },
                "keyword": {
                    "type": "string",
                    "description": "The keyword or email pattern to add.",
                },
                "type": {
                    "type": "string",
                    "enum": ["keyword", "email_pattern"],
                    "description": "'keyword' to match meeting subjects; 'email_pattern' to match organiser email. Defaults to 'keyword'.",
                },
            },
            "required": ["project_label", "keyword"],
        },
    },
    "remove_keyword": {
        "fn": _tool_remove_keyword,
        "description": (
            "Remove a keyword or email pattern from a project mapping in CATXT Sync config. "
            "Auto-detects whether it's in keywords or email_patterns if type is omitted. "
            "Use get_mappings first to see current keywords for a project."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_label": {
                    "type": "string",
                    "description": "Project label from get_mappings (partial match accepted).",
                },
                "keyword": {
                    "type": "string",
                    "description": "The exact keyword or email pattern to remove.",
                },
                "type": {
                    "type": "string",
                    "enum": ["keyword", "email_pattern"],
                    "description": "Optional. Narrows the search to a specific list.",
                },
            },
            "required": ["project_label", "keyword"],
        },
    },
    "post_time_entry": {
        "fn": _tool_post_time_entry,
        "description": (
            "Post a single time entry to CATXT.  "
            "Supports WBS/project entries, Sales-Order (SO) entries (e.g. Clorox, "
            "Waters, Zero Motorcycles, PTC), and default cost-centre entries — "
            "the correct receiving-object type is resolved automatically from the mapping.  "
            "IMPORTANT: always confirm with the user before calling this tool.  "
            "Use get_existing_entries first to avoid exceeding 8h/day.  "
            "Use get_mappings to find the correct project_label value — "
            "check both 'mappings' (WBS/SO projects) and 'auto_mappings' "
            "(CC-only keyword rules such as NXL Knowledge Share, team meetings)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_date": {
                    "type": "string",
                    "description": "Date in YYYY-MM-DD format.",
                },
                "project_label": {
                    "type": "string",
                    "description": (
                        "Exact label from get_mappings "
                        "(e.g. 'Keurig Dr Pepper — Trading Partner Enablement'), "
                        "or 'default' for the default cost centre."
                    ),
                },
                "hours": {
                    "type": "number",
                    "description": "Hours to post, e.g. 1.0 or 0.5.",
                },
                "description": {
                    "type": "string",
                    "description": "Entry description / meeting title (max 40 chars displayed in CATXT).",
                },
                "tasktype": {
                    "type": "string",
                    "description": "Optional tasktype override (CFPP, MEET, EDUC, …). Leave blank to use the mapping default.",
                },
                "subtype": {
                    "type": "string",
                    "description": "Optional subtype code (MANAGER, WEBEX, …). Leave blank if not needed.",
                },
                "calendar_event_id": {
                    "type": "string",
                    "description": (
                        "Optional Outlook calendar event ID (from list_calendar_events or "
                        "get_calendar_event). When provided and the post succeeds, the event "
                        "is marked as processed so the tray app's scheduled sync won't "
                        "re-present it in the review dialog. Always pass this when posting "
                        "a calendar-derived entry."
                    ),
                },
            },
            "required": ["target_date", "project_label", "hours", "description"],
        },
    },
    "get_sync_status": {
        "fn": _tool_get_sync_status,
        "description": (
            "Return the sync status for each business day in a date range — "
            "whether the tray app has already processed that day and how many "
            "events were captured. Use this to answer 'what days am I missing "
            "this week?' or 'has last Monday been synced?'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "Start of range in YYYY-MM-DD format.",
                },
                "end_date": {
                    "type": "string",
                    "description": "End of range in YYYY-MM-DD format. Defaults to start_date for a single day.",
                },
            },
            "required": ["start_date"],
        },
    },
    "clear_sync_history": {
        "fn": _tool_clear_sync_history,
        "description": (
            "Clear the processed-event cache for a date range so those days "
            "will be re-processed on the next sync. Use when an entry failed "
            "silently or was skipped and the user wants to retry. "
            "IMPORTANT: confirm with the user before calling — this cannot be undone. "
            "Already-posted entries are safe (CATXT's duplicate check prevents re-posting)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "Start of range in YYYY-MM-DD format.",
                },
                "end_date": {
                    "type": "string",
                    "description": "End of range in YYYY-MM-DD format. Defaults to start_date for a single day.",
                },
            },
            "required": ["start_date"],
        },
    },
    "get_tray_status": {
        "fn": _tool_get_tray_status,
        "description": (
            "Check whether the CATXT Sync tray app is currently running. "
            "Returns running=true/false. The MCP server can be running even when "
            "the tray app GUI is closed (standalone mode). "
            "Use this before start_tray_app to avoid launching a second instance."
        ),
        "inputSchema": {
            "type": "object", "properties": {}, "required": [],
        },
    },
    "start_tray_app": {
        "fn": _tool_start_tray_app,
        "description": (
            "Launch the CATXT Sync tray app if it is not already running. "
            "The app will appear in the Windows system tray within a few seconds. "
            "Note: calendar syncing and posting require the tray app to be running "
            "and authenticated. Call get_tray_status first to check."
        ),
        "inputSchema": {
            "type": "object", "properties": {}, "required": [],
        },
    },
    "get_app_info": {
        "fn": _tool_get_app_info,
        "description": (
            "Return the installed version of CATXT Sync and check whether a newer "
            "version is available at the configured update_check_url. "
            "Returns: local_version, update_available (bool), latest_version, "
            "download_url, and changes (brief changelog). "
            "If update_check_url is not set in config.json the remote check is "
            "skipped and update_available is always false. "
            "Call this once at the start of a posting session — do not call it "
            "on every interaction."
        ),
        "inputSchema": {
            "type": "object", "properties": {}, "required": [],
        },
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# MCP JSON-RPC handler (streamable-HTTP transport)
# ══════════════════════════════════════════════════════════════════════════════

async def _mcp_endpoint(request: Request) -> Response:
    """Handle MCP JSON-RPC 2.0 requests."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params", {})

    # ── initialize ─────────────────────────────────────────────────────────
    if method == "initialize":
        return JSONResponse({
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities":    {"tools": {}},
                "serverInfo":      {"name": "CATXT Sync", "version": "1.0.0"},
            },
        })

    # ── notifications (no response body needed) ────────────────────────────
    if method.startswith("notifications/"):
        return Response(status_code=204)

    # ── tools/list ─────────────────────────────────────────────────────────
    if method == "tools/list":
        return JSONResponse({
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "tools": [
                    {
                        "name":        name,
                        "description": meta["description"],
                        "inputSchema": meta["inputSchema"],
                    }
                    for name, meta in _TOOLS.items()
                ]
            },
        })

    # ── tools/call ─────────────────────────────────────────────────────────
    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        if tool_name not in _TOOLS:
            return JSONResponse({
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            })
        try:
            # Run the sync tool function in a thread so Playwright's sync API
            # (which needs its own event loop) doesn't conflict with uvicorn's
            # running asyncio loop.  asyncio.to_thread() requires Python 3.9+.
            #
            # Write tools (post_time_entry) are additionally serialised via
            # _post_lock to prevent concurrent POSTs hitting the CATXT backend
            # record lock (LR(093): Personnel number locked by user ...).
            if tool_name in _SERIALISED_TOOLS:
                async with _post_lock:
                    result_text = await asyncio.to_thread(_TOOLS[tool_name]["fn"], arguments)
                    await asyncio.sleep(_POST_SLEEP_S)
            else:
                result_text = await asyncio.to_thread(_TOOLS[tool_name]["fn"], arguments)
            return JSONResponse({
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "content":  [{"type": "text", "text": result_text}],
                    "isError":  False,
                },
            })
        except Exception as exc:
            log.exception(f"Tool '{tool_name}' raised an exception")
            return JSONResponse({
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "content":  [{"type": "text", "text": f"Error: {exc}"}],
                    "isError":  True,
                },
            })

    # ── unknown method ─────────────────────────────────────────────────────
    return JSONResponse({
        "jsonrpc": "2.0", "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    })


# ══════════════════════════════════════════════════════════════════════════════
# ASGI app + entry point
# ══════════════════════════════════════════════════════════════════════════════

app = Starlette(routes=[
    Route("/mcp", _mcp_endpoint, methods=["POST"]),
])


def run(port: int = DEFAULT_PORT) -> None:
    """Start the MCP server (blocking).  Call from a daemon thread."""
    # log_config=None tells uvicorn to skip its own logging setup and
    # inherit whatever is already configured — avoids conflicts when
    # started inside the tray app which has already called basicConfig().
    uvicorn.run(app, host="127.0.0.1", port=port, log_config=None)


if __name__ == "__main__":
    # Configure file-based logging when running standalone (scheduled task /
    # direct invocation).  pythonw.exe suppresses all stdout/stderr, so without
    # this any crash or error fails completely silently.
    _log_file = _HERE / "catxt_mcp.log"
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)-5s] %(message)s",
        handlers=[logging.FileHandler(_log_file, encoding="utf-8")],
    )
    log.info("catxt_mcp_server starting in standalone mode.")
    run()
