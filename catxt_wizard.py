"""
catxt_wizard.py — First-run setup wizard for CATXT Sync
=========================================================
Shown automatically when config.json is missing or incomplete.
Collects the minimum required settings (default cost center) and writes
a starter config.json.  The user can refine mappings later in Settings.
"""

import os as _os
import sys as _sys
import tkinter as tk
from tkinter import ttk
from pathlib import Path

_ICON_PATH = _os.path.join(
    getattr(_sys, "_MEIPASS", _os.path.dirname(_os.path.abspath(__file__))),
    "assets", "catxt.ico",
)

COL_HDR_BG = "#003366"
COL_HDR_FG = "#ffffff"
COL_ACCENT = "#0070d2"
COL_GOLD   = "#f0ab00"


class SetupWizard:
    """
    Multi-step first-run wizard.

    Shows three pages:
      1. Welcome
      2. Default cost center (required to post any time entry)
      3. Done — offers to run staffing sync now

    Sets self.completed = True and self.run_staffing_sync = bool on finish.
    Sets self.completed = False if the user closes / cancels.
    """

    def __init__(self, parent: tk.Misc):
        self.completed        = False
        self.run_staffing_sync = False
        self._step            = 0

        self.top = tk.Toplevel(parent)
        try:
            self.top.iconbitmap(_ICON_PATH)
        except Exception:
            pass
        self.top.title("CATXT Sync — First-time Setup")
        self.top.geometry("540x420")
        self.top.resizable(False, False)
        self.top.grab_set()
        self.top.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_shell()
        self._show_step(0)

        self.top.update_idletasks()
        x = (self.top.winfo_screenwidth()  - self.top.winfo_width())  // 2
        y = (self.top.winfo_screenheight() - self.top.winfo_height()) // 2
        self.top.geometry(f"+{x}+{y}")

        parent.wait_window(self.top)

    # ── Shell (header + content area + nav buttons) ───────────────────────────

    def _build_shell(self):
        self.top.columnconfigure(0, weight=1)
        self.top.rowconfigure(1, weight=1)

        # Header strip
        hdr = tk.Frame(self.top, bg=COL_HDR_BG, height=70)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.columnconfigure(0, weight=1)
        hdr.grid_propagate(False)

        self._hdr_title = tk.Label(
            hdr, bg=COL_HDR_BG, fg=COL_HDR_FG,
            font=("Segoe UI", 13, "bold"), anchor="w",
        )
        self._hdr_title.grid(row=0, column=0, padx=20, pady=(14, 2), sticky="w")
        self._hdr_sub = tk.Label(
            hdr, bg=COL_HDR_BG, fg="#a8c8f0",
            font=("Segoe UI", 9), anchor="w",
        )
        self._hdr_sub.grid(row=1, column=0, padx=20, sticky="w")

        # Content pane
        self._content = tk.Frame(self.top, bg="white")
        self._content.grid(row=1, column=0, sticky="nsew")
        self._content.columnconfigure(0, weight=1)

        # Nav bar
        nav = tk.Frame(self.top, bg="#f0f0f0", bd=1, relief="groove")
        nav.grid(row=2, column=0, sticky="ew")
        nav.columnconfigure(0, weight=1)

        # Step dots
        self._dots_frame = tk.Frame(nav, bg="#f0f0f0")
        self._dots_frame.grid(row=0, column=0, sticky="w", padx=16, pady=8)
        self._dot_labels = []
        for _ in range(3):
            d = tk.Label(self._dots_frame, text="●", bg="#f0f0f0",
                          font=("Segoe UI", 10))
            d.pack(side="left", padx=3)
            self._dot_labels.append(d)

        btn_frame = tk.Frame(nav, bg="#f0f0f0")
        btn_frame.grid(row=0, column=1, padx=12, pady=8)

        self._btn_back = tk.Button(
            btn_frame, text="← Back",
            command=self._on_back,
            relief="flat", padx=12, pady=4, font=("Segoe UI", 9),
        )
        self._btn_back.pack(side="left", padx=(0, 6))

        self._btn_next = tk.Button(
            btn_frame, text="Next →",
            command=self._on_next,
            bg=COL_ACCENT, fg="white",
            relief="flat", padx=12, pady=4,
            font=("Segoe UI", 9, "bold"),
        )
        self._btn_next.pack(side="left")

    # ── Step rendering ────────────────────────────────────────────────────────

    def _clear_content(self):
        for w in self._content.winfo_children():
            w.destroy()

    def _update_dots(self, step: int):
        for i, d in enumerate(self._dot_labels):
            if i == step:
                d.config(fg=COL_ACCENT)
            elif i < step:
                d.config(fg=COL_GOLD)
            else:
                d.config(fg="#cccccc")

    def _show_step(self, step: int):
        self._step = step
        self._clear_content()
        self._update_dots(step)
        getattr(self, f"_page_{step}")()

        self._btn_back.config(state="normal" if step > 0 else "disabled")
        if step == 2:
            self._btn_next.config(text="Finish", bg=COL_GOLD, fg="#333")
        else:
            self._btn_next.config(text="Next →", bg=COL_ACCENT, fg="white")

    # ── Page 0: Welcome ───────────────────────────────────────────────────────

    def _page_0(self):
        self._hdr_title.config(text="Welcome to CATXT Sync")
        self._hdr_sub.config(text="Step 1 of 3 — Let's get you set up")

        c = self._content
        tk.Label(
            c, bg="white",
            text=(
                "CATXT Sync reads your Outlook calendar each day and\n"
                "automatically posts matching meetings as time entries\n"
                "in SAP CATXT.\n\n"
                "This wizard will create a starter configuration.  You\n"
                "can add project rules and fine-tune everything later\n"
                "from the Settings dialog."
            ),
            font=("Segoe UI", 10), justify="left", anchor="w",
        ).pack(padx=30, pady=30, anchor="w")

        tk.Label(
            c, bg="white",
            text="You will need your SAP cost center number ready.",
            font=("Segoe UI", 9, "italic"), fg="#555", anchor="w",
        ).pack(padx=30, anchor="w")

    # ── Page 1: Default cost center ───────────────────────────────────────────

    def _page_1(self):
        self._hdr_title.config(text="Default Cost Centre")
        self._hdr_sub.config(text="Step 2 of 3 — Meetings with no project rule")

        c = self._content
        c.columnconfigure(1, weight=1)

        tk.Label(
            c, bg="white",
            text=(
                "When a meeting doesn't match any project keyword, CATXT Sync\n"
                "will use your default cost center.  Enter it below.\n"
                "(e.g. 0800080808)"
            ),
            font=("Segoe UI", 9), fg="#333", justify="left",
        ).grid(row=0, column=0, columnspan=2, padx=24, pady=(20, 12), sticky="w")

        tk.Label(
            c, text="Cost Centre (rkostl):", bg="white",
            font=("Segoe UI", 9, "bold"), anchor="e",
        ).grid(row=1, column=0, padx=(24, 8), pady=6, sticky="e")

        self._rkostl_var = tk.StringVar()
        tk.Entry(
            c, textvariable=self._rkostl_var,
            font=("Segoe UI", 10), width=16,
        ).grid(row=1, column=1, sticky="w", pady=6)

        tk.Label(
            c, text="Label (optional):", bg="white",
            font=("Segoe UI", 9), anchor="e",
        ).grid(row=2, column=0, padx=(24, 8), pady=6, sticky="e")

        self._deflabel_var = tk.StringVar(value="Default Cost Centre")
        tk.Entry(
            c, textvariable=self._deflabel_var,
            font=("Segoe UI", 9), width=30,
        ).grid(row=2, column=1, sticky="ew", padx=(0, 24), pady=6)

        tk.Label(
            c, bg="white",
            text=(
                "Tip: you can leave the cost center blank for now and set it\n"
                "later in Settings → Default Mapping."
            ),
            font=("Segoe UI", 8, "italic"), fg="#777", justify="left",
        ).grid(row=3, column=0, columnspan=2, padx=24, pady=(12, 0), sticky="w")

    # ── Page 2: Done ──────────────────────────────────────────────────────────

    def _page_2(self):
        self._hdr_title.config(text="Setup Complete")
        self._hdr_sub.config(text="Step 3 of 3 — Ready to sync")

        c = self._content
        tk.Label(
            c, bg="white",
            text=(
                "Your starter configuration has been saved.\n\n"
                "Next steps:\n"
                "  • Right-click the tray icon → Settings to add project\n"
                "    keyword rules for your customer meetings.\n"
                "  • Use \"Sync Staffing\" to auto-import your current\n"
                "    project assignments from CATXT.\n"
                "  • \"Sync Today\" will open a review window so you can\n"
                "    confirm entries before they are posted."
            ),
            font=("Segoe UI", 10), justify="left", anchor="w",
        ).pack(padx=30, pady=20, anchor="w")

        self._staffing_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            c,
            text="Run Staffing Sync now to import my active projects",
            variable=self._staffing_var,
            bg="white", font=("Segoe UI", 9),
        ).pack(padx=30, anchor="w")

    # ── Navigation ────────────────────────────────────────────────────────────

    def _on_back(self):
        if self._step > 0:
            self._show_step(self._step - 1)

    def _on_next(self):
        if self._step == 2:
            self._finish()
        else:
            self._show_step(self._step + 1)

    def _finish(self):
        from catxt_core import (
            CONFIG_FILE, load_config, save_config, EXCLUDED_SUBJECTS
        )
        config = load_config()

        rkostl = self._rkostl_var.get().strip() if hasattr(self, "_rkostl_var") else ""
        label  = self._deflabel_var.get().strip() if hasattr(self, "_deflabel_var") else "Default Cost Centre"

        config.setdefault("default_mapping", {}).update({
            "label":     label or "Default Cost Centre",
            "wbs":       "",
            "rproj":     "",
            "rkostl":    rkostl,
            "tasktype":  "",
            "tasklevel": "",
        })
        config.setdefault("wbs_mappings", [])
        config["_setup_done"] = True
        if "_excluded_keywords" not in config:
            config["_excluded_keywords"] = list(EXCLUDED_SUBJECTS)

        save_config(config)

        self.run_staffing_sync = bool(
            self._staffing_var.get() if hasattr(self, "_staffing_var") else False
        )
        self.completed = True
        self.top.destroy()

    def _on_close(self):
        self.completed = False
        self.top.destroy()
