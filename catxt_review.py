"""
catxt_review.py — Event review dialog for CATXT Sync
=====================================================
Opens a modal tkinter window showing calendar events before they are
posted to CATXT.

Layout
------
  • Treeview table: Subject | Start | Hours | WBS / Project
    - Automatically-mapped events shown normally.
    - Unmapped events appear at the bottom in yellow, pre-assigned to the
      default cost center.  User can reassign or skip them.
  • Assignment panel (bottom of table): shows details for the selected
    row; combobox lets user change WBS; Skip button removes the row.
  • Footer: total hours  |  Cancel  |  Confirm & Post

Return value
------------
  result — list of (event, mapping) to post, or None if cancelled.
"""

import os as _os
import sys as _sys
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import date, datetime
from typing import Optional

# Resolve icon path whether running as script or PyInstaller bundle
_ICON_PATH = _os.path.join(
    getattr(_sys, "_MEIPASS", _os.path.dirname(_os.path.abspath(__file__))),
    "assets", "catxt.ico",
)

# Colour tokens
COL_MAPPED   = "#ffffff"
COL_UNMAPPED = "#fff3cd"   # yellow tint
COL_SKIPPED  = "#f0f0f0"
COL_MANUAL   = "#d4edda"   # light green — manually added entries
FG_SKIPPED   = "#999999"
COL_HDR_BG   = "#003366"   # SAP blue
COL_HDR_FG   = "#ffffff"
COL_ACCENT   = "#0070d2"


