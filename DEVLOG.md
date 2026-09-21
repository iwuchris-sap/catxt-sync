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
| `Obart` | `"PR"` for WBS, `"KS"` for cost centre, `"VB"` for Sales Orders |
| `Objnr` | `"PR" + rproj[-8:]` for WBS (e.g. `"PR01386783"`); `"KS0001" + rkostl` for CC; `"VB" + rkdauf + rkdpos` for Sales Orders |
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

### Unreleased (next zip → bump to 1.1.1)
- **Fix: `Tasklevel = "K1"` required for `Tasktype = ICON` CC entries (ZCATSXT-225)**
  - Fixed in `catxt_core.py` and `catxt_sync.py`.
- **Fix: `get_mappings` and `suggest_mapping` descriptions updated to surface `auto_mappings` CC entries**
  - Joule now knows to look at both `mappings` and `auto_mappings` when resolving project labels.
- **Feat: starter keywords auto-generated from customer name on first staffing sync**
  - New `_starter_keywords()` in `catxt_core.py` — new users get working keyword matching immediately.
- **Feat: Re-authenticate tray menu item**
  - Refreshes SAP session without triggering a calendar sync or review dialog.
- **Fix: `setup.bat` now installs `requirements_mcp.txt`** (starlette + uvicorn were missing)
- **Fix: blank PERNR/KOSTL fallbacks** in `catxt_core.py` — prevents posting to wrong timesheet if auto-detection fails.
- **Docs: customer names and internal identifiers removed from public docs**
- **Docs: SETUP-GUIDE.md added to zip**

> **Release checklist (every version bump):**
> 1. Bump `APP_VERSION` in `catxt_core.py` (single source of truth)
> 2. Update `version.json` — `version` + `changes` summary
> 3. **Add a DEVLOG entry here** with date, version, and technical detail for every fix/feature
> 4. Run `python package.py` to build the zip
> 5. `git add catxt_core.py version.json DEVLOG.md && git commit -m "vX.Y.Z: ..." && git push`

---

### 2026-09-18 — v1.2.1
- **Fix: duplicate detection false positives in `post_time_entry`**
  - CATXT backend echoes the first-created `ActivityHeader` (same `Taskcounter`) for all
    subsequent POSTs in the same session. The old duplicate check saw a non-zero `Taskcounter`
    and incorrectly concluded the entry already existed.
  - Fix: the returned entry's description, date, and hours are now verified against what was
    sent before flagging as duplicate. A mismatched echo is logged as debug and treated as success.
- **Add: MCP server file logging (`catxt_mcp.log`)**
  - The scheduled-task process (`pythonw.exe`) was failing silently with no output.
  - `catxt_mcp_server.py` now writes a rotating log file to the same folder when running standalone.
  - `catxt_mcp.log` added to `.gitignore`.
- **Add: Watchdog auto-restart launcher (`catxt_mcp_launcher.py`)**
  - New launcher wraps `catxt_mcp_server.py` and restarts it on exit with exponential back-off
    (10 s → 20 s → … → 60 s cap). The scheduled task now runs the launcher instead of the server.
  - `setup.bat` updated to register the launcher.
- **Fix: tray status detection (`get_tray_status` / `start_tray_app`)**
  - Replaced unreliable `tasklist` string match (always returned True) with a direct `msvcrt`
    byte-range lock probe on `catxt_app.pid`. Lock held → app running; lock acquirable → not running.
    No locale-dependent string parsing, no stale PID file false positives.

### 2026-09-18 — v1.2.2
- **Add: silent SSO re-auth on cookie expiry**
  - All sync paths now follow a three-step cascade:
    1. Valid cookies → proceed normally.
    2. Cookies expired but corporate SSO still active → silent headless re-auth using the live
       Edge profile (completely invisible, no notification required).
    3. SAP SSO itself expired → notification asking for manual re-authentication.
  - Implemented via `_get_session_or_notify` pattern across `catxt_app.py` and
    `catxt_mcp_server.py`. Previously every cookie expiry triggered a manual re-auth notification.

### 2026-09-18 — v1.2.3
- **Fix: headless auth when Edge profile is locked (temp profile copy)**
  - v1.2.2 headless re-auth always failed while Edge was open because Playwright tried to open
    the live Edge profile directory, which is exclusively locked by the running browser.
  - Fix: `authenticate_via_browser()` in `catxt_core.py` now attempts the live profile first,
    and if that fails due to a lock, copies only `Cookies` + `Local State` to a temp directory
    and launches Edge headlessly from the copy.
  - The temp dir is a few MB, lives for seconds, and is always cleaned up on success or failure.
  - Result: silent re-auth now succeeds in normal use (Edge open, corporate SSO active).
    The "Session Expired" notification only appears when SAP SSO itself has genuinely expired.

