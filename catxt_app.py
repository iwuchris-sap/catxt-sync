"""
catxt_app.py — CATXT Sync Windows Tray Application
====================================================
Entry point for the packaged .exe.

Architecture
------------
  Main thread  : hidden tkinter root + gui_queue processor (all GUI windows
                 must be created here — tkinter is not thread-safe).
  Tray thread  : pystray icon (run_detached).
  Worker thread: one background sync thread at a time; communicates back to
                 the main thread via gui_queue for review dialogs and toasts.

Tray menu
---------
  Sync Today          → manual sync for today (opens review dialog)
  Sync Date...        → date-picker then manual sync
  Preview Today       → show review dialog in read-only mode, no posting
  ─────────────────
  Sync Staffing       → update project list from CATXT staffing endpoint
  ─────────────────
  Settings            → open SettingsDialog
  View Log            → open catxt_sync.log in Notepad
  ─────────────────
  Quit
"""

import logging
import os
import queue
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import ttk, simpledialog

# ── Logging ───────────────────────────────────────────────────────────────────
from catxt_core import LOG_FILE

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── App imports (after logging is set up) ─────────────────────────────────────
import catxt_core as core
from catxt_notify  import notify
from catxt_review  import ReviewDialog
from catxt_settings import SettingsDialog
from catxt_wizard  import SetupWizard

try:
    import pystray
    from pystray import Icon, Menu, MenuItem
    from PIL import Image
    _PYSTRAY_OK = True
except ImportError:
    _PYSTRAY_OK = False
    log.error("pystray or Pillow not installed — tray icon unavailable.")


# ── Icon paths ─────────────────────────────────────────────────────────────────
_ASSETS = Path(__file__).parent / "assets"
_ICO_PATH = _ASSETS / "catxt.ico"


def _load_pil_image(path: Path) -> "Image.Image":
    img = Image.open(path)
    img = img.convert("RGBA")
    # Ensure we have a 64×64 version for the tray
    return img.resize((64, 64), Image.LANCZOS)


def _make_syncing_image(base: "Image.Image") -> "Image.Image":
    """Tint the icon orange while sync is in progress."""
    from PIL import ImageEnhance
    grey = base.convert("L").convert("RGBA")
    tinted = Image.new("RGBA", base.size, (240, 171, 0, 180))
    return Image.alpha_composite(grey, tinted)


# ══════════════════════════════════════════════════════════════════════════════
# Main app class
# ══════════════════════════════════════════════════════════════════════════════