class ReviewDialog:
    """
    Modal event-review dialog.

    Parameters
    ----------
    parent   : tk widget (hidden root or existing window)
    target_date : the date being processed
    mapped   : [(event, mapping), ...]  — events with a keyword/email match
    unmapped : [event, ...]             — events with no specific rule match
    config   : full config dict (for WBS label list and default_mapping)
    """

    def __init__(
        self,
        parent: tk.Misc,
        target_date: date,
        mapped: list,
        unmapped: list,
        config: dict,
        preview_only: bool = False,
        days: list = None,           # list of (date, mapped, unmapped) for multi-date mode
        existing_hours: dict = None, # "YYYYMMDD" → already-posted hours from CATXT
    ):
        self.result: Optional[list] = None   # list or dict[date, list] depending on mode
        self.copies: list = []               # copy-to-days requests from "Copy to days…" button
        self.permanently_skipped: list = []  # (date, event_id) pairs — written to processed cache
        self._preview_only = preview_only
        self._config  = config
        self._multi_date = days is not None
        self._existing_hours = existing_hours or {}  # already-posted hours per day

        # ── Build internal row model ──────────────────────────────────────────
        default_m = config.get("default_mapping") or {}
        self._rows: list[dict] = []

        if self._multi_date:
            # Multi-date: flat list with separator rows between each day's entries
            self._target_date = days[0][0] if days else target_date
            for day_date, day_mapped, day_unmapped in (days or []):
                self._rows.append({"_separator": True, "date": day_date})
                for event, mapping in day_mapped:
                    self._rows.append({
                        "event":     event,
                        "mapping":   mapping,
                        "skipped":   False,
                        "tasktype":  mapping.get("tasktype", "CFPP" if mapping.get("rproj") else "MEET"),
                        "zzsubtype": mapping.get("zzsubtype") or mapping.get("tasklevel", ""),
                        "ltxa1":     None,
                        "date":      day_date,
                        "_mapped":   True,
                    })
                for event in day_unmapped:
                    self._rows.append({
                        "event":     event,
                        "mapping":   default_m,
                        "skipped":   False,
                        "tasktype":  default_m.get("tasktype", "MEET"),
                        "zzsubtype": default_m.get("zzsubtype", ""),
                        "ltxa1":     None,
                        "date":      day_date,
                        "_mapped":   False,
                    })
            self._unmapped_start = 0   # not used in multi-date mode
        else:
            # Single-date mode (existing behaviour)
            self._target_date = target_date
            for event, mapping in (mapped or []):
                self._rows.append({
                    "event":     event,
                    "mapping":   mapping,
                    "skipped":   False,
                    "tasktype":  mapping.get("tasktype", "CFPP" if mapping.get("rproj") else "MEET"),
                    "zzsubtype": mapping.get("zzsubtype") or mapping.get("tasklevel", ""),
                    "ltxa1":     None,
                    "_mapped":   True,
                })
            for event in (unmapped or []):
                self._rows.append({
                    "event":     event,
                    "mapping":   default_m,
                    "skipped":   False,
                    "tasktype":  default_m.get("tasktype", "MEET"),
                    "zzsubtype": default_m.get("zzsubtype", ""),
                    "ltxa1":     None,
                    "_mapped":   False,
                })
            self._unmapped_start = len(mapped or [])

        # ── WBS label → mapping dict lookup ──────────────────────────────────
        self._label_to_mapping: dict[str, dict] = {}
        for m in config.get("wbs_mappings", []):
            if m.get("label"):
                self._label_to_mapping[m["label"]] = m
        if default_m.get("label"):
            self._label_to_mapping[default_m["label"]] = default_m
        # "Default Cost Centre" always available even if label differs
        self._default_label = default_m.get("label", "Default Cost Centre")
        self._all_labels = [self._default_label] + [
            m["label"] for m in config.get("wbs_mappings", []) if m.get("label")
        ]

        # ── Build window ──────────────────────────────────────────────────────
        self.top = tk.Toplevel(parent)
        try:
            self.top.iconbitmap(_ICON_PATH)
        except Exception:
            pass

        if self._multi_date and days:
            start_d = days[0][0]
            end_d   = days[-1][0]
            self.top.title(
                f"Review CATXT Entries — "
                f"{start_d.strftime('%a %d %b')} – {end_d.strftime('%a %d %b %Y')}"
            )
            self.top.geometry("960x640")
        else:
            self.top.title(
                f"Review CATXT Entries — "
                f"{target_date.strftime('%A, %B %d %Y')}"
            )
            self.top.geometry("960x560")

        self.top.minsize(700, 420)
        self.top.resizable(True, True)
        self.top.grab_set()     # modal
        self.top.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self._prev_idx:  Optional[int] = None   # for auto-apply on row navigation
        self._selecting: bool          = False   # re-entry guard for _on_select
        self._build_ui()
        self._refresh_tree()
        self._refresh_footer()

        # Centre on screen
        self.top.update_idletasks()
        x = (self.top.winfo_screenwidth()  - self.top.winfo_width())  // 2
        y = (self.top.winfo_screenheight() - self.top.winfo_height()) // 2
        self.top.geometry(f"+{x}+{y}")

        parent.wait_window(self.top)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self.top.columnconfigure(0, weight=1)
        self.top.rowconfigure(1, weight=1)

        # ── Title bar ─────────────────────────────────────────────────────────
        hdr = tk.Frame(self.top, bg=COL_HDR_BG, padx=12, pady=8)
        hdr.grid(row=0, column=0, sticky="ew")
        tk.Label(
            hdr,
            text="CATXT — Review & Confirm Entries",
            bg=COL_HDR_BG, fg=COL_HDR_FG,
            font=("Segoe UI", 12, "bold"),
        ).pack(side="left")
        tk.Label(
            hdr,
            text=self._target_date.strftime("%A, %B %d %Y"),
            bg=COL_HDR_BG, fg="#a8c8f0",
            font=("Segoe UI", 10),
        ).pack(side="right")

        # ── Main frame (tree + scrollbar) ─────────────────────────────────────
        main = tk.Frame(self.top, bg="white")
        main.grid(row=1, column=0, sticky="nsew", padx=10, pady=(8, 0))
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)

        cols = ("subject", "start", "hours", "mapping")
        self._tree = ttk.Treeview(
            main, columns=cols, show="headings", selectmode="extended"
        )
        self._tree.heading("subject", text="Meeting Subject")
        self._tree.heading("start",   text="Start")
        self._tree.heading("hours",   text="Hours")
        self._tree.heading("mapping", text="WBS / Project")
        self._tree.column("subject", width=340, minwidth=200, stretch=True)
        self._tree.column("start",   width=70,  minwidth=60,  stretch=False, anchor="center")
        self._tree.column("hours",   width=55,  minwidth=50,  stretch=False, anchor="center")
        self._tree.column("mapping", width=280, minwidth=180, stretch=True)

        self._tree.tag_configure("mapped",    background=COL_MAPPED)
        self._tree.tag_configure("unmapped", background=COL_UNMAPPED)
        self._tree.tag_configure("skipped",  background=COL_SKIPPED, foreground=FG_SKIPPED)
        self._tree.tag_configure("manual",   background=COL_MANUAL)
        self._tree.tag_configure("separator",  background="#d4dde8", foreground="#1a1a2e",
                                  font=("Segoe UI", 9, "bold"))
        self._tree.tag_configure("tentative", background="#fff3cd", foreground="#7d5a00")

        vsb = ttk.Scrollbar(main, orient="vertical",   command=self._tree.yview)
        hsb = ttk.Scrollbar(main, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        # Keyboard shortcuts
        self._tree.bind("<Return>",         lambda _: self._on_apply())
        self._tree.bind("<Delete>",         lambda _: self._on_skip())
        self._tree.bind("<s>",              lambda _: self._on_skip())
        self._tree.bind("<Control-Return>", lambda _: self._on_confirm())
        self.top.bind( "<Control-Return>",  lambda _: self._on_confirm())
        self.top.bind( "<Escape>",          lambda _: self._on_cancel())

        # ── Assignment panel ──────────────────────────────────────────────────
        assign_frame = tk.Frame(self.top, bg="#f5f5f5", relief="groove", bd=1)
        assign_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=(4, 0))
        assign_frame.columnconfigure(1, weight=1)
        assign_frame.columnconfigure(3, weight=1)

        # Row 0 — Description (editable; controls Ltxa1 sent to CATXT)
        tk.Label(
            assign_frame, text="Description:", bg="#f5f5f5",
            font=("Segoe UI", 9), anchor="w",
        ).grid(row=0, column=0, padx=(8, 4), pady=(6, 2), sticky="w")
        self._desc_var = tk.StringVar()
        self._desc_entry = tk.Entry(
            assign_frame, textvariable=self._desc_var,
            font=("Segoe UI", 9), width=48, state="disabled",
        )
        self._desc_entry.grid(row=0, column=1, columnspan=4, padx=4, pady=(6, 2), sticky="ew")
        tk.Label(
            assign_frame, text="(max 40 chars)", bg="#f5f5f5",
            fg="#999", font=("Segoe UI", 7),
        ).grid(row=0, column=5, padx=(0, 8), sticky="w")

        # Row 1 — Assign to + Task Type
        tk.Label(assign_frame, text="Assign to:", bg="#f5f5f5",
                 font=("Segoe UI", 9)).grid(row=1, column=0, padx=(8, 4), pady=3, sticky="w")
        self._assign_var = tk.StringVar()
        self._assign_cb = ttk.Combobox(
            assign_frame, textvariable=self._assign_var,
            values=self._all_labels, state="readonly", width=30)
        self._assign_cb.grid(row=1, column=1, padx=(0, 12), pady=3, sticky="ew")

        tk.Label(assign_frame, text="Task Type:", bg="#f5f5f5",
                 font=("Segoe UI", 9)).grid(row=1, column=2, padx=(0, 4), pady=3, sticky="w")
        self._tasktype_var = tk.StringVar()
        tt_labels = [f"{c} - {v['label']}" for c, v in self._tasktype_data().items()]
        self._tasktype_cb = ttk.Combobox(
            assign_frame, textvariable=self._tasktype_var,
            values=tt_labels, state="readonly", width=26)
        self._tasktype_cb.grid(row=1, column=3, padx=(0, 8), pady=3, sticky="ew")
        self._tasktype_cb.bind("<<ComboboxSelected>>", self._on_tasktype_changed)

        tk.Label(assign_frame, text="Hours:", bg="#f5f5f5",
                 font=("Segoe UI", 9)).grid(row=1, column=4, padx=(0, 4), pady=3, sticky="w")
        self._hours_var = tk.StringVar(value="0.00")
        tk.Spinbox(
            assign_frame, from_=0.25, to=24.0, increment=0.25,
            textvariable=self._hours_var, width=6,
            font=("Segoe UI", 9), format="%.2f",
        ).grid(row=1, column=5, padx=(0, 10), pady=3, sticky="w")

        # Row 2 — Sub-type + Requestor (required for OPEN tasktype) + buttons
        tk.Label(assign_frame, text="Sub-type:", bg="#f5f5f5",
                 font=("Segoe UI", 9)).grid(row=2, column=0, padx=(8, 4), pady=(3, 6), sticky="w")
        self._subtype_var = tk.StringVar()
        self._subtype_cb = ttk.Combobox(
            assign_frame, textvariable=self._subtype_var,
            values=[], state="readonly", width=24)
        self._subtype_cb.grid(row=2, column=1, padx=(0, 8), pady=(3, 6), sticky="ew")

        self._zzcontpers_label = tk.Label(
            assign_frame, text="Requestor (User ID):", bg="#f5f5f5",
            font=("Segoe UI", 9),
        )
        self._zzcontpers_label.grid(row=2, column=2, padx=(0, 4), pady=(3, 6), sticky="w")
        self._zzcontpers_var = tk.StringVar(
            value=self._config.get("requestor_default", "")
        )
        self._zzcontpers_entry = tk.Entry(
            assign_frame, textvariable=self._zzcontpers_var,
            font=("Segoe UI", 9), width=14,
        )
        self._zzcontpers_entry.grid(row=2, column=3, padx=(0, 8), pady=(3, 6), sticky="ew")
        # Hidden by default — only OPEN tasktype needs a requestor
        self._zzcontpers_label.grid_remove()
        self._zzcontpers_entry.grid_remove()

        btn_row = tk.Frame(assign_frame, bg="#f5f5f5")
        btn_row.grid(row=2, column=4, columnspan=2, padx=(0, 8), pady=(3, 6), sticky="e")

        tk.Button(btn_row, text="Favorites ▾", command=self._show_favorites_menu,
                  relief="flat", padx=8, pady=3,
                  font=("Segoe UI", 9)).pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="Apply", command=self._on_apply,
                  bg=COL_ACCENT, fg="white", relief="flat", padx=10, pady=3,
                  font=("Segoe UI", 9)).pack(side="left", padx=(0, 6))
        if not self._multi_date:
            tk.Button(btn_row, text="Copy to days…", command=self._on_copy_to_days,
                      relief="flat", padx=8, pady=3,
                      font=("Segoe UI", 9)).pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="Skip event", command=self._on_skip,
                  bg="#888", fg="white", relief="flat", padx=10, pady=3,
                  font=("Segoe UI", 9)).pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="Skip always", command=self._on_skip_always,
                  bg="#555", fg="white", relief="flat", padx=10, pady=3,
                  font=("Segoe UI", 9)).pack(side="left")

        # ── Footer ────────────────────────────────────────────────────────────
        footer = tk.Frame(self.top, bg="white", pady=8)
        footer.grid(row=3, column=0, sticky="ew", padx=10)
        footer.columnconfigure(0, weight=1)

        self._total_label = tk.Label(
            footer, text="", bg="white",
            font=("Segoe UI", 9), anchor="w",
        )
        self._total_label.grid(row=0, column=0, sticky="w", padx=4)

        tk.Label(
            footer,
            text="⚠ Yellow rows have no keyword match — defaulting to cost center.",
            bg="white", fg="#856404",
            font=("Segoe UI", 8),
        ).grid(row=1, column=0, sticky="w", padx=4, pady=(0, 4))

        tk.Button(
            footer, text="＋ Add manual entry",
            command=self._on_add_entry,
            relief="flat", padx=8, pady=2,
            font=("Segoe UI", 9), fg=COL_ACCENT, bg="white",
            cursor="hand2",
        ).grid(row=2, column=0, sticky="w", padx=2, pady=(0, 4))

        btn_frame = tk.Frame(footer, bg="white")
        btn_frame.grid(row=0, column=1, rowspan=2, padx=4)

        tk.Button(
            btn_frame, text="Cancel",
            command=self._on_cancel,
            relief="flat", padx=16, pady=5,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            btn_frame,
            text="Close Preview" if self._preview_only else "Confirm & Post",
            command=self._on_cancel if self._preview_only else self._on_confirm,
            bg="#555555" if self._preview_only else "#0070d2",
            fg="white",
            relief="flat", padx=16, pady=5,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

    # ── Tree population ───────────────────────────────────────────────────────

    def _refresh_tree(self, selected_idx: int | None = None):
        self._tree.delete(*self._tree.get_children())
        self._iid_to_idx: dict[str, int] = {}

        for idx, row in enumerate(self._rows):
            # ── Date group separator (multi-date mode) ────────────────────────
            if row.get("_separator"):
                d = row["date"]
                day_h = sum(
                    r["event"]["duration_hours"]
                    for r in self._rows
                    if not r.get("_separator")
                    and r.get("date") == d
                    and not r["skipped"]
                )
                already_h = self._existing_hours.get(d.strftime("%Y%m%d"), 0.0)
                total_h   = day_h + already_h

                if already_h > 0:
                    day_label = (
                        f"─── {d.strftime('%A, %d %b %Y')}  —  "
                        f"{day_h:.2f} h new  +  {already_h:.2f} h posted  =  {total_h:.2f} h"
                    )
                else:
                    day_label = f"─── {d.strftime('%A, %d %b %Y')}  —  {day_h:.2f} h"

                # Status indicator based on combined total vs configured daily target
                _tgt = float(self._config.get("daily_hours_target", 8.0))
                if total_h > _tgt + 0.05:
                    day_label += f"  ⚠ {total_h - _tgt:.2f} h OVER"
                elif total_h >= _tgt - 0.05:
                    day_label += "  ✓"
                elif total_h > 0:
                    day_label += f"  ·  ⚠ {_tgt - total_h:.2f} h short"
                day_label += "  ───"
                self._tree.insert(
                    "", "end",
                    iid=f"sep-{idx}-{d.isoformat()}",
                    values=(day_label, "", "", ""),
                    tags=("separator",),
                )
                # NOT added to _iid_to_idx → clicking returns None from _selected_idx()
                continue

            ev      = row["event"]
            mapping = row["mapping"]
            skipped = row["skipped"]

            start_str = self._format_time(ev.get("start", ""))
            hours_str = f"{ev['duration_hours']:.2f}"
            map_label = mapping.get("label", "—") if mapping else "—"

            is_tentative = ev.get("response_type") == "tentative"

            if skipped:
                tag = "skipped"
                map_label = f"[skipped]  {map_label}"
            elif row.get("manual"):
                tag = "manual"
            elif is_tentative:
                tag = "tentative"
            elif not row.get("_mapped", idx < self._unmapped_start):
                tag = "unmapped"
            else:
                tag = "mapped"

            # Use ltxa1 override when set (user may have edited the description)
            effective_subj = (row.get("ltxa1") or ev["subject"])
            subj_display = (
                f"[tentative] {effective_subj[:67]}"
                if is_tentative
                else effective_subj[:80]
            )

            iid = self._tree.insert(
                "", "end",
                values=(subj_display, start_str, hours_str, map_label),
                tags=(tag,),
            )
            self._iid_to_idx[iid] = idx

        # Restore selection if requested (e.g. after Apply)
        if selected_idx is not None:
            for iid, i in self._iid_to_idx.items():
                if i == selected_idx:
                    self._tree.selection_set(iid)
                    self._tree.see(iid)
                    break

    def _format_time(self, dt_str: str) -> str:
        if not dt_str:
            return "—"
        try:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            return dt.strftime("%H:%M")
        except Exception:
            return dt_str[11:16] if len(dt_str) >= 16 else dt_str

    # ── Selection handling ────────────────────────────────────────────────────

    def _selected_idx(self) -> Optional[int]:
        indices = self._selected_indices()
        return indices[0] if indices else None

    def _selected_indices(self) -> list:
        """All currently selected row-model indices, excluding separator rows."""
        return [
            self._iid_to_idx[iid]
            for iid in self._tree.selection()
            if iid in self._iid_to_idx
        ]

    # ── Task-type / sub-type helpers ──────────────────────────────────────────

    def _tasktype_data(self) -> dict:
        """Return task type dict from config or built-in fallback."""
        from catxt_core import get_task_types
        return get_task_types(self._config)

    def _tasktype_code(self) -> str:
        """Extract code from 'MEET - Meetings' label format."""
        val = self._tasktype_var.get()
        return val.split(" - ")[0] if " - " in val else val

    def _subtypes_for(self, code: str) -> list[str]:
        """Return sub-type display labels for a task type code."""
        data   = self._tasktype_data()
        subs   = data.get(code, {}).get("subtypes", [])
        labels = ["(none — optional)"] + [
            f"{s['code']} - {s['label']}" if s.get("code") else s.get("label", "")
            for s in subs if s.get("label")
        ]
        return labels

    def _subtype_code(self) -> str:
        """Extract sub-type code from dropdown value; return '' for 'none'."""
        val = self._subtype_var.get()
        if not val or val.startswith("(none"):
            return ""
        return val.split(" - ")[0] if " - " in val else val

    def _update_requestor_visibility(self):
        """Show the Requestor (User ID) field only when OPEN tasktype is selected."""
        if self._tasktype_code() == "OPEN":
            self._zzcontpers_label.grid()
            self._zzcontpers_entry.grid()
        else:
            self._zzcontpers_label.grid_remove()
            self._zzcontpers_entry.grid_remove()

    def _on_tasktype_changed(self, _event=None):
        """Repopulate sub-type dropdown when task type changes."""
        code    = self._tasktype_code()
        options = self._subtypes_for(code)
        self._subtype_cb.configure(values=options, state="readonly" if len(options) > 1 else "disabled")
        self._subtype_var.set(options[0] if options else "")
        self._update_requestor_visibility()

    # ── Favorites ─────────────────────────────────────────────────────────────

    def _show_favorites_menu(self):
        favorites = self._config.get("_favorites", [])
        if not favorites:
            tk.messagebox.showinfo(
                "Favorites", "No favorites found.\n\nCreate favorites in the CATXT web app.",
                parent=self.top)
            return
        menu = tk.Menu(self.top, tearoff=0)
        for fav in favorites:
            label = fav.get("label") or fav.get("ltxa1") or fav.get("tasktype", "?")
            # Capture fav in closure
            menu.add_command(
                label=label,
                command=lambda f=fav: self._apply_favorite(f),
            )
        try:
            btn_widget = self.top.focus_get()
            menu.tk_popup(
                self.top.winfo_pointerx(),
                self.top.winfo_pointery(),
            )
        finally:
            menu.grab_release()

    def _apply_favorite(self, fav: dict):
        """Apply a favorite's task type, sub-type, and description to selected row."""
        idx = self._selected_idx()
        if idx is None:
            return
        tt   = fav.get("tasktype", "")
        sub  = fav.get("zzsubtype", "")
        ltxa1 = fav.get("ltxa1", "")

        # Set task type dropdown
        data = self._tasktype_data()
        if tt in data:
            self._tasktype_var.set(f"{tt} - {data[tt]['label']}")
            self._on_tasktype_changed()

        # Set sub-type
        options = self._subtypes_for(tt)
        matched = next((o for o in options if o.startswith(sub + " - ") or o == sub), "")
        if matched:
            self._subtype_var.set(matched)

        # Apply to row
        self._rows[idx]["tasktype"]  = tt
        self._rows[idx]["zzsubtype"] = sub
        if ltxa1:
            self._rows[idx]["ltxa1"] = ltxa1
        self._refresh_tree()
        self._refresh_footer()

    # ── Selection handling ────────────────────────────────────────────────────

    def _on_select(self, _event=None):
        if self._selecting:
            return

        sel      = self._tree.selection()
        sel_idxs = [self._iid_to_idx[s] for s in sel if s in self._iid_to_idx]
        new_idx  = sel_idxs[0] if sel_idxs else None

        # ── Auto-apply: commit panel to previous single-row selection ──────────
        if (self._prev_idx is not None and new_idx is not None
                and self._prev_idx != new_idx and len(sel_idxs) == 1):
            self._auto_apply(self._prev_idx)
            self._selecting = True
            try:
                self._refresh_tree(selected_idx=new_idx)
                self._refresh_footer()
            finally:
                self._selecting = False

        self._prev_idx = new_idx if len(sel_idxs) == 1 else None

        # ── Load panel from first selected row ────────────────────────────────
        if new_idx is None:
            self._desc_var.set("")
            self._desc_entry.config(state="disabled")
            self._zzcontpers_var.set("")
            self._zzcontpers_label.grid_remove()
            self._zzcontpers_entry.grid_remove()
            self._assign_var.set("")
            self._tasktype_var.set("")
            self._subtype_var.set("")
            return

        row     = self._rows[new_idx]
        ev      = row["event"]
        mapping = row["mapping"]
        multi   = len(sel_idxs) > 1

        if multi:
            self._desc_var.set(f"({len(sel_idxs)} rows — description edit not available for multi-select)")
            self._desc_entry.config(state="disabled")
            self._zzcontpers_var.set("")
            self._hours_var.set("")   # hours differ per event; skip on batch apply
        else:
            self._desc_entry.config(state="normal")
            self._desc_var.set((row.get("ltxa1") or ev["subject"])[:40])
            # Use row-level value if set, else fall back to saved default
            self._zzcontpers_var.set(
                row.get("zzcontpers") or self._config.get("requestor_default", "")
            )
            self._hours_var.set(f"{ev['duration_hours']:.2f}")

        self._assign_var.set(mapping.get("label", self._default_label) if mapping else "")

        # Task type dropdown — project mappings always default to CFPP
        tt   = row.get("tasktype", "")
        data = self._tasktype_data()
        if mapping and mapping.get("rproj"):
            tt = "CFPP"
        if tt in data:
            self._tasktype_var.set(f"{tt} - {data[tt]['label']}")
        else:
            self._tasktype_var.set(tt)

        # Sub-type dropdown
        options = self._subtypes_for(tt)
        self._subtype_cb.configure(
            values=options,
            state="readonly" if len(options) > 1 else "disabled",
        )
        sub = row.get("zzsubtype", "")
        matched = next((o for o in options if o.startswith(sub + " - ") or o == sub), options[0] if options else "")
        self._subtype_var.set(matched)
        self._update_requestor_visibility()

    # ── Assignment / skip actions ─────────────────────────────────────────────

    def _on_apply(self):
        indices = self._selected_indices()
        if not indices:
            return
        label = self._assign_var.get()
        if label:
            if label == self._default_label:
                new_mapping = self._config.get("default_mapping") or {}
            else:
                new_mapping = self._label_to_mapping.get(label)
        else:
            new_mapping = None
        for idx in indices:
            if new_mapping is not None:
                self._rows[idx]["mapping"] = new_mapping
            self._rows[idx]["tasktype"]  = self._tasktype_code()
            self._rows[idx]["zzsubtype"] = self._subtype_code()
            self._rows[idx]["skipped"]   = False
        # Hours + description + requestor — single-row only
        if len(indices) == 1:
            try:
                h = float(self._hours_var.get())
                if h > 0:
                    self._rows[indices[0]]["event"]["duration_hours"] = h
            except ValueError:
                pass
            desc = self._desc_var.get().strip()[:40]
            if desc:
                self._rows[indices[0]]["ltxa1"] = desc
            zzc = self._zzcontpers_var.get().strip()
            self._rows[indices[0]]["zzcontpers"] = zzc
            # Persist as default so the user doesn't have to re-enter it
            if zzc and zzc != self._config.get("requestor_default", ""):
                self._config["requestor_default"] = zzc
                try:
                    from catxt_core import save_config
                    save_config(self._config)
                except Exception:
                    pass
        self._refresh_tree(selected_idx=indices[0])
        self._refresh_footer()

    def _auto_apply(self, idx: int):
        """Silently commit the current assignment panel to row idx (no tree refresh)."""
        if idx is None or idx >= len(self._rows):
            return
        row = self._rows[idx]
        if row.get("_separator"):
            return
        label = self._assign_var.get()
        if label:
            if label == self._default_label:
                new_mapping = self._config.get("default_mapping") or {}
            else:
                new_mapping = self._label_to_mapping.get(label)
            if new_mapping is not None:
                row["mapping"] = new_mapping
        row["tasktype"]  = self._tasktype_code()
        row["zzsubtype"] = self._subtype_code()
        try:
            h = float(self._hours_var.get())
            if h > 0:
                row["event"]["duration_hours"] = h
        except ValueError:
            pass
        if self._desc_entry.cget("state") != "disabled":
            desc = self._desc_var.get().strip()[:40]
            if desc:
                row["ltxa1"] = desc
        row["zzcontpers"] = self._zzcontpers_var.get().strip()

    def _on_copy_to_days(self):
        """Queue the current entry to be posted to user-selected dates."""
        import copy as _copy
        idx = self._selected_idx()
        if idx is None:
            messagebox.showwarning("No Selection", "Select an event first.", parent=self.top)
            return

        # Build mapping from current UI state (mirrors _on_apply logic)
        label = self._assign_var.get()
        if label and label == self._default_label:
            mapping = _copy.copy(self._config.get("default_mapping") or {})
        elif label:
            mapping = _copy.copy(self._label_to_mapping.get(label) or {})
        else:
            mapping = _copy.copy(self._rows[idx]["mapping"])
        mapping["tasktype"]  = self._tasktype_code()
        mapping["zzsubtype"] = self._subtype_code()

        # Snapshot the event with any hours override applied
        ev = _copy.copy(self._rows[idx]["event"])
        try:
            h = float(self._hours_var.get())
            if h > 0:
                ev["duration_hours"] = h
        except ValueError:
            pass

        # Open non-contiguous date picker
        picker = _PickDatesDialog(self.top)
        if picker.result is None:
            return
        dates = picker.result   # list[date], already filtered to business days

        self.copies.append({"event": ev, "mapping": mapping, "dates": dates})
        n = len(dates)
        messagebox.showinfo(
            "Queued",
            f"Entry queued for {n} date{'s' if n != 1 else ''}.\n"
            f"Will post when you confirm the review.",
            parent=self.top,
        )

    def _on_skip(self):
        idx = self._selected_idx()
        if idx is None:
            return
        self._rows[idx]["skipped"] = not self._rows[idx]["skipped"]
        self._refresh_tree()
        self._refresh_footer()

    def _on_skip_always(self):
        """Skip this event for this session AND record it so it never appears again."""
        idx = self._selected_idx()
        if idx is None:
            return
        row = self._rows[idx]
        if row.get("_separator"):
            return
        row["skipped"] = True
        event_id   = row["event"]["id"]
        event_date = row.get("date") or self._target_date
        pair = (event_date, event_id)
        if pair not in self.permanently_skipped:
            self.permanently_skipped.append(pair)
        self._refresh_tree()
        self._refresh_footer()

    # ── Footer totals ─────────────────────────────────────────────────────────

    def _refresh_footer(self):
        real_rows = [r for r in self._rows if not r.get("_separator")]
        active    = [r for r in real_rows if not r["skipped"]]
        total_h   = sum(r["event"]["duration_hours"] for r in active)
        skipped   = len(real_rows) - len(active)

        if self._multi_date:
            days_with_entries = {r["date"] for r in active if "date" in r}
            text = (f"{len(active)} event(s)  |  {total_h:.2f} h total  |  "
                    f"{len(days_with_entries)} day(s)")
        else:
            text = f"{len(active)} event(s)  |  {total_h:.2f} h total"

        if skipped:
            text += f"  |  {skipped} skipped"
        self._total_label.config(text=text)

    # ── Confirm / cancel ──────────────────────────────────────────────────────

    def _on_confirm(self):
        # Flush any unsaved panel edits for the currently-selected row so that
        # clicking "Confirm & Post" without first clicking Apply still captures
        # changes made in the Description / Requestor fields.
        if self._prev_idx is not None:
            self._auto_apply(self._prev_idx)
        import copy
        if self._multi_date:
            # Multi-date: result is dict[date, list[(event, mapping)]]
            result: dict = {}
            for r in self._rows:
                if r.get("_separator") or r["skipped"] or not r["mapping"]:
                    continue
                m = copy.copy(r["mapping"])
                m["tasktype"]  = r.get("tasktype")  or m.get("tasktype", "")
                m["zzsubtype"] = r.get("zzsubtype") or ""
                if r.get("ltxa1"):
                    m["_ltxa1_override"] = r["ltxa1"]
                if r.get("zzcontpers"):
                    m["_zzcontpers"] = r["zzcontpers"]
                result.setdefault(r["date"], []).append((r["event"], m))
            self.result = result
        else:
            # Single-date: result is list[(event, mapping)]
            to_post = []
            for r in self._rows:
                if r["skipped"] or not r["mapping"]:
                    continue
                m = copy.copy(r["mapping"])
                m["tasktype"]  = r.get("tasktype")  or m.get("tasktype", "")
                m["zzsubtype"] = r.get("zzsubtype") or ""
                if r.get("ltxa1"):
                    m["_ltxa1_override"] = r["ltxa1"]
                if r.get("zzcontpers"):
                    m["_zzcontpers"] = r["zzcontpers"]
                to_post.append((r["event"], m))
            self.result = to_post
        self.top.destroy()

    def _on_cancel(self):
        self.result = None
        self.top.destroy()

    # ── Manual entry ──────────────────────────────────────────────────────────

    def _on_add_entry(self):
        """Open the manual entry form and append the result as new row(s)."""
        import time as _t, copy as _copy

        # Always open with target_date as the pre-populated default.
        # The Date(s) field is always visible so the user can change/add dates.
        dlg = _ManualEntryDialog(
            self.top,
            all_labels=self._all_labels,
            default_label=self._default_label,
            config=self._config,
            target_date=self._target_date,
        )

        if dlg.result is None:
            return
        r = dlg.result

        # Dates come from the Date(s) field (always a list now)
        target_dates = r.get("target_dates") or [r.get("target_date") or self._target_date]

        ts = int(_t.time() * 1000)
        queued_copies = []

        for i, entry_date in enumerate(target_dates):
            event = {
                "id":              f"manual-{ts}-{i}",
                "subject":         r["desc"],
                "start":           entry_date.strftime("%Y-%m-%dT09:00:00"),
                "duration_hours":  r["hours"],
                "organiser_email": "",
                "organised_by_me": True,
            }
            new_row = {
                "event":     event,
                "mapping":   r["mapping"],
                "skipped":   False,
                "tasktype":  r["tasktype"],
                "zzsubtype": r["zzsubtype"],
                "ltxa1":     r["desc"],
                "manual":    True,
                "date":      entry_date,
                "_mapped":   True,
            }

            if self._multi_date:
                # Insert after the last existing row for that date
                insert_at = len(self._rows)
                for j in range(len(self._rows) - 1, -1, -1):
                    rr = self._rows[j]
                    if not rr.get("_separator") and rr.get("date") == entry_date:
                        insert_at = j + 1
                        break
                self._rows.insert(insert_at, new_row)
            elif entry_date == self._target_date:
                # Single-date mode: entry for the current review date → show in review
                self._rows.append(new_row)
            else:
                # Single-date mode: different date → queue via copies mechanism
                m = _copy.copy(r["mapping"])
                m["tasktype"]      = r["tasktype"]
                m["zzsubtype"]     = r["zzsubtype"]
                m["_ltxa1_override"] = r["desc"]
                self.copies.append({"event": event, "mapping": m, "dates": [entry_date]})
                queued_copies.append(entry_date)

        if queued_copies:
            n = len(queued_copies)
            date_strs = ", ".join(d.strftime("%Y-%m-%d") for d in queued_copies)
            messagebox.showinfo(
                "Entries Queued",
                f"{n} entr{'ies' if n > 1 else 'y'} queued for dates outside this review:\n"
                f"{date_strs}\n\nThey will post automatically when you confirm.",
                parent=self.top,
            )

        self._refresh_tree()
        self._refresh_footer()


# ══════════════════════════════════════════════════════════════════════════════
# Manual entry form
# ══════════════════════════════════════════════════════════════════════════════

class _PickDatesDialog:
    """
    Date picker that accepts non-contiguous dates entered as a comma-separated list.
    Quick-fill buttons provide shortcuts for common selections (this week, next week).

    result — sorted list[date] of business days, or None if the user cancelled.
    """

    def __init__(self, parent):
        from datetime import timedelta
        self.result = None

        # Default: next business day
        d = date.today() + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        default_str = d.strftime("%Y-%m-%d")

        top = tk.Toplevel(parent)
        top.title("Pick Dates")
        top.resizable(False, False)
        top.grab_set()
        top.protocol("WM_DELETE_WINDOW", top.destroy)
        try:
            top.iconbitmap(_ICON_PATH)
        except Exception:
            pass

        hdr = tk.Frame(top, bg=COL_HDR_BG, padx=12, pady=8)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Copy entry to specific dates",
                 bg=COL_HDR_BG, fg=COL_HDR_FG,
                 font=("Segoe UI", 11, "bold")).pack(side="left")

        form = tk.Frame(top, bg="white", padx=20, pady=12)
        form.pack(fill="both", expand=True)
        form.columnconfigure(0, weight=1)

        tk.Label(form, text="Enter dates separated by commas (YYYY-MM-DD):",
                 bg="white", font=("Segoe UI", 9), anchor="w",
                 ).pack(fill="x", pady=(0, 4))

        self._text = tk.Text(form, font=("Segoe UI", 9), height=4, width=50,
                             wrap="word", relief="solid", borderwidth=1)
        self._text.insert("1.0", default_str)
        self._text.pack(fill="x", pady=(0, 4))

        tk.Label(form, text="Weekends are automatically skipped.",
                 bg="white", fg="#888", font=("Segoe UI", 8), anchor="w",
                 ).pack(fill="x", pady=(0, 8))

        quick = tk.Frame(form, bg="white")
        quick.pack(fill="x", pady=(0, 4))
        tk.Label(quick, text="Quick fill:", bg="white",
                 font=("Segoe UI", 8)).pack(side="left")
        for label, fn in [
            ("This week",  self._fill_this_week),
            ("Next week",  self._fill_next_week),
            ("Today",      self._fill_today),
        ]:
            tk.Button(quick, text=label, command=fn,
                      relief="flat", padx=6, pady=1,
                      font=("Segoe UI", 8)).pack(side="left", padx=(4, 0))

        btn_frame = tk.Frame(top, bg="#f0f0f0", padx=12, pady=8)
        btn_frame.pack(fill="x")
        tk.Button(btn_frame, text="Cancel", command=top.destroy,
                  relief="flat", padx=10, pady=3,
                  font=("Segoe UI", 9)).pack(side="right", padx=(6, 0))
        tk.Button(btn_frame, text="Copy", command=lambda: self._confirm(top),
                  bg=COL_ACCENT, fg="white", relief="flat", padx=10, pady=3,
                  font=("Segoe UI", 9)).pack(side="right")

        top.update_idletasks()
        x = (top.winfo_screenwidth()  - top.winfo_reqwidth())  // 2
        y = (top.winfo_screenheight() - top.winfo_reqheight()) // 2
        top.geometry(f"+{x}+{y}")
        top.wait_window()

    def _fill_this_week(self):
        from datetime import timedelta
        today = date.today()
        mon   = today - timedelta(days=today.weekday())
        dates = [mon + timedelta(days=i) for i in range(5)]
        self._text.delete("1.0", "end")
        self._text.insert("1.0", ", ".join(d.strftime("%Y-%m-%d") for d in dates))

    def _fill_next_week(self):
        from datetime import timedelta
        today = date.today()
        mon   = today - timedelta(days=today.weekday()) + timedelta(weeks=1)
        dates = [mon + timedelta(days=i) for i in range(5)]
        self._text.delete("1.0", "end")
        self._text.insert("1.0", ", ".join(d.strftime("%Y-%m-%d") for d in dates))

    def _fill_today(self):
        self._text.delete("1.0", "end")
        self._text.insert("1.0", date.today().strftime("%Y-%m-%d"))

    def _confirm(self, top):
        # Accept comma or whitespace as separators
        raw = self._text.get("1.0", "end").replace(",", " ")
        dates = []
        for part in raw.split():
            if not part:
                continue
            try:
                dates.append(date.fromisoformat(part))
            except ValueError:
                messagebox.showwarning(
                    "Invalid Date",
                    f"'{part}' is not a valid date.\nUse YYYY-MM-DD format (e.g. 2026-08-21).",
                    parent=top,
                )
                return
        if not dates:
            messagebox.showwarning("No Dates", "Enter at least one date.", parent=top)
            return
        # Deduplicate, sort, and filter out weekends
        dates = sorted({d for d in dates if d.weekday() < 5})
        if not dates:
            messagebox.showwarning("No Business Days",
                                   "None of the entered dates are business days.", parent=top)
            return
        self.result = dates
        top.destroy()


