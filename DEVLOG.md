# CATXT Sync — Development Log

## What this tool does
Reads qualifying calendar events from Outlook (via OWA Playwright interception) and
posts them as time entries to SAP CATXT (ZCATSXTMO OData v2 service) automatically.

---

## Key technical decisions & hard-won discoveries

### Authentication
- Uses the user's existing Edge browser profile for SSO — no app registration needed.
- When Edge is running, the profile is locked, so the script copies it to a temp folder
  first and uses that copy.
- Session cookies are saved to `.auth_cookies.json` and reused across runs.

### Calendar data source
- OWA (Outlook Web App) is opened via Playwright in a headless-ish browser.
- The `GetCalendarView` OWA service.svc response is intercepted — it returns events in
  PascalCase (OWA format), not camelCase (Graph API format). Both are handled.
- The script does NOT use the Microsoft Graph API (would need app registration + admin consent).

### CATXT OData service
- Base URL (via launchpad proxy):
  `https://[your-tenant].launchpad.cfapps.[region].hana.ondemand.com`
  `/[app-guid].sapcomcatsxtv2catsxtui5.sapcomcatsxtv2`
  `/sap/opu/odata/sap/ZCATSXTMO`
- CSRF token fetched via HEAD request before any writes.

### Posting time entries — critical findings
The correct write pattern is **POST to `ActivityHeader` with `Activity` as a deep-insert**,
NOT a direct POST to `Activity`. Posting to `Activity` directly is rejected at the gateway.

Required Activity line fields (will silently fail without these):
| Field | Notes |
|-------|-------|
| `Tasktype` | `"CFPP"` for WBS/project, `"MEET"` for cost-centre |
| `Taskcomponent` | `"WORKHRS"` for WBS, `"WORKSTAT"` for cost-centre |
| `Tasklevel` | `"G3"` for this user (grade-specific — must come from Staffing data for others) |
| `Zz_location` | `"R"` (Remote) for WBS entries — **required, backend silently drops Activity line without it** |
| `Skostl` | Sending cost centre (`"0800080808"`) — required for WBS entries, blank for CC |
| `Rproj` | 24-char internal WBS number for project entries; `"000000000000000000000000"` (24 zeros) for CC |
| `Obart` | `"PR"` for WBS, `"KS"` for cost centre |
| `Objnr` | `"PR" + rproj[-8:]` for WBS (e.g. `"PR01386783"`); `"KS0001" + rkostl` for CC |
| `Zcpr_extid` | CPR project root ID (e.g. `"CPS.40001234"`) — derived from `wbs` field in config |
| `Zcpr_objgextid` | Full CPR task ID (e.g. `"CPS.40012580.00002"`) — IS the `wbs` field in config |
| `Zcpr_objtype` | Always `"TTO"` for task objects |

**How the backend signals failure without an HTTP error:**
The ActivityHeader returns HTTP 201 with `"Activity": {"results": []}` (empty array) when
a required field is missing. The script checks for this and treats it as a failure.
The `Msgtxt` field in the response body contains the human-readable error (e.g.
`"CATSXT(014): You have not made a required entry (Location of Activity field)"`).

### Reading existing entries (duplicate detection)
- Use a **direct GET**, NOT a batch GET. The `Param(...)` function import in `$batch` returns
  "malformed syntax" (error code `005056A509B11ED1B9BF94F386DD82E6`) regardless of headers.
- Direct GET works fine:
  `GET /Param(Pernr='01854017',Datefrom='YYYYMMDD',Dateto='YYYYMMDD')/Activity`
- Query the full current week (Mon–Sun) then filter to target date.

### Staffing sync (auto-discover projects)
- Also uses a direct GET (same reason as above — batch gets rejected):
  `GET /Param(Pernr='',Datefrom='YYYYMMDD',Dateto='YYYYMMDD')/Staffing`
- Skip entries where `Rproj == "0" * 24` — those are sales-order or CC entries, not WBS.
- The response includes `Objnr_f`, `Sold2Party`, `Endda` fields useful for labelling.

### PERNR
- Currently hardcoded as `PERNR = "01234567"` near the top of `catxt_sync.py`.
- The `Userinfo` endpoint (called on every run) returns the authenticated user's PERNR.
- **This must be auto-detected before sharing with other users.**

