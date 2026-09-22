# CATXT Sync — Submission Narrative
## Conversational Time Tracking for SAP Employees via Joule

---

## The Problem

Every SAP employee on a customer project logs time in CATXT (CAT2) every day.
The process is manual: navigate to the timesheet, recall which meetings happened,
look up the right WBS codes, enter each entry one by one. It takes 10–15 minutes
daily — not because it is complex, but because it is repetitive friction applied
to an already long workday.

Multiply that across SAP's workforce:

| Scope | Daily cost | Annual cost |
|---|---|---|
| 1 employee | ~12 min/day | ~50 hours/year |
| 1,000 employees | ~200 hours/day | ~50,000 hours/year |
| 50,000 employees | ~10,000 hours/day | ~2.5M hours/year |

Beyond time, manual entry produces errors: wrong WBS codes, missed days,
duplicate entries submitted in frustration. These create downstream corrections
in finance and project reporting.

---

## The Solution

**CATXT Sync** connects Outlook, CATXT, and Joule into a single conversational
workflow. Instead of navigating three systems manually, the employee says one
sentence:

> *"Post today's entries."*

Joule reads the calendar, matches each meeting to the right WBS project using
keyword rules, presents a summary for confirmation, and posts — in under
60 seconds.

### What a typical interaction looks like

| Before CATXT Sync | With CATXT Sync |
|---|---|
| Open browser → navigate to CATXT | "Post today's entries" |
| Recall each meeting from memory | Joule reads calendar automatically |
| Look up WBS code for each project | Auto-matched via keyword rules |
| Enter each entry manually | Confirm once → all entries posted |
| Check for duplicates manually | Duplicate check happens automatically |
| **10–15 minutes** | **Under 60 seconds** |

---

## Innovation & Creativity

Three design decisions make this technically novel:

### 1. No app registration required

Most SAP integrations require an OAuth app registration, IT approval, and weeks
of setup before a tool can access any SAP system. CATXT Sync bypasses this
entirely — it piggybacks on the user's existing SAP SSO session.

The tray app uses Playwright to open Outlook Web App in a headless browser,
intercepts the Microsoft Graph API calendar response, and reuses existing SAP
session cookies to post to the CATXT OData service. **Zero IT involvement.
Works on day one.**

### 2. Local MCP server architecture

Rather than building a cloud service, CATXT Sync runs a lightweight MCP
(Model Context Protocol) server locally on the user's Windows machine. It
starts automatically at logon via Task Scheduler and listens at
`http://127.0.0.1:7432/mcp`.

This means:
- **All data stays on the user's machine** — no cloud intermediary
- **No deployment, no infrastructure, no maintenance**
- **Connects to Joule Work Desktop via the standard MCP connector** — one URL,
  one-time setup

### 3. Conversational configuration

Beyond posting entries, the Joule skill can modify the tool's own configuration
conversationally:

- *"Add 'Keurig planning' as a keyword for the KDP project"* — adds a matching
  rule so future meetings auto-assign
- *"Remove @lincoln.com from the Lincoln Electric email pattern"* — removes a rule
- *"Retry last Tuesday's sync"* — clears the processed-event cache so a failed
  day re-appears in the next sync
- *"Start the tray app"* — launches the application if it isn't running

No settings panel required. No documentation to look up.

---

## Reusability Across SAP

The pattern established here is a **reusable blueprint** for any SAP internal
tool that lacks a Joule integration and cannot wait for official backend
connectivity.

### The pattern

```
Existing SAP SSO session
        ↓
Local MCP server (lightweight Python, starts at logon)
        ↓
Joule Work Desktop (standard MCP connector — one URL)
        ↓
Conversational skill (SKILL.md — defines workflows and tool calls)
```

### Where else this applies

| Use case | What changes |
|---|---|
| Concur expense reports | Replace CATXT OData with Concur API |
| SAP Learning Hub completions | Replace calendar with Learning Hub data |
| Internal IT tickets | Replace posting logic with ServiceNow API |
| Travel booking approvals | Replace CATXT with travel system |

Any internal tool with an HTTP API and SAP SSO can be connected to Joule
using this same architecture — without waiting for official backend integration
to be built and shipped.

