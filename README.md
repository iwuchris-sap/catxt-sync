# CATXT Sync — Joule Skill & MCP Server

**Conversational SAP time tracking for Joule Work Desktop.**

Stop manually navigating to CATXT every day. Tell Joule what you did, and it posts your time entries automatically — matching your calendar meetings to WBS projects, checking for duplicates, and confirming before it touches anything.

---

## What it does

CATXT Sync connects your Outlook calendar to SAP CATXT (CAT2 / My Timesheet) through a local MCP server that Joule calls conversationally.

**Natural language time tracking:**
> *"Post today's entries"* → Joule reads your calendar, matches each meeting to a WBS project, shows you a summary, and posts on confirmation.

> *"What have I posted this week?"* → Breakdown by project with total hours and any gaps.

> *"Add 'Keurig planning' as a keyword for the KDP project"* → Keyword rule saved immediately, takes effect on the next sync.

> *"The tray app isn't running — start it"* → Joule launches it for you.

### Full capability list

| Category | What you can say |
|---|---|
| **Post entries** | "Post today's entries", "log my hours for Monday" |
| **Check status** | "What have I posted today?", "how many hours am I missing this week?" |
| **Sync status** | "Which days haven't been synced?", "has last Tuesday been processed?" |
| **Project rules** | "What keywords do I have for Lincoln?", "which project does 'Sauder review' map to?" |
| **Manage keywords** | "Add 'spin master kickoff' as a keyword for Spin Master", "remove @oldclient.com from Solventum" |
| **Retry failed days** | "Last Wednesday's sync failed — retry it" |
| **Tray app control** | "Is CATXT running?", "start the tray app" |

---

## How it works

### Architecture

```
Joule Work Desktop
       │  natural language
       ▼
  catxt-sync skill  ──────────►  MCP Server (localhost:7432)
                                        │
                          ┌─────────────┼─────────────┐
                          │             │             │
                    Outlook/OWA    config.json    CATXT OData
                    (calendar)    (WBS rules)    (time entries)
```

**Key design decisions:**

- **No app registration required.** The MCP server piggybacks on your existing SAP SSO session via cookie reuse. Zero IT approval needed — works on day one.
- **Playwright-based calendar interception.** Calendar events are read by intercepting the OWA API response directly in Edge, without needing Graph API permissions.
- **Local-first.** The server runs on `127.0.0.1`. No data leaves your machine. No cloud middleware.
- **Standalone MCP server.** The MCP server starts at Windows logon independently of the tray app GUI, so Joule can reach it (and start the tray app) even when the GUI is closed.

### MCP Tools (11 total)

| Tool | Type | Purpose |
|---|---|---|
| `get_mappings` | Read | All configured WBS project rules + excluded keywords list |
| `suggest_mapping` | Read | Match a meeting subject/email to a project; returns `excluded: true` for events that should never be posted |
| `get_existing_entries` | Read | What's already posted in CATXT for a date |
| `get_staffing_assignments` | Read | Live project list from CATXT Staffing API |
| `get_sync_status` | Read | Which days have been processed |
| `get_tray_status` | Read | Whether the tray app is running |
| `post_time_entry` | Write | Post a single time entry to CATXT; accepts optional `calendar_event_id` to mark the event as processed |
| `add_keyword` | Write | Add a keyword/email rule to a project mapping |
| `remove_keyword` | Write | Remove a keyword/email rule |
| `clear_sync_history` | Write | Clear processed-event cache to retry a day |
| `start_tray_app` | Action | Launch the tray app if not running |

---

## Requirements

- Windows 10/11
- Microsoft Edge (for Outlook calendar access)
- SAP CATXT / My Timesheet access (standard SAP employee entitlement)
- [Joule Work Desktop](https://www.sap.com/products/artificial-intelligence/joule.html)

---

## Setup

### Step 1 — Install the tray app

**Option A: Python (development)**
```
cd windows_app
setup.bat
```
This installs dependencies, registers the MCP server as a Windows logon task, and creates a Desktop shortcut.

**Option B: Compiled exe** *(recommended for end users)*
Download `CATXT.zip` from [Releases](../../releases), extract, and run `CATXT.exe`. The setup wizard runs on first launch.

### Step 2 — Add the MCP Connector in Joule

1. Open Joule Work Desktop → Settings → Connectors
2. Add connector URL: `http://127.0.0.1:7432/mcp`
3. Name it `CATXT Sync`

### Step 3 — Install the skill

**From skills.cloud.sap:**
```
npx skills add iwuchris-sap/catxt-sync
```

**Or install from file:**
In Joule Work Desktop, drag and drop `catxt-sync.md` onto the Skills panel, or use Settings → Skills → Install from file.

### Step 4 — Authenticate

On first use, Joule will prompt you to log in. A browser window opens, completes SSO automatically using your Edge profile, and closes. Subsequent syncs are silent.

---

## Configuration

WBS project mappings are stored in `config.json` next to the tray app. You can manage them conversationally through Joule (add/remove keywords) or edit the file directly.

**`_excluded_keywords`** — calendar events whose subject contains any of these phrases (case-insensitive) are silently skipped by both the tray app and the Joule skill. Add entries like `"vacation"`, `"sick day"`, or `"administrative tasks"` to keep non-billable calendar blocks out of CATXT automatically.

```json
{
  "wbs_mappings": [
    {
      "label": "Keurig Dr Pepper — TPE",
      "wbs": "CPS.40001234.00001",
      "rproj": "000000000000000001234567",
      "keywords": ["keurig", "kdp"],
      "email_patterns": ["@keurigdrpepper.com"],
      "tasktype": "CFPP",
      "tasklevel": "G3"
    }
  ],
  "default_mapping": {
    "label": "Default Cost Centre",
    "rkostl": "0800012345"
  },
  "_excluded_keywords": [
    "administrative tasks",
    "vacation",
    "sick day",
    "catxt",
    "time entry"
  ]
}
```

---

## Reusability

The **local MCP server pattern** used here — a lightweight Python server that piggybacks on an existing SAP SSO session — is applicable to any SAP internal tool that lacks a public API or requires IT-approved app registrations. Potential adaptations:

- **Concur / Travel & Expense** — post expense reports from Joule
- **SAP Learning Hub** — check required training completions
- **Internal ticketing / SNOW** — create and update tickets conversationally

The `SKILL.md` + MCP server pattern is the reusable building block. The CATXT-specific logic is isolated in `catxt_core.py`.

---

## Privacy & Security

- All traffic stays on `127.0.0.1` — nothing is sent to external servers
- Session cookies are stored locally in `.auth_cookies.json` (gitignored)
- No SAP credentials are stored — authentication is delegated entirely to your existing Edge SSO session

---

## License

MIT