### 2026-09-21 — v1.2.13
- **Fix: scheduled task now triggers sync when tray app is already running**
  - The Windows Task Scheduler job launches `catxt_app.py --scheduled` daily.
    When the tray is already running, the instance guard was exiting immediately
    without doing anything — so the scheduled sync never fired on any normal
    workday where the tray had been started at login.
  - Fix (two parts):
    1. **Instance guard** (`__main__`): if `--scheduled` is in `sys.argv` and
       the running-instance lock is held, write `.sync_trigger` (a zero-byte
       file in the app directory) before exiting, instead of just exiting silently.
    2. **Trigger poll** (`_trigger_check_tick`): a new 60-second tick checks
       for `.sync_trigger`, deletes it, and calls `_start_scheduled_sync()`.
  - Result: within ≤ 60 seconds of the Task Scheduler firing, the running tray
    auto-posts all matched entries — no user action required.

### 2026-09-21 — v1.2.12
- **Fix: add missing `_get_session_or_notify` method to `CatxtApp`**
  - `_background_staffing_worker` (hourly tick) and `_sync_worker` both called
    `self._get_session_or_notify()` but the method was never defined on the class.
  - Result: background staffing check raised `AttributeError` every hour since v1.2.2,
    silently swallowed by the `except Exception` in each worker.
  - Fix: added `_get_session_or_notify(self)` — calls `core.get_or_refresh_session()`,
    returns the session on success, or `None` after sending a throttled
    "Session Expired" toast (respects the existing 4-hour cooldown).

### 2026-09-21 — v1.2.11
- **Fix: revert content-differs silent-drop detection (`post_activity` / `post_time_entry`)**
  - v1.2.7 introduced a "content differs → SILENT DROP → return False" branch: when
    `Activity.results[0]` echoed a pre-existing `Taskcounter` but with different `Ltxa1`/date/qty,
    the code concluded the new entry was not created and returned failure.
  - Production confirmation 2026-09-21: TechEd (0.5 h) and NDL Team Meeting (0.5 h) on Sep 17
    both appeared in CATXT even though `results[0]` was TC=0313955242 (a pre-existing entry).
    The backend creates the new entry but echoes an old one in `results[0]` of the response body.
  - Revert: the content-differs branch now logs a `WARNING` ("entry IS created") and returns `True`.
    Only the true-duplicate path (same `Ltxa1` + date + qty) still returns `False`.
  - The empty-`Activity.results` check (genuine silent drop) is unchanged and still returns `False`.

### 2026-09-21 — v1.2.10
- **Add: `re_authenticate` MCP tool — full automatic session recovery**
  - Two-step approach: (1) silent headless SSO re-auth via live Edge profile
    (succeeds invisibly when corporate SSO is still active); (2) if that fails,
    opens a visible browser window for full SAP login and waits up to 120 s.
  - Returns `authenticated: true` on success so the skill can immediately
    resume posting without any further user action.
  - Returns `authenticated: false` with `requires_manual_auth: true` only if
    both steps fail — in that case the user still needs the tray icon.
  - Covers the common case (Azure SSO still live, only SAP cookies expired)
    completely automatically. Full SAP SSO expiry (typically once per week)
    opens a browser window — the user logs in once and everything resumes.
- **Fix: SKILL.md — `re_authenticate` used in pre-flight and mid-batch recovery**
  - Pre-flight: if `check_session()` returns expired, call `re_authenticate()`
    automatically before telling the user anything. If it succeeds silently,
    the user never even knows the session was expired.
  - Mid-batch: if `session_expired: true` is returned mid-batch, call
    `re_authenticate()` automatically and resume posting if successful.

### 2026-09-21 — v1.2.9
- **Add: `check_session` MCP tool — pre-flight SAP session validation**
  - New tool calls the Userinfo endpoint to confirm the SAP session is live before
    a batch of posts begins. Returns `valid=true` with pernr/kostl/name on success,
    or `valid=false` with `session_expired=true` and an `action` message on failure.
  - Previously, session expiry was only discovered when the first post failed
    mid-batch, causing Joule to lose track of the remaining queued entries.
- **Fix: structured `session_expired` errors in `post_time_entry`**
  - Previously, if `_get_session()` raised a RuntimeError (expired session), the
    error propagated as a raw exception string with no machine-readable flag.
  - Now returns `{"success": false, "session_expired": true, "action": "..."}` so
    the skill can detect the specific failure mode and handle it gracefully.
  - Same structured response returned if CSRF token fetch fails (also session-related).
- **Fix: SKILL.md — session pre-check and mid-batch recovery**
  - Skill now calls `check_session()` as step 1 of every posting workflow. If the
    session is expired before the batch starts, the user is told to re-authenticate
    immediately — no posts are attempted.
  - If session expires mid-batch, skill stops, shows exactly what succeeded and what
    still needs posting, and resumes from where it left off once the user re-auths.