---

## Ease of Adoption

### Zero-interaction mode

For users who want completely hands-free posting, CATXT Sync includes a built-in
scheduler. Configure a daily run time in Settings → Schedule and register the task
with one click. At the configured time, the Windows Task Scheduler fires, the tray
app auto-posts every matched entry, and a toast notification confirms the result —
no typing, no confirmation, no user action required.

### For the end user

For interactive use, the daily interaction is one sentence. The skill handles:

- Already-posted entries (skips duplicates automatically)
- Days where 8h is already logged ("You're at 8h for today — looks complete")
- Meetings with no keyword match ("I couldn't match this meeting — which project
  should I use, or skip it?")
- Session expiry (Joule calls `re_authenticate` automatically — silently restores
  the SAP session via SSO in the common case; only prompts the user if the full
  corporate SSO has also expired, which happens at most once a week)

### For setup

1. Download and run `CATXT.exe` — setup wizard runs automatically on first launch
2. Add `http://127.0.0.1:7432/mcp` as a Connector in Joule Work Desktop
3. Install the skill from skills.cloud.sap

No Python, no command line, no IT ticket.

---

## Technical Architecture Summary

```
┌─────────────────────────────────────────────┐
│           Joule Work Desktop                │
│                                             │
│  User: "Post today's entries"               │
│         ↓                                  │
│  catxt-sync skill (SKILL.md)               │
│         ↓  MCP tool calls                  │
│  http://127.0.0.1:7432/mcp                 │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│         CATXT Sync (local)                  │
│                                             │
│  MCP Server (starlette + uvicorn)           │
│  ├── get_existing_entries                   │
│  ├── post_time_entry                        │
│  ├── get_mappings / suggest_mapping         │
│  ├── add_keyword / remove_keyword           │
│  ├── get_staffing_assignments               │
│  ├── get_sync_status                        │
│  ├── clear_sync_history                     │
│  ├── get_tray_status                        │
│  ├── start_tray_app                         │
│  ├── check_session                          │
│  └── re_authenticate                        │
│                                             │
│  Tray App                                   │
│  ├── Outlook calendar (OWA interception)    │
│  └── CATXT OData API (SAP SSO session)     │
└─────────────────────────────────────────────┘
```

---

## Demo Script

### Scene 1 — The problem (30 seconds)
Show a full Outlook calendar day. Cut to the CATXT web interface.
*"This is what every SAP employee on a customer project does every day.
Navigate here, remember what meetings you had, find the WBS codes,
type it all in. It takes 10–15 minutes."*

### Scene 2 — Check status (20 seconds)
Switch to Joule Work Desktop.
Type: **"What have I posted today?"**
Show response: total hours posted, breakdown by project.
*"Joule checks CATXT directly — 2 hours posted, 6 still to go."*

### Scene 3 — Post entries (60 seconds)
Type: **"Post today's remaining entries."**
Show Joule presenting the matched entries as a table:

| Meeting | Project | Hours |
|---|---|---|
| KDP Weekly | Keurig Dr Pepper — TPE | 1.0h |
| Lincoln standup | Lincoln Electric — SCC | 0.5h |
| Team sync | Default Cost Centre (MEET) | 0.5h |

Type: **"Yes, post all of them."**
Show confirmation: *"Posted 3 entries, 4.0h total. You're at 6h for today."*

### Scene 4 — Smart configuration (30 seconds)
Type: **"Add 'Keurig planning' as a keyword for the KDP project."**
Show confirmation: *"Added. Any meeting with 'Keurig planning' in the title
will now auto-match to Keurig Dr Pepper — TPE."*

### Scene 5 — Architecture close (20 seconds)
Show the SAP icon in the Windows system tray.
Show the Joule connector settings with `http://127.0.0.1:7432/mcp`.
*"No cloud. No IT approval. No app registration. It works with the SSO
credentials you already have."*

**Total runtime: ~2.5 minutes**

---

*Submitted for the Joule Work Desktop Skills Contest, September 2026.*
*Repository: https://github.com/iwuchris-sap/catxt-sync*
*skills.cloud.sap: catxt-sync*