class _ManualEntryDialog:
    """
    Small modal form for creating a time entry that has no calendar source.

    Parameters
    ----------
    target_date : date or None
        When None the dialog is in *standalone* mode — a Date field is shown so
        the user can choose which day to charge.  When a date is supplied (called
        from ReviewDialog) the date field is hidden and the value is used as-is.

    result — dict {desc, hours, mapping, tasktype, zzsubtype, target_date} or
             None if the user cancelled.  target_date is None when the dialog was
             opened from within ReviewDialog (caller owns the date).
    """

    def __init__(self, parent, all_labels: list, default_label: str, config: dict,
                 target_date=None):
        self.result      = None
        self._config     = config
        self._all_labels = all_labels

        top = tk.Toplevel(parent)
        top.title("Add Manual Time Entry")
        top.resizable(False, False)
        top.grab_set()
        top.protocol("WM_DELETE_WINDOW", top.destroy)
        try:
            top.iconbitmap(_ICON_PATH)
        except Exception:
            pass

        # Header strip
        hdr = tk.Frame(top, bg=COL_HDR_BG, padx=12, pady=8)
        hdr.pack(fill="x")
        tk.Label(
            hdr, text="Add Manual Time Entry",
            bg=COL_HDR_BG, fg=COL_HDR_FG,
            font=("Segoe UI", 11, "bold"),
        ).pack(side="left")

        # Form
        form = tk.Frame(top, bg="white", padx=20, pady=12)
        form.pack(fill="both", expand=True)
        form.columnconfigure(1, weight=1)

        def lbl(text, row):
            tk.Label(
                form, text=text, bg="white",
                font=("Segoe UI", 9), anchor="e", width=13,
            ).grid(row=row, column=0, padx=(0, 8), pady=4, sticky="e")

        r = 0  # dynamic row counter

        # ── Date(s) — always shown; target_date used as the default ───────────
        lbl("Date(s):", r)
        self._dates_var = tk.StringVar(
            value=(target_date or date.today()).strftime("%Y-%m-%d")
        )
        tk.Entry(form, textvariable=self._dates_var,
                 font=("Segoe UI", 9), width=40,
                 ).grid(row=r, column=1, pady=4, sticky="ew")
        r += 1
        tk.Label(form,
                 text="Comma or space-separated YYYY-MM-DD dates for multi-day entries",
                 bg="white", fg="#888", font=("Segoe UI", 8),
                 ).grid(row=r, column=1, sticky="w", pady=(0, 4))
        r += 1

        # ── Description ───────────────────────────────────────────────────────
        lbl("Description:", r)
        self._desc = tk.Entry(form, font=("Segoe UI", 9), width=36)
        self._desc.grid(row=r, column=1, pady=4, sticky="ew")
        r += 1
        tk.Label(
            form, text="Max 40 characters — becomes the CATXT entry text.",
            bg="white", fg="#888", font=("Segoe UI", 8),
        ).grid(row=r, column=1, sticky="w", pady=(0, 4))
        r += 1

        # ── Hours ─────────────────────────────────────────────────────────────
        lbl("Hours:", r)
        self._hours_var = tk.StringVar(value="0.50")
        tk.Spinbox(
            form, from_=0.25, to=24.0, increment=0.25,
            textvariable=self._hours_var, width=8,
            font=("Segoe UI", 9), format="%.2f",
        ).grid(row=r, column=1, sticky="w", pady=4)
        r += 1

        # ── Assign to ─────────────────────────────────────────────────────────
        lbl("Assign to:", r)
        self._assign_var = tk.StringVar(value=default_label)
        ttk.Combobox(
            form, textvariable=self._assign_var,
            values=all_labels, state="readonly", width=34,
        ).grid(row=r, column=1, sticky="ew", pady=4)
        r += 1

        # ── Task Type ─────────────────────────────────────────────────────────
        lbl("Task Type:", r)
        from catxt_core import get_task_types
        self._task_data = get_task_types(config)
        tt_labels = [f"{c} - {v['label']}" for c, v in self._task_data.items()]
        cur_tt = config.get("default_mapping", {}).get("tasktype", "MEET")
        cur_tt_display = next(
            (f"{c} - {v['label']}" for c, v in self._task_data.items() if c == cur_tt),
            cur_tt,
        )
        self._tasktype_var = tk.StringVar(value=cur_tt_display)
        self._tasktype_cb = ttk.Combobox(
            form, textvariable=self._tasktype_var,
            values=tt_labels, state="readonly", width=34,
        )
        self._tasktype_cb.grid(row=r, column=1, sticky="ew", pady=4)
        self._tasktype_cb.bind("<<ComboboxSelected>>", lambda _e: self._update_subtypes())
        r += 1

        # ── Sub-type ──────────────────────────────────────────────────────────
        lbl("Sub-type:", r)
        self._subtype_var = tk.StringVar()
        self._subtype_cb = ttk.Combobox(
            form, textvariable=self._subtype_var,
            values=[], state="disabled", width=34,
        )
        self._subtype_cb.grid(row=r, column=1, sticky="ew", pady=4)
        r += 1
        tk.Label(
            form, text="Optional — leave blank if not needed.",
            bg="white", fg="#888", font=("Segoe UI", 8),
        ).grid(row=r, column=1, sticky="w", pady=(0, 8))
        self._update_subtypes()

        # ── Favorites + action buttons ────────────────────────────────────────
        btn_frame = tk.Frame(top, bg="white", padx=20, pady=10)
        btn_frame.pack(fill="x")
        tk.Button(
            btn_frame, text="Favorites ▾",
            command=lambda: self._show_favorites_menu(top),
            relief="flat", padx=10, pady=4, font=("Segoe UI", 9),
        ).pack(side="left")
        tk.Button(
            btn_frame, text="Cancel", command=top.destroy,
            relief="flat", padx=12, pady=4, font=("Segoe UI", 9),
        ).pack(side="right", padx=(6, 0))
        tk.Button(
            btn_frame, text="Add Entry",
            command=lambda: self._confirm(top),
            bg=COL_ACCENT, fg="white", relief="flat",
            padx=12, pady=4, font=("Segoe UI", 9, "bold"),
        ).pack(side="right")

        top.update_idletasks()
        x = (top.winfo_screenwidth()  - top.winfo_reqwidth())  // 2
        y = (top.winfo_screenheight() - top.winfo_reqheight()) // 2
        top.geometry(f"+{x}+{y}")
        self._desc.focus_set()
        parent.wait_window(top)

    def _show_favorites_menu(self, top: tk.Toplevel):
        favorites = self._config.get("_favorites", [])
        if not favorites:
            messagebox.showinfo(
                "Favorites",
                "No favorites found.\n\nCreate favorites in the CATXT web app first,\n"
                "then run a sync so the app fetches them.",
                parent=top,
            )
            return
        menu = tk.Menu(top, tearoff=0)
        for fav in favorites:
            label = fav.get("label") or fav.get("ltxa1") or fav.get("tasktype", "?")
            menu.add_command(label=label,
                             command=lambda f=fav: self._apply_favorite(f))
        try:
            menu.tk_popup(top.winfo_pointerx(), top.winfo_pointery())
        finally:
            menu.grab_release()

    def _apply_favorite(self, fav: dict):
        tt    = fav.get("tasktype", "")
        sub   = fav.get("zzsubtype", "")
        ltxa1 = fav.get("ltxa1", "")

        # Fill description if favorite has one
        if ltxa1:
            self._desc.delete(0, "end")
            self._desc.insert(0, ltxa1[:40])

        # Set task type
        if tt in self._task_data:
            self._tasktype_var.set(f"{tt} - {self._task_data[tt]['label']}")
            self._update_subtypes()

        # Set sub-type
        if sub:
            subs = self._task_data.get(tt, {}).get("subtypes", [])
            matched = next(
                (f"{s['code']} - {s['label']}" for s in subs if s.get("code") == sub),
                None,
            )
            if matched:
                self._subtype_var.set(matched)

    def _update_subtypes(self):
        val  = self._tasktype_var.get()
        code = val.split(" - ")[0] if " - " in val else val
        subs = self._task_data.get(code, {}).get("subtypes", [])
        options = ["(none — optional)"] + [
            f"{s['code']} - {s['label']}" if s.get("code") else s.get("label", "")
            for s in subs
            if s.get("label")
        ]
        self._subtype_cb.configure(
            values=options,
            state="readonly" if len(options) > 1 else "disabled",
        )
        self._subtype_var.set(options[0])

    def _confirm(self, top: tk.Toplevel):
        # Parse Date(s) field — always present; accepts commas or whitespace
        target_dates = []
        raw = self._dates_var.get().replace(",", " ")
        for part in raw.split():
            if not part:
                continue
            try:
                target_dates.append(date.fromisoformat(part))
            except ValueError:
                messagebox.showwarning(
                    "Invalid Date",
                    f"'{part}' is not a valid date.\nUse YYYY-MM-DD format (e.g. 2026-08-04).",
                    parent=top,
                )
                return
        if not target_dates:
            messagebox.showwarning("Required", "Enter at least one date.", parent=top)
            return
        entry_date = target_dates[0]

        desc = self._desc.get().strip()[:40]
        if not desc:
            messagebox.showwarning("Required", "Please enter a description.", parent=top)
            return
        try:
            hours = round(float(self._hours_var.get()), 2)
            if hours <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Invalid", "Hours must be a positive number.", parent=top)
            return

        # Resolve mapping from label
        assign_label = self._assign_var.get()
        label_to_m: dict = {}
        for m in self._config.get("wbs_mappings", []):
            if m.get("label"):
                label_to_m[m["label"]] = m
        dm = self._config.get("default_mapping") or {}
        if dm.get("label"):
            label_to_m[dm["label"]] = dm
        mapping = label_to_m.get(assign_label) or dm

        # Resolve task type + sub-type codes
        tt_val   = self._tasktype_var.get()
        tasktype = tt_val.split(" - ")[0] if " - " in tt_val else tt_val
        sub_val  = self._subtype_var.get()
        if sub_val.startswith("(none") or not sub_val:
            zzsubtype = ""
        elif " - " in sub_val:
            zzsubtype = sub_val.split(" - ")[0]
        else:
            zzsubtype = sub_val

        self.result = {
            "desc":         desc,
            "hours":        hours,
            "mapping":      mapping,
            "tasktype":     tasktype,
            "zzsubtype":    zzsubtype,
            "target_date":  entry_date,    # first date (for backward compat)
            "target_dates": target_dates,  # full list — always set
        }
        top.destroy()
