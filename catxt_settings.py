"""
catxt_settings.py — Settings dialog for CATXT Sync
====================================================
Full tkinter editor for config.json.  Opened from the tray icon menu.

Tabs
----
  1. WBS Mappings   — add/edit/delete keyword-to-project rules
  2. Default Mapping — default cost center used when no rule matches
  3. Excluded Keywords — subjects that are always skipped

Returns the modified config dict on save, or None if cancelled.
"""

import copy
import os as _os
import subprocess
import sys
import sys as _sys
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional

from catxt_core import save_config as _save_config

_ICON_PATH = _os.path.join(
    getattr(_sys, "_MEIPASS", _os.path.dirname(_os.path.abspath(__file__))),
    "assets", "catxt.ico",
)


COL_HDR_BG = "#003366"
COL_HDR_FG = "#ffffff"
COL_ACCENT = "#0070d2"
COL_GOLD   = "#f0ab00"


class SettingsDialog:
    """
    Modal settings dialog.

    Usage
    -----
        dlg = SettingsDialog(parent, config)
        new_config = dlg.result   # None if cancelled
    """

    def __init__(self, parent: tk.Misc, config: dict):
        self.result: Optional[dict] = None
        self._config = copy.deepcopy(config)   # work on a copy; write back on Save

        self.top = tk.Toplevel(parent)
        try:
            self.top.iconbitmap(_ICON_PATH)
        except Exception:
            pass
        self.top.title("CATXT Sync — Settings")
        self.top.geometry("860x600")
        self.top.minsize(700, 480)
        self.top.resizable(True, True)
        self.top.grab_set()
        self.top.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self._build_ui()

        self.top.update_idletasks()
        x = (self.top.winfo_screenwidth()  - self.top.winfo_width())  // 2
        y = (self.top.winfo_screenheight() - self.top.winfo_height()) // 2
        self.top.geometry(f"+{x}+{y}")

        parent.wait_window(self.top)

    # ── Top-level layout ──────────────────────────────────────────────────────

    def _build_ui(self):
        self.top.columnconfigure(0, weight=1)
        self.top.rowconfigure(1, weight=1)

        # Header
        hdr = tk.Frame(self.top, bg=COL_HDR_BG, padx=12, pady=8)
        hdr.grid(row=0, column=0, sticky="ew")
        tk.Label(
            hdr, text="CATXT Sync — Settings",
            bg=COL_HDR_BG, fg=COL_HDR_FG,
            font=("Segoe UI", 12, "bold"),
        ).pack(side="left")

        # Notebook
        nb = ttk.Notebook(self.top)
        nb.grid(row=1, column=0, sticky="nsew", padx=10, pady=8)
        self._build_tab_general(nb)
        self._build_tab_mappings(nb)
        self._build_tab_auto_mappings(nb)
        self._build_tab_default(nb)
        self._build_tab_excluded(nb)
        self._build_tab_schedule(nb)

        # Footer buttons
        footer = tk.Frame(self.top, bg="white", pady=8)
        footer.grid(row=2, column=0, sticky="ew", padx=10)
        footer.columnconfigure(0, weight=1)

        tk.Button(
            footer, text="Cancel",
            command=self._on_cancel,
            relief="flat", padx=16, pady=5,
            font=("Segoe UI", 9),
        ).grid(row=0, column=1, padx=(0, 6))

        tk.Button(
            footer, text="Save & Close",
            command=self._on_save,
            bg=COL_ACCENT, fg="white",
            relief="flat", padx=16, pady=5,
            font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=2)

    # ══════════════════════════════════════════════════════════════════════════
    # Tab 1 — WBS Mappings
    # ══════════════════════════════════════════════════════════════════════════

    def _build_tab_general(self, nb: ttk.Notebook):
        frame = tk.Frame(nb, bg="white")
        nb.add(frame, text="  General  ")
        frame.columnconfigure(1, weight=1)

        tk.Label(frame, text="General Settings", bg="white",
                 font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 6))

        # Daily hour target
        tk.Label(frame, text="Daily hour target:", bg="white",
                 font=("Segoe UI", 9)).grid(
            row=1, column=0, sticky="w", padx=12, pady=4)

        self._daily_hours_var = tk.StringVar(
            value=str(self._config.get("daily_hours_target", 8))
        )
        spin_row = tk.Frame(frame, bg="white")
        spin_row.grid(row=1, column=1, sticky="w", padx=8, pady=4)
        tk.Spinbox(
            spin_row, from_=1, to=24, increment=0.5,
            textvariable=self._daily_hours_var,
            width=5, font=("Segoe UI", 10),
        ).pack(side="left")
        tk.Label(spin_row, text="hours  (used for short-day warnings)",
                 bg="white", font=("Segoe UI", 9), fg="#666").pack(
            side="left", padx=(6, 0))

        # Scheduled sync max lookback
        tk.Label(frame, text="Scheduled sync lookback:", bg="white",
                 font=("Segoe UI", 9)).grid(
            row=2, column=0, sticky="w", padx=12, pady=4)

        self._lookback_var = tk.StringVar(
            value=str(self._config.get("max_scheduled_lookback_days", 10))
        )
        lb_row = tk.Frame(frame, bg="white")
        lb_row.grid(row=2, column=1, sticky="w", padx=8, pady=4)
        tk.Spinbox(
            lb_row, from_=1, to=30,
            textvariable=self._lookback_var,
            width=4, font=("Segoe UI", 10),
        ).pack(side="left")
        tk.Label(lb_row, text="business days  (how far back scheduled sync looks for unposted entries)",
                 bg="white", font=("Segoe UI", 9), fg="#666").pack(
            side="left", padx=(6, 0))

    # ══════════════════════════════════════════════════════════════════════════

    def _build_tab_mappings(self, nb: ttk.Notebook):
        frame = tk.Frame(nb, bg="white")
        nb.add(frame, text="  WBS Mappings  ")
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(0, weight=1)

        # ── Left: list + add/delete ───────────────────────────────────────────
        left = tk.Frame(frame, bg="white", bd=0)
        left.grid(row=0, column=0, sticky="ns", padx=(8, 0), pady=8)

        tk.Label(
            left, text="Mappings", bg="white",
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w")

        lb_frame = tk.Frame(left, bg="white")
        lb_frame.pack(fill="both", expand=True)

        self._map_lb = tk.Listbox(
            lb_frame, width=28, activestyle="dotbox",
            font=("Segoe UI", 9), selectbackground=COL_ACCENT,
        )
        lb_scroll = ttk.Scrollbar(lb_frame, orient="vertical",
                                   command=self._map_lb.yview)
        self._map_lb.configure(yscrollcommand=lb_scroll.set)
        self._map_lb.pack(side="left", fill="both", expand=True)
        lb_scroll.pack(side="left", fill="y")
        self._map_lb.bind("<<ListboxSelect>>", self._on_map_select)

        btn_row = tk.Frame(left, bg="white")
        btn_row.pack(fill="x", pady=(4, 0))
        tk.Button(
            btn_row, text="+ Add",
            command=self._on_map_add,
            bg=COL_ACCENT, fg="white", relief="flat",
            padx=8, pady=3, font=("Segoe UI", 9),
        ).pack(side="left", padx=(0, 4))
        tk.Button(
            btn_row, text="Delete",
            command=self._on_map_delete,
            bg="#dc3545", fg="white", relief="flat",
            padx=8, pady=3, font=("Segoe UI", 9),
        ).pack(side="left")

        # ── Right: edit form ──────────────────────────────────────────────────
        right = tk.Frame(frame, bg="white")
        right.grid(row=0, column=1, sticky="nsew", padx=8, pady=8)
        right.columnconfigure(1, weight=1)

        def lbl(text, row):
            tk.Label(
                right, text=text, bg="white",
                font=("Segoe UI", 9), anchor="e", width=14,
            ).grid(row=row, column=0, sticky="e", padx=(0, 6), pady=3)

        lbl("Label:",      0); self._m_label   = self._entry(right, 0)
        lbl("WBS:",        1); self._m_wbs     = self._entry(right, 1)
        lbl("Rproj:",      2); self._m_rproj   = self._entry(right, 2)
        lbl("Tasktype:",   3)
        self._m_tasktype_var = tk.StringVar()
        ttk.Combobox(
            right, textvariable=self._m_tasktype_var,
            values=["", "CFPP", "MEET"], state="readonly", width=8,
        ).grid(row=3, column=1, sticky="w", pady=3)

        lbl("Tasklevel:",  4)
        self._m_tasklevel_var = tk.StringVar()
        ttk.Combobox(
            right, textvariable=self._m_tasklevel_var,
            values=["", "G1", "G2", "G3", "G4", "G5"], state="readonly", width=8,
        ).grid(row=4, column=1, sticky="w", pady=3)

        lbl("Sales Doc\n(rkdauf):", 5); self._m_rkdauf = self._entry(right, 5)
        lbl("Sales Pos\n(rkdpos):", 6); self._m_rkdpos = self._entry(right, 6)

        lbl("Keywords\n(one per line):", 7)
        self._m_keywords = tk.Text(right, height=4, width=32,
                                    font=("Segoe UI", 9))
        self._m_keywords.grid(row=7, column=1, sticky="ew", pady=3)

        lbl("Email patterns\n(one per line):", 8)
        self._m_emails = tk.Text(right, height=3, width=32,
                                  font=("Segoe UI", 9))
        self._m_emails.grid(row=8, column=1, sticky="ew", pady=3)

        tk.Button(
            right, text="Apply changes to selected mapping",
            command=self._on_map_apply,
            bg=COL_ACCENT, fg="white", relief="flat",
            padx=10, pady=4, font=("Segoe UI", 9),
        ).grid(row=9, column=0, columnspan=2, pady=(8, 0), sticky="w")

        self._map_form_widgets = [
            self._m_label, self._m_wbs, self._m_rproj,
            self._m_rkdauf, self._m_rkdpos,
            self._m_keywords, self._m_emails,
        ]
        self._map_form_state(enabled=False)
        self._refresh_map_list()

    def _entry(self, parent, row) -> tk.Entry:
        e = tk.Entry(parent, font=("Segoe UI", 9))
        e.grid(row=row, column=1, sticky="ew", pady=3)
        return e

    def _map_form_state(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for w in self._map_form_widgets:
            w.config(state=state)

    def _refresh_map_list(self):
        self._map_lb.delete(0, "end")
        for m in self._config.get("wbs_mappings", []):
            self._map_lb.insert("end", m.get("label", "(no label)"))

    def _on_map_select(self, _e=None):
        sel = self._map_lb.curselection()
        if not sel:
            return
        idx = sel[0]
        self._last_map_idx = idx   # remember for Apply / Save & Close fallback
        m   = self._config["wbs_mappings"][idx]
        self._map_form_state(enabled=True)
        self._m_label.delete(0, "end"); self._m_label.insert(0, m.get("label", ""))
        self._m_wbs.delete(0, "end");   self._m_wbs.insert(0,   m.get("wbs",   ""))
        self._m_rproj.delete(0, "end"); self._m_rproj.insert(0, m.get("rproj", ""))
        self._m_tasktype_var.set(m.get("tasktype", ""))
        self._m_tasklevel_var.set(m.get("tasklevel", ""))
        self._m_rkdauf.delete(0, "end"); self._m_rkdauf.insert(0, m.get("rkdauf", ""))
        self._m_rkdpos.delete(0, "end"); self._m_rkdpos.insert(0, m.get("rkdpos", ""))
        self._m_keywords.delete("1.0", "end")
        self._m_keywords.insert("1.0", "\n".join(m.get("keywords", [])))
        self._m_emails.delete("1.0", "end")
        self._m_emails.insert("1.0", "\n".join(m.get("email_patterns", [])))

    def _on_map_apply(self):
        sel = self._map_lb.curselection()
        # Fall back to the tracked index if the listbox selection was lost
        # (e.g. user clicked elsewhere before hitting Apply / Save & Close)
        idx = sel[0] if sel else getattr(self, "_last_map_idx", None)
        if idx is None:
            return

        # Read ALL form values first — before any widget/list manipulation
        # that could trigger <<ListboxSelect>> and reload the form.
        new_label    = self._m_label.get().strip()
        new_wbs      = self._m_wbs.get().strip()
        new_rproj    = self._m_rproj.get().strip()
        new_tasktype = self._m_tasktype_var.get()
        new_tasklevel= self._m_tasklevel_var.get()
        new_rkdauf   = self._m_rkdauf.get().strip()
        new_rkdpos   = self._m_rkdpos.get().strip()
        # Temporarily enable the Text widgets so .get() works even if disabled
        kw_state = self._m_keywords.cget("state")
        em_state = self._m_emails.cget("state")
        self._m_keywords.config(state="normal")
        self._m_emails.config(state="normal")
        new_keywords = [
            ln.strip() for ln in self._m_keywords.get("1.0", "end").splitlines()
            if ln.strip()
        ]
        new_emails = [
            ln.strip() for ln in self._m_emails.get("1.0", "end").splitlines()
            if ln.strip()
        ]
        self._m_keywords.config(state=kw_state)
        self._m_emails.config(state=em_state)

        # Now write to config
        m = self._config["wbs_mappings"][idx]
        m["label"]          = new_label
        m["wbs"]            = new_wbs
        m["rproj"]          = new_rproj
        m["tasktype"]       = new_tasktype
        m["tasklevel"]      = new_tasklevel
        m["rkdauf"]         = new_rkdauf
        m["rkdpos"]         = new_rkdpos
        m["keywords"]       = new_keywords
        m["email_patterns"] = new_emails

        # Update only the label in the listbox (avoids delete/re-insert triggering
        # <<ListboxSelect>> which would reload the form and potentially overwrite
        # what was just read above)
        self._map_lb.delete(idx)
        self._map_lb.insert(idx, new_label or "(no label)")
        self._map_lb.selection_set(idx)

        _save_config(self._config)
        try:
            self.top.title("CATXT Sync — Settings  ✓ Saved")
            self.top.after(1500, lambda: self.top.title("CATXT Sync — Settings"))
        except Exception:
            pass

    def _on_map_add(self):
        new = {
            "label": "New Mapping", "wbs": "", "rproj": "",
            "keywords": [], "email_patterns": [],
            "tasktype": "CFPP", "tasklevel": "G3",
        }
        self._config.setdefault("wbs_mappings", []).append(new)
        self._refresh_map_list()
        idx = len(self._config["wbs_mappings"]) - 1
        self._map_lb.selection_clear(0, "end")
        self._map_lb.selection_set(idx)
        self._map_lb.see(idx)
        self._on_map_select()

    def _on_map_delete(self):
        sel = self._map_lb.curselection()
        if not sel:
            return
        idx   = sel[0]
        label = self._config["wbs_mappings"][idx].get("label", "this mapping")
        if not messagebox.askyesno(
            "Delete mapping",
            f"Delete \"{label}\"?\n\nThis cannot be undone.",
            parent=self.top,
        ):
            return
        del self._config["wbs_mappings"][idx]
        self._map_form_state(enabled=False)
        self._refresh_map_list()

    # ══════════════════════════════════════════════════════════════════════════
    # Tab: Auto-Mappings
    # ══════════════════════════════════════════════════════════════════════════

    def _build_tab_auto_mappings(self, nb: ttk.Notebook):
        from catxt_core import KNOWN_TASK_TYPES as _KTT

        frame = tk.Frame(nb, bg="white")
        nb.add(frame, text="  Auto-Mappings  ")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        tk.Label(
            frame,
            text=(
                "Auto-mappings let CATXT auto-post events without showing the review dialog.\n"
                "If an event subject contains the keyword (case-insensitive) it is posted automatically.\n"
                "Checked after WBS rules, before CATXT favorites."
            ),
            bg="white", fg="#555", font=("Segoe UI", 9, "italic"),
            justify="left",
        ).grid(row=0, column=0, columnspan=2, padx=12, pady=(10, 6), sticky="w")

        main = tk.Frame(frame, bg="white")
        main.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=8, pady=4)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        # ── Left: treeview + action buttons ──────────────────────────────────
        left = tk.Frame(main, bg="white")
        left.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        left.rowconfigure(0, weight=1)

        cols = ("keyword", "tasktype", "subtype")
        self._am_tree = ttk.Treeview(
            left, columns=cols, show="headings",
            selectmode="browse", height=16,
        )
        self._am_tree.heading("keyword",  text="Keyword")
        self._am_tree.heading("tasktype", text="Type")
        self._am_tree.heading("subtype",  text="Subtype")
        self._am_tree.column("keyword",  width=200, minwidth=120)
        self._am_tree.column("tasktype", width=55,  minwidth=40)
        self._am_tree.column("subtype",  width=85,  minwidth=60)

        tree_scroll = ttk.Scrollbar(left, orient="vertical",
                                    command=self._am_tree.yview)
        self._am_tree.configure(yscrollcommand=tree_scroll.set)
        self._am_tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self._am_tree.bind("<<TreeviewSelect>>", self._am_on_select)

        btn_row = tk.Frame(left, bg="white")
        btn_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        tk.Button(
            btn_row, text="+ Add",
            command=self._am_on_add,
            bg=COL_ACCENT, fg="white", relief="flat",
            padx=8, pady=3, font=("Segoe UI", 9),
        ).pack(side="left", padx=(0, 4))
        tk.Button(
            btn_row, text="Delete",
            command=self._am_on_delete,
            bg="#dc3545", fg="white", relief="flat",
            padx=8, pady=3, font=("Segoe UI", 9),
        ).pack(side="left", padx=(0, 10))
        tk.Button(
            btn_row, text="Import from CATXT Favorites",
            command=self._am_on_import,
            relief="flat", padx=8, pady=3, font=("Segoe UI", 9),
        ).pack(side="left")

        # ── Right: edit form ──────────────────────────────────────────────────
        right = tk.Frame(main, bg="white")
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)

        def lbl(text, row):
            tk.Label(
                right, text=text, bg="white",
                font=("Segoe UI", 9), anchor="e", width=20,
            ).grid(row=row, column=0, sticky="ne", padx=(0, 6), pady=3)

        lbl("Keyword:", 0)
        self._am_keyword = tk.Entry(right, font=("Segoe UI", 9))
        self._am_keyword.grid(row=0, column=1, sticky="ew", pady=3)

        lbl("Display name\n(optional):", 1)
        self._am_label = tk.Entry(right, font=("Segoe UI", 9))
        self._am_label.grid(row=1, column=1, sticky="ew", pady=3)
        tk.Label(
            right, text="If blank, the keyword is used.",
            bg="white", fg="#888", font=("Segoe UI", 8),
        ).grid(row=2, column=1, sticky="w")

        lbl("Task Type:", 3)
        tt_options = [f"{c} - {v['label']}" for c, v in _KTT.items()]
        self._am_tasktype_var = tk.StringVar()
        self._am_tt_cb = ttk.Combobox(
            right, textvariable=self._am_tasktype_var,
            values=tt_options, state="readonly", width=26,
        )
        self._am_tt_cb.grid(row=3, column=1, sticky="w", pady=3)
        self._am_tasktype_var.trace_add("write", self._am_on_tasktype_change)

        lbl("Sub-type:", 4)
        self._am_subtype_var = tk.StringVar()
        self._am_st_cb = ttk.Combobox(
            right, textvariable=self._am_subtype_var,
            values=[], state="readonly", width=26,
        )
        self._am_st_cb.grid(row=4, column=1, sticky="w", pady=3)

        lbl("Cost Centre (rkostl):", 5)
        self._am_rkostl = tk.Entry(right, font=("Segoe UI", 9), width=16)
        self._am_rkostl.grid(row=5, column=1, sticky="w", pady=3)

        lbl("WBS Project\n(blank = CC only):", 6)
        self._am_rproj = tk.Entry(right, font=("Segoe UI", 9))
        self._am_rproj.grid(row=6, column=1, sticky="ew", pady=3)

        lbl("Description (ltxa1)\n(optional):", 7)
        self._am_ltxa1 = tk.Entry(right, font=("Segoe UI", 9))
        self._am_ltxa1.grid(row=7, column=1, sticky="ew", pady=3)
        tk.Label(
            right, text="If blank, the calendar event subject is used.",
            bg="white", fg="#888", font=("Segoe UI", 8),
        ).grid(row=8, column=1, sticky="w")

        tk.Button(
            right, text="Apply changes to selected mapping",
            command=self._am_on_apply,
            bg=COL_ACCENT, fg="white", relief="flat",
            padx=10, pady=4, font=("Segoe UI", 9),
        ).grid(row=9, column=0, columnspan=2, pady=(10, 0), sticky="w")

        self._am_form_widgets = [
            self._am_keyword, self._am_label,
            self._am_rkostl, self._am_rproj, self._am_ltxa1,
        ]
        self._am_form_state(enabled=False)
        self._am_refresh_tree()

    # ── Auto-Mappings helpers ─────────────────────────────────────────────────

    def _am_form_state(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for w in self._am_form_widgets:
            w.config(state=state)
        self._am_tt_cb.config(state="readonly" if enabled else "disabled")
        self._am_st_cb.config(state="readonly" if enabled else "disabled")

    def _am_refresh_tree(self):
        for item in self._am_tree.get_children():
            self._am_tree.delete(item)
        for i, m in enumerate(self._config.get("auto_mappings", [])):
            kw = m.get("keyword") or m.get("label", "")
            tt = m.get("tasktype", "")
            st = m.get("zzsubtype", "")
            self._am_tree.insert("", "end", iid=str(i), values=(kw, tt, st))

    def _am_on_select(self, _e=None):
        sel = self._am_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        m   = self._config["auto_mappings"][idx]
        self._am_form_state(enabled=True)

        self._am_keyword.delete(0, "end")
        self._am_keyword.insert(0, m.get("keyword", ""))

        self._am_label.delete(0, "end")
        self._am_label.insert(0, m.get("label", ""))

        # Task type — set combo first; this triggers _am_on_tasktype_change
        # which repopulates the subtype list before we set the subtype value.
        from catxt_core import KNOWN_TASK_TYPES as _KTT
        cur_tt = m.get("tasktype", "")
        cur_tt_str = next(
            (f"{c} - {v['label']}" for c, v in _KTT.items() if c == cur_tt),
            cur_tt,
        )
        self._am_tasktype_var.set(cur_tt_str)

        cur_st = m.get("zzsubtype", "")
        task_types = self._config.get("_task_types", {})
        subtypes   = task_types.get(cur_tt, {}).get("subtypes", [])
        cur_st_str = next(
            (f"{s['code']} - {s['label']}" for s in subtypes
             if s["code"] == cur_st),
            cur_st,
        )
        self._am_subtype_var.set(cur_st_str)

        self._am_rkostl.delete(0, "end")
        self._am_rkostl.insert(0, m.get("rkostl", ""))
        self._am_rproj.delete(0, "end")
        self._am_rproj.insert(0, m.get("rproj", ""))
        self._am_ltxa1.delete(0, "end")
        self._am_ltxa1.insert(0, m.get("ltxa1", ""))

    def _am_on_tasktype_change(self, *_args):
        raw  = self._am_tasktype_var.get()
        code = raw.split(" - ")[0] if " - " in raw else raw
        task_types = self._config.get("_task_types", {})
        subtypes   = task_types.get(code, {}).get("subtypes", [])
        st_options = [""] + [f"{s['code']} - {s['label']}" for s in subtypes]
        self._am_st_cb.config(values=st_options)
        if self._am_subtype_var.get() not in st_options:
            self._am_subtype_var.set("")

    def _am_on_add(self):
        dm = self._config.get("default_mapping", {})
        new = {
            "label":     "",
            "keyword":   "New Mapping",
            "tasktype":  "MEET",
            "zzsubtype": "",
            "rkostl":    dm.get("rkostl", ""),
            "rproj":     "",
            "ltxa1":     "",
        }
        self._config.setdefault("auto_mappings", []).append(new)
        self._am_refresh_tree()
        idx = len(self._config["auto_mappings"]) - 1
        self._am_tree.selection_set(str(idx))
        self._am_tree.see(str(idx))
        self._am_on_select()

    def _am_on_delete(self):
        sel = self._am_tree.selection()
        if not sel:
            return
        idx   = int(sel[0])
        label = self._config["auto_mappings"][idx].get("keyword", "this mapping")
        if not messagebox.askyesno(
            "Delete auto-mapping",
            f"Delete \"{label}\"?\n\nThis cannot be undone.",
            parent=self.top,
        ):
            return
        del self._config["auto_mappings"][idx]
        self._am_form_state(enabled=False)
        self._am_refresh_tree()

    def _am_on_apply(self):
        sel = self._am_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        m   = self._config["auto_mappings"][idx]

        keyword = self._am_keyword.get().strip()
        label   = self._am_label.get().strip()

        raw_tt   = self._am_tasktype_var.get()
        tasktype = raw_tt.split(" - ")[0] if " - " in raw_tt else raw_tt

        raw_st    = self._am_subtype_var.get()
        zzsubtype = raw_st.split(" - ")[0] if " - " in raw_st else raw_st

        m["keyword"]   = keyword
        m["label"]     = label or keyword
        m["tasktype"]  = tasktype
        m["zzsubtype"] = zzsubtype
        m["rkostl"]    = self._am_rkostl.get().strip()
        m["rproj"]     = self._am_rproj.get().strip()
        m["ltxa1"]     = self._am_ltxa1.get().strip()

        self._am_refresh_tree()
        self._am_tree.selection_set(str(idx))

    def _am_on_import(self):
        favorites = self._config.get("_favorites", [])
        if not favorites:
            messagebox.showinfo(
                "Import from Favorites",
                "No CATXT favorites found in the local cache.\n\n"
                "Use 'Sync Metadata' from the tray menu first to fetch your favorites,\n"
                "then reopen Settings.",
                parent=self.top,
            )
            return

        existing = {
            m.get("keyword", "").lower()
            for m in self._config.get("auto_mappings", [])
        }
        added = 0
        for fav in favorites:
            keyword = (fav.get("ltxa1") or "").strip()
            if not keyword or keyword.lower() in existing:
                continue
            self._config.setdefault("auto_mappings", []).append({
                "label":     keyword,
                "keyword":   keyword,
                "tasktype":  fav.get("tasktype", ""),
                "zzsubtype": fav.get("zzsubtype", ""),
                "rkostl":    fav.get("rkostl", ""),
                "rproj":     fav.get("rproj", ""),
                "ltxa1":     keyword,
            })
            existing.add(keyword.lower())
            added += 1

        if added:
            self._am_refresh_tree()
            messagebox.showinfo(
                "Import from Favorites",
                f"{added} favorite(s) imported.\n\n"
                "Review each entry and adjust keywords or details before saving.",
                parent=self.top,
            )
        else:
            messagebox.showinfo(
                "Import from Favorites",
                "All favorites are already present as auto-mappings — nothing to import.",
                parent=self.top,
            )

    def _collect_auto_mappings(self):
        self._am_on_apply()   # flush any unsaved form edits; no-op if nothing selected

    # ══════════════════════════════════════════════════════════════════════════
    # Tab 2 — Default Mapping
    # ══════════════════════════════════════════════════════════════════════════

    def _build_tab_default(self, nb: ttk.Notebook):
        frame = tk.Frame(nb, bg="white")
        nb.add(frame, text="  Default Mapping  ")
        frame.columnconfigure(1, weight=1)

        dm = self._config.get("default_mapping", {})

        def lbl(text, row):
            tk.Label(
                frame, text=text, bg="white",
                font=("Segoe UI", 9), anchor="e", width=22,
            ).grid(row=row, column=0, sticky="e", padx=(20, 6), pady=5)

        tk.Label(
            frame,
            text=(
                "The default mapping is used for meetings that don't match any\n"
                "keyword or email pattern.  Typically your overhead cost center."
            ),
            bg="white", fg="#555", font=("Segoe UI", 9, "italic"),
            justify="left",
        ).grid(row=0, column=0, columnspan=2, padx=20, pady=(12, 8), sticky="w")

        lbl("Label:", 1)
        self._def_label = tk.Entry(frame, font=("Segoe UI", 9))
        self._def_label.insert(0, dm.get("label", "Default Cost Centre"))
        self._def_label.grid(row=1, column=1, sticky="ew", padx=(0, 20), pady=5)

        lbl("Cost Centre (rkostl):", 2)
        self._def_rkostl = tk.Entry(frame, font=("Segoe UI", 9), width=16)
        self._def_rkostl.insert(0, dm.get("rkostl", ""))
        self._def_rkostl.grid(row=2, column=1, sticky="w", padx=(0, 20), pady=5)

        lbl("WBS (leave blank for CC):", 3)
        self._def_wbs = tk.Entry(frame, font=("Segoe UI", 9))
        self._def_wbs.insert(0, dm.get("wbs", ""))
        self._def_wbs.grid(row=3, column=1, sticky="ew", padx=(0, 20), pady=5)

        lbl("Rproj:", 4)
        self._def_rproj = tk.Entry(frame, font=("Segoe UI", 9))
        self._def_rproj.insert(0, dm.get("rproj", ""))
        self._def_rproj.grid(row=4, column=1, sticky="ew", padx=(0, 20), pady=5)

        lbl("Default Task Type:", 5)
        from catxt_core import KNOWN_TASK_TYPES
        tt_options = [""] + [f"{c} - {v['label']}" for c, v in KNOWN_TASK_TYPES.items()]
        self._def_tasktype_var = tk.StringVar()
        cur_tt = dm.get("tasktype", "")
        cur_tt_label = next(
            (f"{c} - {v['label']}" for c, v in KNOWN_TASK_TYPES.items() if c == cur_tt), cur_tt
        )
        self._def_tasktype_var.set(cur_tt_label)
        ttk.Combobox(
            frame, textvariable=self._def_tasktype_var,
            values=tt_options, state="readonly", width=30,
        ).grid(row=5, column=1, sticky="w", padx=(0, 20), pady=5)

        tk.Label(
            frame,
            text="(Used for meetings with no keyword/email match.  MEET is typical.)",
            bg="white", fg="#888", font=("Segoe UI", 8), anchor="w",
        ).grid(row=6, column=1, sticky="w", padx=(0, 20))

    def _collect_default(self):
        raw_tt = self._def_tasktype_var.get()
        tasktype = raw_tt.split(" - ")[0] if " - " in raw_tt else raw_tt
        self._config["default_mapping"] = {
            "label":     self._def_label.get().strip()  or "Default Cost Center",
            "rkostl":    self._def_rkostl.get().strip(),
            "wbs":       self._def_wbs.get().strip(),
            "rproj":     self._def_rproj.get().strip(),
            "tasktype":  tasktype,
            "tasklevel": "",
        }

    # ══════════════════════════════════════════════════════════════════════════
    # Tab 3 — Excluded Keywords
    # ══════════════════════════════════════════════════════════════════════════

    def _build_tab_excluded(self, nb: ttk.Notebook):
        frame = tk.Frame(nb, bg="white")
        nb.add(frame, text="  Excluded Keywords  ")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        tk.Label(
            frame,
            text=(
                "Meetings whose subject contains any of these keywords (case-insensitive)\n"
                "are always ignored — they will never be posted to CATXT."
            ),
            bg="white", fg="#555", font=("Segoe UI", 9, "italic"),
            justify="left",
        ).grid(row=0, column=0, columnspan=2, padx=20, pady=(12, 4), sticky="w")

        lb_frame = tk.Frame(frame, bg="white")
        lb_frame.grid(row=1, column=0, sticky="nsew", padx=20, pady=4)
        lb_frame.columnconfigure(0, weight=1)
        lb_frame.rowconfigure(0, weight=1)

        self._excl_lb = tk.Listbox(
            lb_frame, font=("Segoe UI", 9),
            selectbackground=COL_ACCENT, activestyle="dotbox",
        )
        excl_scroll = ttk.Scrollbar(lb_frame, orient="vertical",
                                     command=self._excl_lb.yview)
        self._excl_lb.configure(yscrollcommand=excl_scroll.set)
        self._excl_lb.grid(row=0, column=0, sticky="nsew")
        excl_scroll.grid(row=0, column=1, sticky="ns")

        # Populate from config — combine built-in + any custom in config
        from catxt_core import EXCLUDED_SUBJECTS
        stored = self._config.get("_excluded_keywords", list(EXCLUDED_SUBJECTS))
        self._config["_excluded_keywords"] = stored
        for kw in stored:
            self._excl_lb.insert("end", kw)

        # Add row
        add_row = tk.Frame(frame, bg="white")
        add_row.grid(row=2, column=0, sticky="ew", padx=20, pady=(4, 8))
        self._excl_entry = tk.Entry(add_row, font=("Segoe UI", 9), width=30)
        self._excl_entry.pack(side="left", padx=(0, 6))
        self._excl_entry.bind("<Return>", lambda _e: self._on_excl_add())
        tk.Button(
            add_row, text="Add",
            command=self._on_excl_add,
            bg=COL_ACCENT, fg="white", relief="flat",
            padx=10, pady=3, font=("Segoe UI", 9),
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            add_row, text="Delete selected",
            command=self._on_excl_delete,
            bg="#dc3545", fg="white", relief="flat",
            padx=10, pady=3, font=("Segoe UI", 9),
        ).pack(side="left")

    def _on_excl_add(self):
        kw = self._excl_entry.get().strip().lower()
        if not kw:
            return
        self._excl_lb.insert("end", kw)
        self._excl_entry.delete(0, "end")

    def _on_excl_delete(self):
        sel = self._excl_lb.curselection()
        if sel:
            self._excl_lb.delete(sel[0])

    def _collect_excluded(self):
        self._config["_excluded_keywords"] = list(self._excl_lb.get(0, "end"))

    # ── Save / cancel ─────────────────────────────────────────────────────────

    # ══════════════════════════════════════════════════════════════════════════
    # Tab 4 — Schedule
    # ══════════════════════════════════════════════════════════════════════════

    def _build_tab_schedule(self, nb: ttk.Notebook):
        frame = tk.Frame(nb, bg="white")
        nb.add(frame, text="Schedule")
        frame.columnconfigure(1, weight=1)

        tk.Label(
            frame, text="Daily Auto-Sync Schedule",
            font=("Segoe UI", 10, "bold"), bg="white",
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=20, pady=(16, 2))

        tk.Label(
            frame,
            text="Register a Windows Task Scheduler job to run CATXT Sync automatically each day.\n"
                 "The scheduled task runs silently — no review dialog. Use the tray for manual syncs.",
            bg="white", fg="#666", font=("Segoe UI", 9), justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 14))

        # Time picker row
        tk.Label(frame, text="Run daily at:", bg="white",
                 font=("Segoe UI", 9)).grid(row=2, column=0, sticky="w",
                                             padx=20, pady=4)
        time_frame = tk.Frame(frame, bg="white")
        time_frame.grid(row=2, column=1, sticky="w", padx=(0, 20), pady=4)

        self._sched_hour = tk.StringVar(value="17")
        self._sched_min  = tk.StringVar(value="00")

        tk.Spinbox(
            time_frame, from_=0, to=23,
            textvariable=self._sched_hour,
            width=4, format="%02.0f", font=("Segoe UI", 10),
        ).pack(side="left")
        tk.Label(time_frame, text=":", bg="white",
                 font=("Segoe UI", 11, "bold")).pack(side="left", padx=2)
        tk.Spinbox(
            time_frame, values=("00", "15", "30", "45"),
            textvariable=self._sched_min,
            width=4, font=("Segoe UI", 10),
        ).pack(side="left")
        tk.Label(time_frame, text="(24-hour)", bg="white",
                 fg="#888", font=("Segoe UI", 8)).pack(side="left", padx=(8, 0))

        # Status
        self._sched_status_var = tk.StringVar(value="Checking task status…")
        tk.Label(
            frame, textvariable=self._sched_status_var,
            bg="white", fg="#444", font=("Segoe UI", 9),
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=20, pady=(12, 4))

        # Buttons
        btn_frame = tk.Frame(frame, bg="white")
        btn_frame.grid(row=4, column=0, columnspan=2, sticky="w", padx=20, pady=8)

        tk.Button(
            btn_frame, text="Register / Update Task",
            command=self._sched_register,
            bg=COL_ACCENT, fg="white", relief="flat",
            padx=12, pady=4, font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=(0, 8))
        tk.Button(
            btn_frame, text="Remove Task",
            command=self._sched_remove,
            relief="flat", padx=12, pady=4,
            font=("Segoe UI", 9),
        ).pack(side="left")

        # Check status after a short delay so the dialog renders first
        frame.after(200, self._sched_check_status)

    def _sched_check_status(self):
        try:
            r = subprocess.run(
                ["schtasks", "/query", "/tn", "CATXT Sync", "/fo", "LIST"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                # Try to pull the next-run time to pre-fill the spinboxes
                import re
                for line in r.stdout.splitlines():
                    m = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*(AM|PM)?",
                                  line, re.IGNORECASE)
                    if m and ("Run Time" in line or "Start Time" in line
                              or "Next Run" in line):
                        h, mn = int(m.group(1)), int(m.group(2))
                        suffix = (m.group(3) or "").upper()
                        if suffix == "PM" and h != 12:
                            h += 12
                        elif suffix == "AM" and h == 12:
                            h = 0
                        self._sched_hour.set(f"{h:02d}")
                        # snap minute to nearest quarter
                        snapped = min((0, 15, 30, 45), key=lambda q: abs(q - mn))
                        self._sched_min.set(f"{snapped:02d}")
                        break
                self._sched_status_var.set("● Task is registered and active")
            else:
                self._sched_status_var.set("○ No scheduled task registered")
        except Exception:
            self._sched_status_var.set("○ Could not check task status")

    def _sched_register(self):
        import pathlib as _pl
        hour = self._sched_hour.get().zfill(2)
        mn   = self._sched_min.get().zfill(2)
        time_str = f"{hour}:{mn}"

        if getattr(sys, "frozen", False):
            # Running from compiled CATXT.exe — just pass --scheduled to itself
            exe_cmd = f'"{sys.executable}" --scheduled'
        else:
            # Running from Python source (e.g., via launch_catxt.vbs).
            # Register the task to run pythonw (no console window) with
            # catxt_app.py (sibling of this file) and the --scheduled flag.
            script_path = _pl.Path(__file__).parent / "catxt_app.py"
            python_dir  = _pl.Path(sys.executable).parent
            # Prefer pythonw.exe so no console window appears when triggered
            pythonw = python_dir / "pythonw.exe"
            python_exe = str(pythonw) if pythonw.exists() else sys.executable
            exe_cmd = f'"{python_exe}" "{script_path}" --scheduled'
        try:
            r = subprocess.run(
                ["schtasks", "/create",
                 "/tn", "CATXT Sync",
                 "/tr", exe_cmd,
                 "/sc", "daily",
                 "/st", time_str,
                 "/f"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                self._sched_status_var.set(
                    f"● Task registered — runs daily at {time_str}"
                )
                messagebox.showinfo(
                    "Task Scheduler",
                    f"Task registered successfully.\nRuns daily at {time_str}.",
                    parent=self.top,
                )
            else:
                messagebox.showerror(
                    "Task Scheduler",
                    f"Could not register task:\n{r.stderr or r.stdout}",
                    parent=self.top,
                )
        except Exception as exc:
            messagebox.showerror("Task Scheduler", str(exc), parent=self.top)

    def _sched_remove(self):
        if not messagebox.askyesno(
            "Remove Task",
            "Remove the CATXT Sync scheduled task?",
            parent=self.top,
        ):
            return
        try:
            r = subprocess.run(
                ["schtasks", "/delete", "/tn", "CATXT Sync", "/f"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                self._sched_status_var.set("○ No scheduled task registered")
                messagebox.showinfo("Task Scheduler", "Task removed.",
                                    parent=self.top)
            else:
                messagebox.showerror(
                    "Task Scheduler",
                    f"Could not remove task:\n{r.stderr or r.stdout}",
                    parent=self.top,
                )
        except Exception as exc:
            messagebox.showerror("Task Scheduler", str(exc), parent=self.top)

    # ── Save / cancel ─────────────────────────────────────────────────────────

    def _on_save(self):
        # Flush any pending form data
        self._on_map_apply()         # no-op if nothing selected
        self._collect_auto_mappings()
        self._collect_default()
        self._collect_excluded()
        # General tab
        try:
            self._config["daily_hours_target"] = float(self._daily_hours_var.get())
        except (ValueError, AttributeError):
            self._config.setdefault("daily_hours_target", 8.0)
        try:
            self._config["max_scheduled_lookback_days"] = int(self._lookback_var.get())
        except (ValueError, AttributeError):
            self._config.setdefault("max_scheduled_lookback_days", 10)
        self.result = self._config
        self.top.destroy()

    def _on_cancel(self):
        self.result = None
        self.top.destroy()
