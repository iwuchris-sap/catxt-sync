# CATXT Sync — Setup Guide

**What this does:** connects your Outlook calendar to SAP CATXT (My Timesheet) through Joule Work Desktop. Instead of manually entering time in the CATXT browser app, you tell Joule what you did and it posts the entries for you.

---

## Before you start

You need:
- **Windows 10 or 11**
- **Microsoft Edge** (standard SAP laptop install — already there)
- **Python 3.10+** — download from [python.org](https://www.python.org/downloads/)
  - During install, check **"Add Python to PATH"**
- **Joule Work Desktop** installed and running

You also need to know your **default cost centre** — the SAP cost centre code for your home org (e.g. `0800012345`). Check your payslip or ask your manager/admin.

---

## Step 1 — Extract the zip

Extract `CATXT-Sync-1.1.0.zip` to a **permanent folder**. Do not run it from Downloads or a temp location — the shortcuts and scheduled tasks will point to wherever you extract it.

Suggested location: `C:\Users\<you>\CATXT-Sync\`

---

## Step 2 — Run setup

Open the extracted folder and double-click **`setup.bat`**.

It will:
1. Install Python dependencies (takes ~1 minute)
2. Install the Playwright Edge driver
3. Copy the blank config template
4. Register the MCP server as a Windows logon task
5. Create a Desktop shortcut and Startup folder entry

When it finishes you will see **"Setup complete!"** and a list of next steps.

---

## Step 3 — First launch

Double-click **CATXT Sync** on your Desktop.

A setup wizard appears. Enter your **default cost centre** when prompted.

After the wizard, a **browser window** opens and completes your SAP SSO login automatically using your Edge profile. This happens once — subsequent launches are silent.

Once authenticated, the app syncs your staffing assignments from CATXT and pre-populates your WBS projects with starter keywords automatically.

---

## Step 4 — Add the Joule connector

1. Open Joule Work Desktop
2. Go to **Settings → Extensions** (or Settings → Connectors)
3. Click **Add connector**
4. Enter URL: `http://127.0.0.1:7432/mcp`
5. Name it: `CATXT Sync`
6. Save — it should show as **Connected** (green)

> If it shows as Disconnected, make sure the CATXT Sync tray app is running (SAP icon in your system tray), then try again.

---

## Step 5 — Install the Joule skill

In Joule Work Desktop:
- Drag **`catxt-sync.md`** (from the extracted folder) onto the Skills panel, **or**
- Go to Settings → Skills → Install from file → select `catxt-sync.md`

---

## You're ready

Try it: open a new Joule conversation and type:

> **"Post today's entries"**

Joule will read your calendar, match meetings to your WBS projects, show you a summary, and post on your confirmation.

---

## Day-to-day

- The tray app starts automatically at Windows login
- It syncs entries hourly in the background
- Use Joule for on-demand posting, status checks, and managing keyword rules
- If you close the tray app, the MCP server stays running — Joule can restart it for you
- SAP sessions expire every ~36–48 hours. When they do, you'll get a toast notification — right-click the tray icon → **Re-authenticate** to refresh. Takes about 30 seconds.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Connector shows Disconnected | Make sure the tray app is running (SAP icon in system tray) |
| "Session expired" toast notification | Right-click tray icon → **Re-authenticate** |
| Meeting not matched to a project | Tell Joule: "Add '[keyword]' as a keyword for [project]" |
| setup.bat says Python not found | Re-install Python and check "Add Python to PATH" |
| Shortcuts not created | Run `create_shortcuts.bat` manually |