### 2026-09-18 — v1.2.8
- **Fix: Sales Order entries silently dropped (`Obart` was `"SD"`, must be `"VB"`)**
  - CATXT backend silently drops Sales Order (SD) entries when `Obart = "SD"`. The correct
    SAP object type for a Vertriebsbeleg (Sales Document) is `"VB"`, with
    `Objnr = "VB" + rkdauf (10-char) + rkdpos (6-char)`.
  - Confirmed via HAR capture of a successful manual Fiori UI post against a Sales Order entry.
  - Fixed in `catxt_core.py` — `obart` and `objnr` construction for the `is_sd` branch.
  - This bug would have silently dropped every Sales Order time entry without any error message.

### 2026-09-18 — v1.2.7
- **Fix: CC entries silently dropped (`Taskcomponent` was `"WORKHRS"`, must be `"WORKSTAT"`)**
  - CATXT backend silently drops cost-centre entries (CC) when `Taskcomponent = "WORKHRS"`.
    The Fiori UI sends `"WORKSTAT"` for all CC entries (MEET, EDUC, ICON).
    WBS and SD entries correctly use `"WORKHRS"`.
  - Root cause: a previous code change overrode the original correct `"WORKSTAT"` to `"WORKHRS"`
    universally, citing "the UI always selects Work Hrs" — which was incorrect per HAR evidence.
  - ICON (`tasktype == "ICON"`) retains `"WORKHRS"` — it requires WORKHRS and silently rejects
    WORKSTAT (separate backend rule, unchanged from ZCATSXT-225 fix).
  - Fix in `catxt_core.py`: `taskcomponent` default now branches on entry type.
  - Confirmed via HAR capture across all 9 configured entry types (MEET/×4, EDUC/×2, ICON/×3, SD/×1).
- **Fix: false-positive success when backend echoes pre-existing `Taskcounter`**
  - When the backend silently drops a new entry it returns the most recently-created entry
    in `Activity.results` instead of empty results. The previous code treated a mismatched echo
    (different `Ltxa1` or `Workdate` to what was sent) as a successful new post.
  - Fix: mismatched echo is now correctly logged as `✗ SILENT DROP` and returns `False`.

### 2026-09-18 — v1.2.6
- **Fix: `add_keyword` NameError + copy bug for `auto_mapping` entries**
  - `_tool_add_keyword` raised a `NameError` when adding keywords to CC-only (`auto_mapping`)
    entries because the variable holding the matched mapping was out of scope.
  - Secondary bug: new keyword entries shared a reference to the original mapping dict rather
    than a copy, so mutations to one entry affected others.
  - Fix: scope corrected; new entries are now built from `dict(m)` (shallow copy).

### 2026-09-18 — v1.2.5
- **Fix: LR(093) Personnel number locked — serialise all `post_time_entry` calls**
  - CATXT backend acquires an exclusive record lock per PERNR during each batch write.
    Concurrent POSTs to the same PERNR are rejected with `LR(093): Personnel number locked`.
  - Fix: `_post_lock` (`asyncio.Lock`) added to `catxt_mcp_server.py`. All `post_time_entry`
    calls acquire the lock before posting and release it after a `_POST_SLEEP_S = 1.5` s
    sleep, giving the backend time to commit and release its own record lock.
  - `_SERIALISED_TOOLS` set documents which tools hold the lock.
- **Fix: remove per-entry Userinfo fetch from `post_time_entry`**
  - Each call was triggering a `GET /Userinfo` + `HEAD /$batch` round-trip before posting,
    adding ~1 s of latency per entry and causing unnecessary session churn.
  - PERNR and KOSTL are now loaded from the config cache (written by `detect_pernr()` at
    tray-app startup) rather than fetched fresh on every post.

### 2026-09-18 — v1.2.4
- **Fix: Playwright Sync API conflict in MCP server (`catxt_mcp_server.py`)**
  - **Root cause:** `_mcp_endpoint` is an `async` Starlette handler running inside uvicorn's
    asyncio event loop. Several tool functions call `core.get_or_refresh_session(headless_only=True)`
    which invokes Playwright's synchronous API (`sync_playwright()`). Playwright's sync API starts
    its own event loop internally, but Python raises `"Playwright Sync API inside the asyncio loop"`
    when a running loop already exists — causing all tool calls that touch Playwright to crash.
  - **Fix:** Added `import asyncio` and changed the tool dispatch on line ~1059 from:
    ```python
    result_text = _TOOLS[tool_name]["fn"](arguments)
    ```
    to:
    ```python
    result_text = await asyncio.to_thread(_TOOLS[tool_name]["fn"], arguments)
    ```
  - `asyncio.to_thread()` runs the synchronous tool function in a thread-pool thread where no
    asyncio event loop is running. Playwright's sync API can start its own loop there without
    any conflict. The result is awaited back into the event loop as normal.
  - **No changes to `catxt_core.py`** — the fix is entirely in the MCP server dispatch layer.
  - Requires Python 3.9+ (already a project requirement).
