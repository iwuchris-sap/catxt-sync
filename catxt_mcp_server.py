"""
catxt_mcp_server.py — MCP Connector for CATXT Sync
====================================================
Implements the MCP streamable-HTTP transport using starlette + uvicorn
directly.  No FastMCP dependency — works with any mcp package version.

Exposes eleven tools Joule can call:
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


# ══════════════════════════════════════════════════════════════════════════════
# Auth helper
# ══════════════════════════════════════════════════════════════════════════════

def _get_session():
    """Return a live requests.Session or raise a descriptive RuntimeError."""
    session = core.get_or_refresh_session(cookies_only=True)
    if session is None:
        raise RuntimeError(
            "No active CATXT session — open the CATXT Sync tray app and "
            "ensure it is authenticated (SAP icon in the system tray)."
        )
    return session


# ══════════════════════════════════════════════════════════════════════════════
# Tool implementations
# ══════════════════════════════════════════════════════════════════════════════

def _tool_get_mappings(args: dict) -> str:
    config = core.load_config()
    result = [
        {
            "label":          m.get("label", ""),
            "wbs":            m.get("wbs", ""),
            "rproj":          m.get("rproj", ""),
            "keywords":       m.get("keywords", []),
            "email_patterns": m.get("email_patterns", []),
            "tasktype":       m.get("tasktype", ""),
        }
        for m in config.get("wbs_mappings", [])
    ]
    return json.dumps(result, indent=2)


def _tool_suggest_mapping(args: dict) -> str:
    subject         = args.get("subject", "")
    organiser_email = args.get("organiser_email", "")
    config = core.load_config()
    event  = {"subject": subject, "organiser_email": organiser_email}
    mapping = core._find_specific_mapping(event, config)
    if mapping is None:
        default = config.get("default_mapping", {})
        return json.dumps({
            "matched":  False,
            "fallback": "default_cost_centre",
            "label":    default.get("label", "Default Cost Centre"),
            "rkostl":   default.get("rkostl", ""),
        })
    return json.dumps({
        "matched":  True,
        "label":    mapping.get("label", ""),
        "wbs":      mapping.get("wbs", ""),
        "rproj":    mapping.get("rproj", ""),
        "tasktype": mapping.get("tasktype", ""),
    })


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
                "description": e.get("Ltxa1", ""),
                "hours":       float(e.get("Catsquantity", 0)),
                "tasktype":    e.get("Tasktype", ""),
                "project":     e.get("Rkostl") or e.get("Rproj", ""),
                "status":      e.get("Status", ""),
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

    session = _get_session()
    config  = core.load_config()
    csrf    = core.get_csrf_token(session)
    if not csrf:
        return json.dumps({"success": False, "error": "Could not obtain CSRF token."})

    d = date.fromisoformat(target_date)

    # Resolve mapping by exact label first, then partial match
    mapping = None
    if project_label.lower() == "default":
        mapping = config.get("default_mapping", {})
    else:
        pl = project_label.lower()
        for m in config.get("wbs_mappings", []):
            if m.get("label", "").lower() == pl:
                mapping = m
                break
        if mapping is None:
            for m in config.get("wbs_mappings", []):
                if pl in m.get("label", "").lower():
                    mapping = m
                    break

    if mapping is None:
        return json.dumps({
            "success": False,
            "error": (
                f"No mapping found for '{project_label}'. "
                "Call get_mappings to see available project labels."
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

    ok = core.post_activity(session, csrf, event, mapping, d)
    if ok:
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

    if not keyword:
        return json.dumps({"success": False, "error": "keyword cannot be empty."})
    if ktype not in ("keyword", "email_pattern"):
        return json.dumps({"success": False, "error": "type must be 'keyword' or 'email_pattern'."})

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

    field    = "keywords" if ktype == "keyword" else "email_patterns"
    existing = mapping.setdefault(field, [])

    if any(k.lower() == keyword.lower() for k in existing):
        return json.dumps({
            "success":       False,
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
    """Return True if catxt_app.py (or catxt.exe) process is currently running."""
    import subprocess
    try:
        # tasklist is available on all Windows versions without extra deps
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="ignore").lower()
        return "catxt.exe" in out or "catxt_app" in out
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
            "Return all WBS project mappings configured in CATXT Sync, "
            "including keywords and email patterns used for auto-matching meetings."
        ),
        "inputSchema": {
            "type": "object", "properties": {}, "required": [],
        },
    },
    "suggest_mapping": {
        "fn": _tool_suggest_mapping,
        "description": (
            "Given a meeting subject and optional organiser email, return the "
            "best matching WBS project mapping, or the default cost centre fallback."
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
            "IMPORTANT: always confirm with the user before calling this tool.  "
            "Use get_existing_entries first to avoid exceeding 8h/day.  "
            "Use get_mappings to find the correct project_label value."
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
            result_text = _TOOLS[tool_name]["fn"](arguments)
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
    run()