class CatxtApp:

    def __init__(self):
        self._gui_queue: queue.Queue = queue.Queue()
        self._sync_lock  = threading.Lock()   # only one sync at a time
        self._icon: "Icon | None" = None
        self._root: tk.Tk | None  = None
        self._icon_idle: "Image.Image | None"    = None
        self._icon_syncing: "Image.Image | None" = None
        # Timestamp of the last "session expired" toast — used to suppress
        # repeated hourly notifications when the user hasn't re-authenticated.
        # Reset to None whenever a background worker gets a live session.
        self._last_session_expiry_notify: "datetime | None" = None

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self):
        # Hidden tkinter root — all child windows use this as parent
        self._root = tk.Tk()
        self._root.withdraw()
        self._root.title("CATXT Sync")
        try:
            self._root.iconbitmap(str(_ICO_PATH))
        except Exception:
            pass

        # Check first-run
        config = core.load_config()
        if not config.get("_setup_done"):
            self._gui_queue.put(self._run_wizard)

        # Build and launch tray icon
        if _PYSTRAY_OK and _ICO_PATH.exists():
            self._icon_idle    = _load_pil_image(_ICO_PATH)
            self._icon_syncing = _make_syncing_image(self._icon_idle)
            self._icon = Icon(
                "CATXT Sync",
                self._icon_idle,
                "CATXT Sync",
                menu=self._build_menu(),
            )
            tray_thread = threading.Thread(target=self._icon.run, daemon=True)
            tray_thread.start()
        else:
            log.warning("Tray icon unavailable — running without system tray.")

        # Queue processor (runs on main thread every 100 ms)
        self._root.after(100, self._process_queue)

        # Start the MCP Connector server so Joule can call CATXT tools.
        # Runs on a background daemon thread — silently skipped if the
        # mcp package is not installed.
        self._start_mcp_server()

        log.info("CATXT Sync tray app started.")

        # Write a PID lock file so the MCP server can reliably detect whether
        # the tray app is running — process-name checks are unreliable when
        # launched via pythonw.exe (the name in tasklist is just "pythonw.exe").
        _PID_FILE = Path(__file__).parent / "catxt_app.pid"
        try:
            _PID_FILE.write_text(str(os.getpid()))
        except Exception:
            pass

        # If launched by the Windows Task Scheduler (--scheduled flag), trigger
        # the smart scheduled sync automatically after the tray is ready.
        if "--scheduled" in sys.argv:
            log.info("Launched with --scheduled flag — triggering scheduled sync.")
            self._root.after(500, self._start_scheduled_sync)

        # In-process background sync timer — fires every 60 minutes while the
        # tray app is running, so syncs happen automatically even if the Windows
        # Task Scheduler only fires once a day (or not at all).
        # First tick after 2 minutes to let auth and startup settle.
        self._root.after(2 * 60 * 1000, self._auto_sync_tick)

        self._root.mainloop()

    # ── Tray menu ─────────────────────────────────────────────────────────────

    def _build_menu(self) -> "Menu":
        return Menu(
            MenuItem("Sync Today",          self._tray_sync_today),
            MenuItem("Sync Date...",        self._tray_sync_date),
            MenuItem("Preview Today",       self._tray_preview),
            MenuItem("Add Manual Entry...", self._tray_add_manual_entry),
            Menu.SEPARATOR,
            MenuItem("Sync Staffing",   self._tray_sync_staffing),
            MenuItem("Sync Metadata",   self._tray_sync_metadata),
            Menu.SEPARATOR,
            MenuItem("Settings",              self._tray_settings),
            MenuItem("View Log",              self._tray_view_log),
            MenuItem("Clear Sync History...", self._tray_clear_history),
            Menu.SEPARATOR,
            MenuItem("Quit",                  self._tray_quit),
        )

    # ── GUI queue ─────────────────────────────────────────────────────────────

    def _process_queue(self):
        try:
            while True:
                task = self._gui_queue.get_nowait()
                try:
                    task()
                except Exception as _task_exc:
                    log.exception(f"GUI queue task error: {_task_exc}")
        except queue.Empty:
            pass
        self._root.after(100, self._process_queue)

    # ── First-run wizard ──────────────────────────────────────────────────────

    def _run_wizard(self):
        wiz = SetupWizard(self._root)
        if not wiz.completed:
            log.info("Setup wizard cancelled — using defaults.")
        if wiz.run_staffing_sync:
            self._start_staffing_sync()

    # ── Tray callbacks (called from tray thread → dispatch to gui_queue) ──────

    def _tray_sync_today(self, _icon=None, _item=None):
        self._gui_queue.put(lambda: self._start_sync(date.today(), manual=True))

    def _tray_sync_date(self, _icon=None, _item=None):
        self._gui_queue.put(self._pick_date_and_sync)

    def _tray_preview(self, _icon=None, _item=None):
        self._gui_queue.put(lambda: self._start_sync(date.today(), preview_only=True))

    def _tray_add_manual_entry(self, _icon=None, _item=None):
        self._gui_queue.put(self._open_manual_entry)

    def _tray_sync_staffing(self, _icon=None, _item=None):
        self._gui_queue.put(self._start_staffing_sync)

    def _tray_sync_metadata(self, _icon=None, _item=None):
        self._gui_queue.put(self._start_metadata_sync)

    def _tray_settings(self, _icon=None, _item=None):
        self._gui_queue.put(self._open_settings)

    def _tray_view_log(self, _icon=None, _item=None):
        self._gui_queue.put(self._open_log)

    def _tray_clear_history(self, _icon=None, _item=None):
        self._gui_queue.put(self._open_clear_history)

    def _tray_quit(self, _icon=None, _item=None):
        self._gui_queue.put(self._quit)

    # ── Clear processed-events cache ─────────────────────────────────────────

    def _open_clear_history(self):
        """GUI thread: pick a date range then wipe processed-event IDs for those days."""
        from tkinter import messagebox as _mb
        dlg = _DateRangeDialog(self._root, title="Clear Sync History — Choose Range")
        if dlg.result is None:
            return
        if isinstance(dlg.result, tuple):
            start, end = dlg.result
        else:
            start = end = dlg.result

        confirmed = _mb.askyesno(
            "Clear Sync History",
            f"Remove the processed-event log for:\n"
            f"  {start.strftime('%a %d %b %Y')}"
            + (f" – {end.strftime('%a %d %b %Y')}" if end != start else "")
            + "\n\nEvents that were already posted to CATXT will be re-validated and "
            "skipped automatically. Events that failed (e.g. OPEN entries) will "
            "re-appear in the review dialog so you can retry them.\n\nContinue?",
            parent=self._root,
        )
        if not confirmed:
            return

        n = core.clear_processed_for_range(start, end)
        notify(
            "CATXT — History Cleared",
            f"Cleared {n} day(s) ({start.strftime('%d %b')} – {end.strftime('%d %b %Y')}).\n"
            "Run Sync to re-process those days.",
        )

    # ── Standalone manual entry ───────────────────────────────────────────────

    def _open_manual_entry(self):
        """GUI thread: open the manual entry dialog and kick off the post worker."""
        from catxt_review import _ManualEntryDialog
        config        = core.load_config()
        default_m     = config.get("default_mapping") or {}
        default_label = default_m.get("label", "Default Cost Centre")
        all_labels    = [default_label] + [
            m["label"] for m in config.get("wbs_mappings", []) if m.get("label")
        ]
        dlg = _ManualEntryDialog(
            self._root,
            all_labels=all_labels,
            default_label=default_label,
            config=config,
            target_date=None,   # standalone mode — shows the Date field
        )
        if dlg.result is None:
            return

        # ── Resolve business-day list from target_dates (comma-separated input) ─
        raw_dates = dlg.result.get("target_dates") or [dlg.result.get("target_date")]
        dates = [d for d in raw_dates if d is not None and d.weekday() < 5]

        if not dates:
            notify("CATXT", "No business days in the selected range.")
            return

        if len(dates) > 1:
            from tkinter import messagebox as _mb
            confirmed = _mb.askyesno(
                "Confirm Multi-Day Post",
                f"Post \"{dlg.result['desc']}\" to {len(dates)} business days:\n"
                f"  {dates[0].strftime('%a %d %b')} – {dates[-1].strftime('%a %d %b')}\n\n"
                "Continue?",
                parent=self._root,
            )
            if not confirmed:
                return

        if not self._sync_lock.acquire(blocking=False):
            notify("CATXT Sync", "A sync is already in progress — try again shortly.")
            return
        threading.Thread(
            target=self._manual_entry_worker,
            args=(dlg.result, dates),
            daemon=True,
        ).start()

    def _manual_entry_worker(self, result: dict, dates: list):
        """Background worker: POST a manual entry to one or more business days."""
        import copy, time as _t
        try:
            self._set_icon_syncing(True)
            log.info(
                f"Manual entry (standalone): {result['desc']} | "
                f"tasktype={result['tasktype']} zzsubtype={result['zzsubtype']} | "
                f"{result['hours']}h across {len(dates)} date(s)"
            )

            session = core.get_or_refresh_session()
            if session is None:
                self._gui_queue.put(lambda: notify(
                    "CATXT — Auth Failed",
                    "Could not authenticate. Check the log for details.",
                ))
                return

            core.detect_pernr(session)
            csrf = core.get_csrf_token(session)

            wbs_label  = result["mapping"].get("label", "Default Cost Centre")
            ok_count   = 0
            fail_count = 0

            for target_date in dates:
                event = {
                    "id":             f"manual-{int(_t.time() * 1000)}-{target_date.isoformat()}",
                    "subject":        result["desc"],
                    "start":          target_date.strftime("%Y-%m-%dT09:00:00"),
                    "duration_hours": result["hours"],
                    "organiser_email": "",
                    "organised_by_me": True,
                }
                mapping = copy.copy(result["mapping"])
                mapping["tasktype"]        = result["tasktype"]
                mapping["zzsubtype"]       = result["zzsubtype"]
                mapping["_ltxa1_override"] = result["desc"]

                if core.post_activity(session, csrf, event, mapping, target_date):
                    # Verify the entry actually landed (CATXT can return 201 silently)
                    posted_text = result["desc"][:40].strip().lower()
                    try:
                        verify    = core.get_existing_entries(session, target_date, csrf_token=csrf)
                        verify_lc = {e.get("Ltxa1", "").strip().lower() for e in verify}
                        if posted_text in verify_lc:
                            ok_count += 1
                        else:
                            log.error(
                                f"  ✗  Manual entry '{result['desc'][:40]}' for {target_date}"
                                f" — NOT found in CATXT after posting (silently dropped)."
                            )
                            fail_count += 1
                    except Exception:
                        ok_count += 1   # can't verify — assume success
                else:
                    fail_count += 1

            if len(dates) == 1:
                if ok_count:
                    msg = f"{result['desc']}  ({result['hours']:.2f}h)  →  {wbs_label}"
                    self._gui_queue.put(lambda: notify("CATXT — Entry Posted", msg))
                else:
                    desc = result["desc"]
                    self._gui_queue.put(lambda: notify(
                        "CATXT — Post Failed",
                        f"Could not post \"{desc}\". Check the log.",
                    ))
            else:
                parts = [f"{ok_count} posted"]
                if fail_count:
                    parts.append(f"{fail_count} failed — see log")
                msg = ", ".join(parts) + f"  ({len(dates)} days  ·  {wbs_label})"
                title = "CATXT — Entries Posted" if not fail_count else "CATXT — Partial Failure"
                self._gui_queue.put(lambda: notify(title, msg))
        except Exception as exc:
            log.exception(f"Manual entry worker error: {exc}")
            self._gui_queue.put(lambda: notify(
                "CATXT — Error", "An unexpected error occurred. Check the log.",
            ))
        finally:
            self._sync_lock.release()
            self._set_icon_syncing(False)

    # ── Date picker ───────────────────────────────────────────────────────────

    def _pick_date_and_sync(self):
        dlg = _DateRangeDialog(self._root)
        if dlg.result is None:
            return
        if isinstance(dlg.result, tuple):
            # Date range — build list of business days then run range worker
            dates = core.business_days(dlg.result[0], dlg.result[1])
            if not dates:
                notify("CATXT Sync", "No business days in that range.")
                return
            self._start_sync_range(dates, manual=True)
        else:
            self._start_sync(dlg.result, manual=True)

    # ── Range sync ────────────────────────────────────────────────────────────

    def _start_sync_range(self, dates: list, manual: bool = True):
        if not self._sync_lock.acquire(blocking=False):
            notify("CATXT Sync", "A sync is already in progress.")
            return
        t = threading.Thread(
            target=self._sync_range_worker,
            args=(dates, manual),
            daemon=True,
        )
        t.start()

    def _sync_range_worker(self, dates: list, manual: bool):
        """Background worker for multi-day (range) syncs."""
        try:
            self._set_icon_syncing(True)
            n = len(dates)
            log.info(f"Range sync: {n} business day(s)  "
                     f"{dates[0]} → {dates[-1]}")

            # ── Auth once ─────────────────────────────────────────────────────
            session = core.get_or_refresh_session()
            if session is None:
                self._gui_queue.put(lambda: notify(
                    "CATXT Sync — Auth Failed",
                    "Could not authenticate. Check the log.",
                ))
                return

            core.detect_pernr(session)
            config    = core.load_config()
            processed = core.load_processed()
            csrf      = core.get_csrf_token(session)
            if csrf:
                core.sync_staffing(session, config, csrf)
                core.sync_catxt_metadata(session, config)
                config = core.load_config()

            # ── Fetch ALL calendar data in ONE browser session ─────────────────
            log.info(f"Fetching calendar for {n} day(s) in one browser session...")
            events_by_date = core.get_events_for_date_range(dates)

            total_ok = total_skipped = total_failed = 0
            total_failed_names: list = []

            if manual:
                # ── Manual mode: collect all days then show ONE review dialog ─────
                all_days = []
                for i, target_date in enumerate(dates, 1):
                    log.info(f"Range sync: preparing day {i}/{n} — {target_date}")
                    mapped, unmapped = core.prepare_day(
                        target_date, config, processed,
                        prefetched_events=events_by_date.get(target_date, []),
                    )
                    if not mapped and not unmapped:
                        log.info(f"  No new events for {target_date} — still shown in review.")
                    all_days.append((target_date, mapped, unmapped))

                if all_days:
                    new_event_days = sum(1 for _, m, u in all_days if m or u)

                    # Pre-fetch already-posted hours (needed for skip check + dialog)
                    try:
                        _range_dates  = [d for d, _, _ in all_days]
                        _existing_hrs = core.get_existing_hours_per_day(session, _range_dates)
                        log.debug(f"Existing hours pre-fetched: {_existing_hrs}")
                    except Exception as _exc:
                        log.warning(f"Could not pre-fetch existing hours: {_exc}")
                        _existing_hrs = {}

                    # Skip dialog only when nothing new AND every day already at target
                    _daily_target = float(config.get("daily_hours_target", 8.0))
                    _any_day_short = any(
                        _existing_hrs.get(d.strftime("%Y%m%d"), 0.0) < _daily_target - 0.05
                        for d, _, _ in all_days
                    )
                    if new_event_days == 0 and not _any_day_short:
                        log.info(
                            f"Range sync: nothing new and all days complete "
                            f"for {dates[0]} – {dates[-1]}."
                        )
                        self._gui_queue.put(lambda: notify(
                            "CATXT Sync",
                            f"Nothing new to post for "
                            f"{dates[0].strftime('%d %b')} – {dates[-1].strftime('%d %b %Y')}.\n"
                            f"All events are already in CATXT.",
                        ))
                        return

                    log.info(
                        f"Range review: {new_event_days} day(s) with new events "
                        f"({len(all_days)} day(s) total) — opening review dialog."
                    )

                    result_holder: list = [None]
                    copies_holder:    list = [[]]
                    perm_skip_holder: list = [[]]
                    done = threading.Event()

                    def show_review():
                        dlg = ReviewDialog(
                            self._root, None, None, None, config,
                            days=all_days,
                            existing_hours=_existing_hrs,
                        )
                        result_holder[0]    = dlg.result
                        copies_holder[0]    = dlg.copies
                        perm_skip_holder[0] = dlg.permanently_skipped
                        done.set()

                    self._gui_queue.put(show_review)
                    done.wait(timeout=7200)   # 2-hour timeout for full-week review

                    result = result_holder[0]   # dict[date, list] or None if cancelled
                    if result is None:
                        log.info("Range review cancelled by user.")
                        return

                    # Post each date's confirmed entries
                    for post_date, entries in result.items():
                        log.info(f"Range sync: posting {len(entries)} entry(ies) for {post_date}")
                        ok, sk, fa, fn = core.post_day(
                            session, post_date, entries, processed
                        )
                        core.save_processed(processed)
                        total_ok      += ok
                        total_skipped += sk
                        total_failed  += fa
                        total_failed_names.extend(fn)

                    # Save "Skip always" events to processed cache so they never reappear
                    for _skip_date, _skip_id in (perm_skip_holder[0] or []):
                        _ids = processed.setdefault(str(_skip_date), [])
                        if _skip_id not in _ids:
                            _ids.append(_skip_id)
                    if perm_skip_holder[0]:
                        core.save_processed(processed)
                        log.info(f"Permanently skipped {len(perm_skip_holder[0])} event(s).")

                    # Process "Copy to days…" requests from the review dialog
                    import copy as _copy, time as _time
                    for copy_req in (copies_holder[0] or []):
                        for copy_date in copy_req["dates"]:
                            ev = _copy.copy(copy_req["event"])
                            ev["id"]    = f"copy-{int(_time.time() * 1000)}-{copy_date.isoformat()}"
                            ev["start"] = copy_date.strftime("%Y-%m-%dT09:00:00")
                            c_ok, c_sk, c_fa, c_fn = core.post_day(
                                session, copy_date, [(ev, copy_req["mapping"])], processed
                            )
                            core.save_processed(processed)
                            total_ok           += c_ok
                            total_skipped      += c_sk
                            total_failed       += c_fa
                            total_failed_names += c_fn

            else:
                # ── Auto mode: assign defaults and post per date ──────────────────
                for i, target_date in enumerate(dates, 1):
                    log.info(f"Range sync: day {i}/{n} — {target_date}")

                    mapped, unmapped = core.prepare_day(
                        target_date, config, processed,
                        prefetched_events=events_by_date.get(target_date, []),
                    )

                    if not mapped and not unmapped:
                        log.info(f"  No new events for {target_date}.")
                        continue

                    default   = config.get("default_mapping") or {}
                    to_submit = list(mapped) + [
                        (ev, default) for ev in unmapped if default
                    ]

                    if to_submit:
                        ok, sk, fa, fn = core.post_day(
                            session, target_date, to_submit, processed
                        )
                        core.save_processed(processed)
                        total_ok      += ok
                        total_skipped += sk
                        total_failed  += fa
                        total_failed_names.extend(fn)

            msg = f"{total_ok} posted"
            if total_skipped:
                msg += f", {total_skipped} already existed"
            if total_failed:
                if len(total_failed_names) == 1:
                    msg += f", 1 failed — {total_failed_names[0]}"
                elif total_failed_names:
                    preview = "; ".join(total_failed_names[:2])
                    msg += f", {total_failed} failed — {preview}"
                    if len(total_failed_names) > 2:
                        msg += " …"
                else:
                    msg += f", {total_failed} failed"
                msg += " — see log"
            title = (
                "CATXT Sync Complete"
                if not total_failed else
                "CATXT Sync — Partial Failure"
            )
            self._gui_queue.put(lambda: notify(title, msg))

        except Exception as exc:
            log.exception("Range sync worker error")
            err = str(exc)[:120]
            self._gui_queue.put(
                lambda: notify("CATXT Sync Error", err)
            )
        finally:
            self._sync_lock.release()
            self._set_icon_syncing(False)

    # ── Settings ──────────────────────────────────────────────────────────────

    def _open_settings(self):
        config = core.load_config()
        dlg    = SettingsDialog(self._root, config)
        if dlg.result is not None:
            core.save_config(dlg.result)
            log.info("Settings saved.")
            notify("CATXT Sync", "Settings saved.")

    # ── Log viewer ────────────────────────────────────────────────────────────

    def _open_log(self):
        if LOG_FILE.exists():
            subprocess.Popen(["notepad", str(LOG_FILE)])
        else:
            notify("CATXT Sync", "Log file not found.")

    # ── Quit ─────────────────────────────────────────────────────────────────

    def _quit(self):
        import os
        from tkinter import messagebox
        sync_running = not self._sync_lock.acquire(blocking=False)
        if not sync_running:
            self._sync_lock.release()

        if sync_running:
            answer = messagebox.askyesno(
                "CATXT Sync — Quit",
                "A sync is in progress. Quit anyway?\n\n"
                "Any entries not yet posted will not be submitted.",
                icon="warning",
            )
            if not answer:
                return

        log.info("CATXT Sync shutting down.")
        # Remove PID lock file so the MCP server won't report a stale "running" state
        try:
            (Path(__file__).parent / "catxt_app.pid").unlink(missing_ok=True)
        except Exception:
            pass
        try:
            if self._icon:
                self._icon.stop()
        except Exception:
            pass
        import time as _time
        _time.sleep(0.5)   # give pystray time to remove the tray icon
        os._exit(0)

    # ── Sync orchestration ────────────────────────────────────────────────────
    # ── MCP Connector server ─────────────────────────────────────────────────

    def _start_mcp_server(self):
        """Start the CATXT MCP server on a background daemon thread."""
        try:
            import catxt_mcp_server as _mcp
        except ModuleNotFoundError as exc:
            if "catxt_mcp_server" in str(exc):
                log.debug("catxt_mcp_server.py not found — MCP Connector not started.")
            else:
                log.warning(
                    f"MCP Connector not started — {exc}.  "
                    "Run: pip install starlette uvicorn"
                )
            return
        except ImportError as exc:
            log.warning(f"MCP Connector not started — {exc}")
            return

        port = _mcp.DEFAULT_PORT

        # If a standalone MCP server is already running on this port (e.g. started
        # at logon via Task Scheduler), skip starting our own embedded copy so we
        # don't fight over the port.
        import socket as _socket
        try:
            _probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            _probe.settimeout(0.5)
            _probe.connect(("127.0.0.1", port))
            _probe.close()
            log.info(
                f"CATXT MCP Connector already running on port {port} "
                f"(standalone mode) — skipping embedded server."
            )
            return
        except OSError:
            pass   # port not yet bound — start our own

        def _run():
            log.info(
                f"CATXT MCP Connector listening on "
                f"http://127.0.0.1:{port}/mcp"
            )
            try:
                _mcp.run(port=port)
            except Exception as exc:
                log.warning(f"MCP server stopped: {exc}")

        t = threading.Thread(target=_run, daemon=True, name="catxt-mcp")
        t.start()
        log.info(
            f"CATXT MCP Connector started — "
            f"add http://127.0.0.1:{port}/mcp as a Connector in Joule."
        )

    # ── In-process background timer ───────────────────────────────────────────

    _AUTO_SYNC_INTERVAL_MS = 60 * 60 * 1000   # 60 minutes

    def _auto_sync_tick(self):
        """
        Fires every 60 minutes while the tray app is running.
        Runs only the staffing sync (respecting the 1-hour TTL) so new
        projects are picked up automatically without triggering a full
        calendar sync or review dialog.  Skips silently if another sync
        is already in progress.
        """
        log.debug("Auto-sync tick: checking staffing assignments.")
        if self._sync_lock.acquire(blocking=False):
            threading.Thread(
                target=self._background_staffing_worker, daemon=True
            ).start()
        else:
            log.debug("Auto-sync tick: sync already in progress — skipping.")
        self._root.after(self._AUTO_SYNC_INTERVAL_MS, self._auto_sync_tick)

    # How long to wait before re-sending the "session expired" toast.
    _SESSION_EXPIRY_NOTIFY_COOLDOWN_H = 4

    def _background_staffing_worker(self):
        """Hourly background staffing check — silent, TTL-gated, no notifications.
        Skips entirely if there is no live session so no browser window is opened."""
        try:
            self._set_icon_syncing(True)
            session = core.get_or_refresh_session(cookies_only=True)
            if session is None:
                now = datetime.now()
                cooldown = timedelta(hours=self._SESSION_EXPIRY_NOTIFY_COOLDOWN_H)
                if (
                    self._last_session_expiry_notify is None
                    or (now - self._last_session_expiry_notify) >= cooldown
                ):
                    self._last_session_expiry_notify = now
                    log.info("Background staffing: SAP session expired — notifying user.")
                    self._gui_queue.put(lambda: notify(
                        "CATXT — Session Expired",
                        "Your SAP session has expired.\n"
                        "Right-click the tray icon → Sync Today to re-authenticate.",
                    ))
                else:
                    log.debug(
                        "Background staffing: session expired — notification suppressed "
                        f"(cooldown {self._SESSION_EXPIRY_NOTIFY_COOLDOWN_H}h)."
                    )
                return
            # Session is live — reset the expiry cooldown so the next expiry notifies promptly
            self._last_session_expiry_notify = None
            config = core.load_config()
            csrf   = core.get_csrf_token(session)
            # force=False so the 1-hour TTL prevents redundant API calls
            added = core.sync_staffing(session, config, csrf or "", force=False)
            if added:
                log.info(f"Background staffing: {added} new project(s) added to config.")
                self._gui_queue.put(lambda n=added: notify(
                    "CATXT — New Project(s)",
                    f"{n} new project(s) added to your WBS mappings. "
                    "Open Settings to add keywords.",
                ))
        except Exception as exc:
            log.warning(f"Background staffing error: {exc}")
        finally:
            self._sync_lock.release()
            self._set_icon_syncing(False)

    # ── Scheduled sync ────────────────────────────────────────────────────────

    def _start_scheduled_sync(self):
        """Trigger the scheduled sync (called on startup when --scheduled is passed,
        and by the in-process auto-sync timer)."""
        if not self._sync_lock.acquire(blocking=False):
            log.warning("Scheduled sync: another sync already running — skipping.")
            return
        t = threading.Thread(target=self._sync_scheduled_worker, daemon=True)
        t.start()

    def _sync_scheduled_worker(self):
        """
        Scheduled sync (launched via Windows Task Scheduler).

        Phase 1  — Auto-post all specifically-mapped entries (keyword / email /
                   favorites match) immediately.  Releases the sync lock after
                   this phase so the user can trigger manual syncs independently.

        Phase 2  — Open the review dialog for any unmapped or failed entries.
                   The dialog has no timeout; it stays open until the user acts.
                   When confirmed, _scheduled_sync_through advances so the next
                   run knows not to re-cover those dates.
        """
        _lock_released = False
        try:
            self._set_icon_syncing(True)

            # ── Auth ──────────────────────────────────────────────────────────
            session = core.get_or_refresh_session()
            if session is None:
                log.error("Scheduled sync: authentication failed.")
                self._gui_queue.put(lambda: notify(
                    "CATXT Sync — Auth Failed",
                    "Scheduled sync could not authenticate. Check the log.",
                ))
                return

            core.detect_pernr(session)
            config    = core.load_config()
            processed = core.load_processed()
            csrf      = core.get_csrf_token(session)
            if csrf:
                core.sync_staffing(session, config, csrf)
            core.sync_catxt_metadata(session, config)
            config = core.load_config()

            # ── Compute date range ────────────────────────────────────────────
            start_date, end_date = core.get_scheduled_date_range(config)
            dates = core.business_days(start_date, end_date)
            log.info(
                f"Scheduled sync: {len(dates)} business day(s)  "
                f"{start_date} → {end_date}"
            )

            # ── Set watermark to one business day BEFORE start_date ───────────
            # Do this before any work so that if the worker crashes mid-run,
            # the next scheduled run starts from start_date again rather than
            # skipping to tomorrow and missing the unfinished dates.
            pre_start = start_date
            while True:
                pre_start = pre_start - timedelta(days=1)
                if pre_start.weekday() < 5:   # walk back to previous business day
                    break
            core.set_scheduled_sync_through(pre_start)
            log.debug(f"Scheduled sync: watermark set to {pre_start} (pre-run safety marker).")

            # ── Fetch calendar ────────────────────────────────────────────────
            log.info(f"Fetching calendar for {len(dates)} day(s)...")
            events_by_date = core.get_events_for_date_range(dates)

            # ── Phase 1: auto-post specifically-mapped entries ─────────────────
            total_auto_ok  = 0
            pending_dialog = []   # list of (date, event, mapping, reason)

            for target_date in dates:
                mapped, unmapped = core.prepare_day(
                    target_date, config, processed,
                    prefetched_events=events_by_date.get(target_date, []),
                )

                # Auto-post mapped entries
                if mapped:
                    ok_pairs, fail_pairs = core.post_day_auto(
                        session, target_date, mapped, processed
                    )
                    total_auto_ok += len(ok_pairs)
                    for ev, mp in fail_pairs:
                        pending_dialog.append((target_date, ev, mp, "auto-post failed"))

                # Queue unmapped (default-CC only) for user review
                # prepare_day returns unmapped as bare event dicts, not (event, mapping) pairs
                for ev in unmapped:
                    pending_dialog.append((target_date, ev, None, "unmapped"))

            core.save_processed(processed)

            # ── Cross-check pending events against CATXT ───────────────────────
            # Events may have been posted via Joule (or manually in CATXT) without
            # the tray app knowing. Fetch existing CATXT entries for each affected
            # day and silently drop any pending event whose description is already
            # there — marking it processed so it won't reappear.
            if pending_dialog:
                from collections import defaultdict as _dd2
                catxt_descs: dict = {}   # date_str → set of lowercased descriptions
                for _d, _ev, _mp, _reason in pending_dialog:
                    _key = str(_d)
                    if _key not in catxt_descs:
                        try:
                            _entries = core.get_existing_entries(session, _d)
                            catxt_descs[_key] = {
                                e.get("Ltxa1", "").strip().lower()
                                for e in _entries
                            }
                        except Exception as _exc:
                            log.debug(f"Cross-check: could not fetch CATXT entries for {_key}: {_exc}")
                            catxt_descs[_key] = set()

                filtered: list = []
                newly_processed = False
                for _d, _ev, _mp, _reason in pending_dialog:
                    _subj = _ev.get("subject", "")[:40].strip().lower()
                    if _subj and _subj in catxt_descs.get(str(_d), set()):
                        # Already in CATXT — mark processed and skip review
                        _ev_id = _ev.get("id") or (_ev.get("subject", "") + _ev.get("start", ""))
                        _day_ids = processed.setdefault(str(_d), [])
                        if _ev_id not in _day_ids:
                            _day_ids.append(_ev_id)
                            newly_processed = True
                        log.info(
                            f"Scheduled sync: '{_ev.get('subject', '')}' on {_d} "
                            "already in CATXT — marked processed, skipping review."
                        )
                    else:
                        filtered.append((_d, _ev, _mp, _reason))
                pending_dialog = filtered
                if newly_processed:
                    core.save_processed(processed)

            # ── Release sync lock after Phase 1 so manual syncs can proceed ───
            self._sync_lock.release()
            _lock_released = True
            self._set_icon_syncing(False)

            # ── Phase 2: handle results ────────────────────────────────────────
            if not pending_dialog:
                # Clean run — advance the watermark
                core.set_scheduled_sync_through(end_date)
                msg = (f"{total_auto_ok} entr(ies) auto-posted."
                       if total_auto_ok else "Nothing new to post.")
                log.info(f"Scheduled sync: clean run. {msg}")
                self._gui_queue.put(lambda m=msg: notify("CATXT Sync — Scheduled", m))
                return

            # Build per-date mapped/unmapped buckets for the review dialog
            # (failures land in the mapped bucket, marked visually by the dialog)
            from collections import defaultdict as _dd
            mapped_by_date:   dict = _dd(list)
            unmapped_by_date: dict = _dd(list)
            for d, ev, mp, reason in pending_dialog:
                if reason == "unmapped":
                    unmapped_by_date[d].append(ev)   # ReviewDialog expects bare events
                else:
                    mapped_by_date[d].append((ev, mp))

            all_dates = sorted(
                set(mapped_by_date) | set(unmapped_by_date)
            )
            pending_days = [
                (d, mapped_by_date.get(d, []), unmapped_by_date.get(d, []))
                for d in all_dates
            ]

            n_pending = len(pending_dialog)
            n_auto    = total_auto_ok
            _end_date = end_date   # capture for closure

            log.info(
                f"Scheduled sync: {n_auto} auto-posted, "
                f"{n_pending} item(s) queued for review."
            )

            # Fetch current CATXT hours now (in the worker thread, while we
            # have a session) so the review dialog shows the correct daily
            # total — including the entries just auto-posted above.
            try:
                _existing_hrs = core.get_existing_hours_per_day(session, all_dates)
            except Exception:
                _existing_hrs = {}

            def show_scheduled_review():
                # Toast first so the user knows the dialog is waiting
                notify(
                    "CATXT Sync — Review Required",
                    f"{n_auto} entr(ies) auto-posted.  "
                    f"{n_pending} item(s) need your review — see review dialog.",
                )
                try:
                    dlg = ReviewDialog(
                        self._root, None, None, None, config,
                        days=pending_days,
                        existing_hours=_existing_hrs,
                    )
                    confirmed = dlg.result
                    perm_skip = dlg.permanently_skipped
                except Exception as _dlg_err:
                    log.exception(f"Scheduled review dialog error: {_dlg_err}")
                    return

                if not confirmed and not perm_skip:
                    log.info("Scheduled review: cancelled or closed without confirming.")
                    return

                # Post confirmed entries in a fresh background thread so we
                # don't block the main/GUI thread.
                import copy as _copy
                def post_confirmed():
                    try:
                        sess2 = core.get_or_refresh_session()
                        if sess2 is None:
                            log.error("Scheduled post-confirm: auth failed.")
                            return
                        proc2 = core.load_processed()   # fresh load
                        if confirmed:
                            for post_date, entries in confirmed.items():
                                log.info(
                                    f"Scheduled confirm: posting {len(entries)} "
                                    f"entr(ies) for {post_date}"
                                )
                                core.post_day(sess2, post_date, entries, proc2)
                        for skip_date, skip_id in (perm_skip or []):
                            ids = proc2.setdefault(str(skip_date), [])
                            if skip_id not in ids:
                                ids.append(skip_id)
                        core.save_processed(proc2)
                        core.set_scheduled_sync_through(_end_date)
                        # ── Completion toast ──────────────────────────────────
                        n_posted = sum(
                            len(v) for v in (confirmed or {}).values()
                        )
                        n_perm   = len(perm_skip or [])
                        # n_posted can exceed n_pending if the review dialog
                        # let the user post additional entries beyond the
                        # original queue — clamp to avoid a negative count.
                        n_temp   = max(0, n_pending - n_posted - n_perm)
                        parts = []
                        if n_posted:
                            parts.append(f"{n_posted} posted")
                        if n_perm:
                            parts.append(f"{n_perm} permanently skipped")
                        _msg = (", ".join(parts) + ".") if parts else "Nothing posted."
                        if n_temp:
                            _msg += (
                                f"  {n_temp} skipped event(s) will appear"
                                f" again on the next sync."
                            )
                        log.info(f"Scheduled post-confirm complete: {_msg}")
                        self._gui_queue.put(
                            lambda m=_msg: notify("CATXT Sync — Done", m)
                        )
                    except Exception as exc:
                        log.exception(f"Scheduled post-confirm error: {exc}")

                threading.Thread(target=post_confirmed, daemon=True).start()

            self._gui_queue.put(show_scheduled_review)

        except Exception as exc:
            log.exception(f"Scheduled sync worker error: {exc}")
            self._gui_queue.put(lambda: notify(
                "CATXT — Scheduled Sync Error",
                "An unexpected error occurred. Check the log.",
            ))
        finally:
            if not _lock_released:
                try:
                    self._sync_lock.release()
                except Exception:
                    pass
            self._set_icon_syncing(False)

    def _start_sync(
        self,
        target_date: date,
        manual: bool = False,
        preview_only: bool = False,
    ):
        if not self._sync_lock.acquire(blocking=False):
            notify("CATXT Sync", "A sync is already in progress.")
            return
        t = threading.Thread(
            target=self._sync_worker,
            args=(target_date, manual, preview_only),
            daemon=True,
        )
        t.start()

    def _sync_worker(
        self,
        target_date: date,
        manual: bool,
        preview_only: bool,
    ):
        """Background sync thread."""
        try:
            self._set_icon_syncing(True)
            log.info(f"{'=' * 60}")
            log.info(f"CATXT Sync — {target_date.strftime('%A, %B %d %Y')}")

            # ── Auth ──────────────────────────────────────────────────────────
            session = core.get_or_refresh_session()
            if session is None:
                self._gui_queue.put(lambda: notify(
                    "CATXT Sync — Auth Failed",
                    "Could not authenticate. Check the log for details.",
                ))
                return

            # ── PERNR ─────────────────────────────────────────────────────────
            core.detect_pernr(session)

            # ── Config / processed ────────────────────────────────────────────
            config    = core.load_config()
            processed = core.load_processed()

            # ── Staffing sync + CATXT metadata (silent, once per run) ────────────
            csrf = core.get_csrf_token(session)
            if csrf:
                core.sync_staffing(session, config, csrf)
                core.sync_catxt_metadata(session, config)
                config = core.load_config()   # reload after staffing + metadata

            # ── Prepare events ────────────────────────────────────────────────
            mapped, unmapped = core.prepare_day(target_date, config, processed)

            # (Even if no new events are found, open the review dialog so the
            # user can add manual entries for the day.)

            # ── Preview-only: open review dialog but don't post ───────────────
            if preview_only:
                done  = threading.Event()
                def show_preview():
                    ReviewDialog(self._root, target_date, mapped, unmapped, config,
                                 preview_only=True)
                    done.set()
                self._gui_queue.put(show_preview)
                done.wait(timeout=3600)   # 1-hour timeout
                return

            # ── Manual: open review dialog and wait for confirmation ──────────
            copies_holder: list = [[]]
            if manual:
                result_holder: list = [None]
                done = threading.Event()

                def show_review():
                    try:
                        dlg = ReviewDialog(
                            self._root, target_date, mapped, unmapped, config
                        )
                        result_holder[0] = dlg.result
                        copies_holder[0] = dlg.copies
                    except Exception as _dlg_exc:
                        log.exception(f"Review dialog error: {_dlg_exc}")
                    finally:
                        done.set()

                self._gui_queue.put(show_review)
                done.wait(timeout=3600)   # 1-hour timeout

                to_submit = result_holder[0]
                if to_submit is None:
                    log.info("Review cancelled by user.")
                    return
            else:
                # Auto mode: apply default_mapping to unmapped
                default = config.get("default_mapping") or {}
                to_submit = list(mapped) + [
                    (ev, default) for ev in unmapped if default
                ]

            # ── Post ──────────────────────────────────────────────────────────
            if not to_submit:
                self._gui_queue.put(lambda: notify(
                    "CATXT Sync", "No events to post after review."
                ))
                return

            ok, skipped, failed, failed_names = core.post_day(
                session, target_date, to_submit, processed
            )
            core.save_processed(processed)

            # ── Process "Copy to days…" requests queued in the review dialog ──
            import copy as _copy, time as _time
            for copy_req in copies_holder[0]:
                for copy_date in copy_req["dates"]:
                    ev = _copy.copy(copy_req["event"])
                    ev["id"]    = f"copy-{int(_time.time() * 1000)}-{copy_date.isoformat()}"
                    ev["start"] = copy_date.strftime("%Y-%m-%dT09:00:00")
                    c_ok, c_sk, c_fa, c_names = core.post_day(
                        session, copy_date, [(ev, copy_req["mapping"])], processed
                    )
                    core.save_processed(processed)
                    ok           += c_ok
                    skipped      += c_sk
                    failed       += c_fa
                    failed_names += c_names

            msg = f"{ok} posted"
            if skipped:
                msg += f", {skipped} already existed"
            if failed:
                if len(failed_names) == 1:
                    msg += f", 1 failed — {failed_names[0]}"
                elif failed_names:
                    preview = "; ".join(failed_names[:2])
                    msg += f", {failed} failed — {preview}"
                    if len(failed_names) > 2:
                        msg += " …"
                else:
                    msg += f", {failed} failed"
                msg += " — see log"
            title = "CATXT Sync Complete" if not failed else "CATXT Sync — Partial Failure"
            self._gui_queue.put(lambda: notify(title, msg))

        except Exception as exc:
            log.exception("Sync worker error")
            err = str(exc)[:120]
            self._gui_queue.put(lambda: notify("CATXT Sync Error", err))
        finally:
            self._sync_lock.release()
            self._set_icon_syncing(False)

    # ── Staffing sync (standalone) ────────────────────────────────────────────

    def _start_staffing_sync(self):
        if not self._sync_lock.acquire(blocking=False):
            notify("CATXT Sync", "A sync is already in progress.")
            return
        threading.Thread(target=self._staffing_worker, daemon=True).start()

    def _staffing_worker(self):
        try:
            self._set_icon_syncing(True)
            session = core.get_or_refresh_session()
            if session is None:
                self._gui_queue.put(lambda: notify(
                    "CATXT Sync", "Authentication failed."
                ))
                return
            core.detect_pernr(session)
            config = core.load_config()
            csrf   = core.get_csrf_token(session)
            added  = core.sync_staffing(session, config, csrf or "", force=True)
            msg    = f"{added} new project(s) added." if added else "Already up to date."
            self._gui_queue.put(lambda: notify("Staffing Sync", msg))
        except Exception as exc:
            log.exception("Staffing sync error")
            self._gui_queue.put(lambda: notify("Staffing Sync Error", str(exc)[:120]))
        finally:
            self._sync_lock.release()
            self._set_icon_syncing(False)

    # ── Metadata sync (standalone) ────────────────────────────────────────────

    def _start_metadata_sync(self):
        if not self._sync_lock.acquire(blocking=False):
            notify("CATXT Sync", "A sync is already in progress.")
            return
        threading.Thread(target=self._metadata_worker, daemon=True).start()

    def _metadata_worker(self):
        try:
            self._set_icon_syncing(True)
            session = core.get_or_refresh_session()
            if session is None:
                self._gui_queue.put(lambda: notify(
                    "CATXT Sync", "Authentication failed."
                ))
                return
            core.detect_pernr(session)
            config  = core.load_config()
            changed = core.sync_catxt_metadata(session, config, force=True)
            config  = core.load_config()
            n_tt    = len(config.get("_task_types", {}))
            n_st    = sum(
                len(v.get("subtypes", []))
                for v in config.get("_task_types", {}).values()
            )
            n_fav   = len(config.get("_favorites", []))
            msg = (
                f"{n_tt} task types, {n_st} sub-types, {n_fav} favorites."
                if changed else
                f"Already up to date ({n_tt} types, {n_st} sub-types)."
            )
            self._gui_queue.put(lambda: notify("Metadata Sync", msg))
        except Exception as exc:
            log.exception("Metadata sync error")
            self._gui_queue.put(lambda: notify("Metadata Sync Error", str(exc)[:120]))
        finally:
            self._sync_lock.release()
            self._set_icon_syncing(False)

    # ── Icon state ────────────────────────────────────────────────────────────

    def _set_icon_syncing(self, syncing: bool):
        if self._icon is None:
            return
        try:
            self._icon.icon = (
                self._icon_syncing if syncing else self._icon_idle
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Date / date-range picker dialog
# ══════════════════════════════════════════════════════════════════════════════

class _DateRangeDialog:
    """
    Date picker supporting both a single date and a date range.

    result:
        None          — cancelled
        date          — single date selected
        (date, date)  — (from, to) range selected
    """

    def __init__(self, parent: tk.Misc, title: str = "Sync — Choose Date"):
        self.result = None
        today = date.today()

        self._top = tk.Toplevel(parent)
        self._top.title(title)
        self._top.resizable(False, False)
        self._top.grab_set()
        self._top.protocol("WM_DELETE_WINDOW", self._top.destroy)
        try:
            self._top.iconbitmap(_ICO_PATH)
        except Exception:
            pass

        pad = {"padx": 16, "pady": 4}

        # ── Mode selector ─────────────────────────────────────────────────────
        mode_frame = tk.Frame(self._top)
        mode_frame.grid(row=0, column=0, sticky="w", **pad)
        self._mode = tk.StringVar(value="single")
        tk.Radiobutton(
            mode_frame, text="Single date", variable=self._mode,
            value="single", command=self._on_mode,
            font=("Segoe UI", 9),
        ).pack(side="left")
        tk.Radiobutton(
            mode_frame, text="Date range", variable=self._mode,
            value="range", command=self._on_mode,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=16)

        # ── Quick-select shortcuts ────────────────────────────────────────────
        quick = tk.Frame(self._top)
        quick.grid(row=1, column=0, columnspan=2, sticky="w", padx=16, pady=(2, 6))
        tk.Label(quick, text="Quick:", font=("Segoe UI", 8), fg="#555"
                 ).pack(side="left", padx=(0, 6))
        for label, fn in [
            ("Today",       self._quick_today),
            ("This week",   self._quick_this_week),
            ("Last week",   self._quick_last_week),
            ("Month to date", self._quick_this_month),
        ]:
            tk.Button(
                quick, text=label, command=fn,
                relief="flat", padx=7, pady=2, font=("Segoe UI", 8),
                bg="#e8f0fe", cursor="hand2",
            ).pack(side="left", padx=(0, 4))

        # ── "From" row (always visible) ───────────────────────────────────────
        self._from_label = tk.Label(
            self._top, text="Date:", font=("Segoe UI", 9), width=6, anchor="e"
        )
        self._from_label.grid(row=2, column=0, sticky="e", padx=(16, 4), pady=6)

        from_spin = tk.Frame(self._top)
        from_spin.grid(row=2, column=1, sticky="w", pady=6, padx=(0, 16))
        self._fy = tk.StringVar(value=str(today.year))
        self._fm = tk.StringVar(value=f"{today.month:02d}")
        self._fd = tk.StringVar(value=f"{today.day:02d}")
        self._make_date_row(from_spin, self._fy, self._fm, self._fd)

        # ── "To" row (range mode only) ────────────────────────────────────────
        self._to_label = tk.Label(
            self._top, text="To:", font=("Segoe UI", 9), width=6, anchor="e"
        )
        to_spin = tk.Frame(self._top)
        self._ty = tk.StringVar(value=str(today.year))
        self._tm = tk.StringVar(value=f"{today.month:02d}")
        self._td = tk.StringVar(value=f"{today.day:02d}")
        self._make_date_row(to_spin, self._ty, self._tm, self._td)
        self._to_widgets = (self._to_label, to_spin)
        # Hidden initially
        self._to_row = to_spin

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = tk.Frame(self._top)
        btn_row.grid(row=5, column=0, columnspan=2, pady=(6, 12))
        tk.Button(
            btn_row, text="Cancel",
            command=self._top.destroy, relief="flat", padx=10, pady=4,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=4)
        tk.Button(
            btn_row, text="Sync →",
            command=self._confirm,
            bg="#0070d2", fg="white", relief="flat", padx=10, pady=4,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=4)

        self._on_mode()   # set initial layout
        self._top.update_idletasks()
        x = (self._top.winfo_screenwidth()  - self._top.winfo_width())  // 2
        y = (self._top.winfo_screenheight() - self._top.winfo_height()) // 2
        self._top.geometry(f"+{x}+{y}")
        parent.wait_window(self._top)

    @staticmethod
    def _make_date_row(frame, y_var, m_var, d_var):
        tk.Spinbox(frame, from_=2020, to=2099, textvariable=y_var,
                   width=6, font=("Segoe UI", 10)).pack(side="left")
        tk.Label(frame, text="-").pack(side="left", padx=2)
        tk.Spinbox(frame, from_=1, to=12, textvariable=m_var,
                   width=4, format="%02.0f", font=("Segoe UI", 10)).pack(side="left")
        tk.Label(frame, text="-").pack(side="left", padx=2)
        tk.Spinbox(frame, from_=1, to=31, textvariable=d_var,
                   width=4, format="%02.0f", font=("Segoe UI", 10)).pack(side="left")

    def _on_mode(self):
        if self._mode.get() == "range":
            self._from_label.config(text="From:")
            self._to_label.grid(row=3, column=0, sticky="e", padx=(16, 4), pady=4)
            self._to_row.grid(row=3, column=1, sticky="w", pady=4, padx=(0, 16))
        else:
            self._from_label.config(text="Date:")
            self._to_label.grid_remove()
            self._to_row.grid_remove()
        self._top.update_idletasks()

    def _set_from(self, d):
        self._fy.set(str(d.year))
        self._fm.set(f"{d.month:02d}")
        self._fd.set(f"{d.day:02d}")

    def _set_to(self, d):
        self._ty.set(str(d.year))
        self._tm.set(f"{d.month:02d}")
        self._td.set(f"{d.day:02d}")

    def _quick_today(self):
        from datetime import timedelta
        self._mode.set("single")
        self._on_mode()
        self._set_from(date.today())
        self._confirm()

    def _quick_this_week(self):
        from datetime import timedelta
        today = date.today()
        mon = today - timedelta(days=today.weekday())
        fri = mon + timedelta(days=4)
        self._mode.set("range")
        self._on_mode()
        self._set_from(mon)
        self._set_to(fri)
        self._confirm()

    def _quick_last_week(self):
        from datetime import timedelta
        today = date.today()
        mon = today - timedelta(days=today.weekday() + 7)
        fri = mon + timedelta(days=4)
        self._mode.set("range")
        self._on_mode()
        self._set_from(mon)
        self._set_to(fri)
        self._confirm()

    def _quick_this_month(self):
        today = date.today()
        first = today.replace(day=1)
        # Cap at today — CATXT rejects entries more than ~1 week in the future
        self._mode.set("range")
        self._on_mode()
        self._set_from(first)
        self._set_to(today)
        self._confirm()

    def _confirm(self):
        try:
            d_from = date(int(self._fy.get()), int(self._fm.get()), int(self._fd.get()))
            if self._mode.get() == "range":
                d_to = date(int(self._ty.get()), int(self._tm.get()), int(self._td.get()))
                if d_to < d_from:
                    tk.messagebox.showerror(
                        "Invalid range", "'To' date must be on or after 'From' date.",
                        parent=self._top,
                    )
                    return
                self.result = (d_from, d_to)
            else:
                self.result = d_from
        except ValueError:
            tk.messagebox.showerror(
                "Invalid date", "Please enter a valid date.", parent=self._top
            )
            return
        self._top.destroy()


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    CatxtApp().run()