### Tasklevel
- `"G3"` is hardcoded as the default for WBS entries (this user's grade).
- The correct value per project comes from the Staffing response.
- **Must be read from Staffing data before sharing with other users.**
- **`Tasktype = ICON` requires `Tasklevel = "K1"`** (ZCATSXT-225). The backend rejects ICON
  entries with any other value including `""` or `"NONE"`. Other CC types accept `""`.
  Fixed in `catxt_core.py` and `catxt_sync.py` — the fallback now sends `"K1"` when
  `tasktype == "ICON"` and no explicit `tasklevel` is set in the mapping.

---

## Files
| File | Purpose |
|------|---------|
| `catxt_sync.py` | Main script |
| `config.json` | WBS mappings, keywords, email patterns, default cost centre |
| `.auth_cookies.json` | Saved session cookies (gitignore this) |
| `.processed_events.json` | Local dedup cache — event IDs that have been submitted |
| `catxt_sync.log` | Rolling log file |

---

## Config structure
```json
{
  "default_mapping": {
    "label": "...", "wbs": "", "rproj": "", "rkostl": "0800080808",
    "tasktype": "", "tasklevel": ""
  },
  "wbs_mappings": [
    {
      "label": "Customer — Project",
      "wbs": "CPS.40012580.00002",       // = Zcpr_objgextid
      "rproj": "000000000000000001386783", // 24-char internal WBS ID
      "keywords": ["spin master"],
      "email_patterns": ["@spinmaster.com"],
      "tasktype": "CFPP",
      "tasklevel": "G3"
    }
  ]
}
```
Sales order entries use `rkdauf` + `rkdpos` instead of `rproj`/`wbs`.

---

## Next: Windows app work
The plan for the packaged Windows app (new conversation):
1. Auto-detect PERNR from Userinfo API response
2. Auto-detect Tasklevel from Staffing response (per-project)
3. First-run setup wizard (generates config.json interactively)
4. System tray icon (pystray) with right-click "Sync now" + Windows toast notifications
5. Event review screen (tkinter) — show events before posting, allow mapping overrides
6. PyInstaller packaging → single `.exe`
7. Optional: auto-run at 5pm via Windows Task Scheduler

Start the new conversation by reading `catxt_sync.py`, `config.json`, and this file.

---

## Changelog

### 2026-09-15
- **Fix: `Tasklevel = "K1"` required for `Tasktype = ICON` CC entries (ZCATSXT-225)**
  - CATXT rejects ICON entries unless `Tasklevel = "K1"`. The old fallback sent `"NONE"`
    (in `catxt_core.py`) or `""` (in `catxt_sync.py`), both rejected by the backend.
  - Fixed in `catxt_core.py` (`post_time_entry` MCP path) and `catxt_sync.py` (auto-sync path).
  - Other CC types continue to use `""`. WBS/SD entries unchanged (`"G3"` or mapping override).

### 2026-09-11
- **Fix: duplicate review dialog after Joule posts entries**
  - `post_time_entry` now accepts `calendar_event_id` and writes it to `.processed_events.json`.
  - Tray app auto-sync skips events already marked processed — no re-post attempt, no review dialog.
- **Fix: `suggest_mapping` now checks exclusion list before returning a result**
  - Returns `excluded: true` + `matched_keyword` for events matching `_excluded_keywords`.
  - `get_mappings` now includes `excluded_keywords` in the response.
- **Fix: organizer email two-pass lookup in skill**
  - `list_calendar_events` returns display names, not email addresses.
  - Skill now calls `suggest_mapping(subject)` first; only fetches full event detail via
    `get_calendar_event` for unmatched events, then retries with the real organizer email.
- **Fix: tray app status detection via PID lock file**
  - `catxt_app.py` writes `catxt_app.pid` on startup, deletes on quit.
  - `_tray_app_running()` validates the PID via `tasklist /FI` — replaces the broken
    process-name check that missed `pythonw.exe`-launched instances.
- **Fix: session-expired toast throttled to once per 4 hours** (was every hourly tick)
- **Add: `create_shortcuts.bat`** — creates Desktop + Startup folder shortcuts without PowerShell.
