"""
gui.py — Project Zomboid Server Manager
CustomTkinter-based UI.
"""

import os
import queue
import threading
import logging
from datetime import datetime, date
from tkinter import filedialog
from typing import Optional

import customtkinter as ctk
from PIL import Image, ImageDraw
import pystray

from backend import AppConfig, ServerManager, LogParser, LogTailer, ServerUpdateChecker

logger = logging.getLogger(__name__)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# Seconds remaining at which to broadcast an in-game warning to players (#2)
_BROADCAST_AT: dict[int, str] = {
    300: "Server restarting in 5 minutes for mod updates.",
    120: "Server restarting in 2 minutes for mod updates.",
    60:  "Server restarting in 1 minute for mod updates. Please find a safe location.",
}

_SERVER_UPDATE_BROADCAST_AT: dict[int, str] = {
    300: "Server restarting in 5 minutes for a game update.",
    120: "Server restarting in 2 minutes for a game update.",
    60:  "Server restarting in 1 minute for a game update. Please find a safe location.",
}

_STATUS_POLL_MS = 60_000   # auto-refresh server status every 60 s  (#6)
_SCHED_POLL_MS  = 60_000   # check scheduled restart time every 60 s (#14)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Project Zomboid Server Manager")
        self.geometry("800x740")
        self.minsize(660, 580)

        # Backend
        self.config = AppConfig()
        self.config.load()
        self.server = ServerManager(self.config)

        # State
        self._tray_icon: Optional[pystray.Icon] = None
        self._auto_check_job: Optional[str] = None
        self._poll_status_job: Optional[str] = None
        self._sched_job: Optional[str] = None
        self._countdown_remaining: int = 0
        self._countdown_active: bool = False
        self._countdown_broadcast_messages: dict = _BROADCAST_AT
        self._countdown_action = None
        self._countdown_post_fn = None
        self._log_tailer: Optional[LogTailer] = None
        self._server_log_queue: queue.Queue = queue.Queue()
        self._last_sched_restart_date: Optional[date] = None
        self._server_update_check_job: Optional[str] = None
        self._server_update_checker = ServerUpdateChecker(self.config)

        self.protocol("WM_DELETE_WINDOW", self._minimize_to_tray)

        self._build_ui()
        self._load_config_into_ui()

        # Deferred startup tasks — let window render first
        self.after(400, self._check_server_status_threaded)
        self.after(600, self._resume_auto_check)          # #1 & #3
        self.after(700, self._resume_server_update_check) # server update schedule
        self.after(800, self._start_log_tail)             # #5
        self._start_status_poll()                         # #6
        self._start_sched_poll()                          # #14
        self._drain_log_queue()                           # #5 — kick off queue drain loop

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(self)
        self.tabs.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        for name in ("Server Control", "RCON Console", "Server Log", "Settings"):
            self.tabs.add(name)

        self._build_control_tab(self.tabs.tab("Server Control"))
        self._build_rcon_tab(self.tabs.tab("RCON Console"))
        self._build_log_tab(self.tabs.tab("Server Log"))
        self._build_settings_tab(self.tabs.tab("Settings"))

    # ---------- Server Control tab ----------

    def _build_control_tab(self, parent: ctk.CTkFrame):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(6, weight=1)

        # Status row
        sf = ctk.CTkFrame(parent)
        sf.grid(row=0, column=0, sticky="ew", padx=5, pady=(5, 3))
        sf.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            sf, text="Server Status:", font=ctk.CTkFont(weight="bold")
        ).grid(row=0, column=0, padx=(12, 6), pady=10)

        self.status_label = ctk.CTkLabel(sf, text="Checking…", text_color="orange")
        self.status_label.grid(row=0, column=1, padx=4, pady=10, sticky="w")

        ctk.CTkButton(
            sf, text="Refresh", width=90,
            command=self._check_server_status_threaded,
        ).grid(row=0, column=2, padx=10, pady=10)

        # Control buttons
        bf = ctk.CTkFrame(parent)
        bf.grid(row=1, column=0, sticky="ew", padx=5, pady=3)
        bf.grid_columnconfigure((0, 1, 2), weight=1)

        ctk.CTkButton(
            bf, text="Start Server",
            fg_color="#1a7f3c", hover_color="#145e2c",
            command=self._start_server,
        ).grid(row=0, column=0, padx=10, pady=10, sticky="ew")

        ctk.CTkButton(
            bf, text="Stop Server",
            fg_color="#a62020", hover_color="#7a1818",
            command=self._stop_server,
        ).grid(row=0, column=1, padx=10, pady=10, sticky="ew")

        ctk.CTkButton(
            bf, text="Restart Server",
            fg_color="#7a5200", hover_color="#5a3c00",
            command=self._restart_server,
        ).grid(row=0, column=2, padx=10, pady=10, sticky="ew")

        # Mod update section
        mf = ctk.CTkFrame(parent)
        mf.grid(row=2, column=0, sticky="ew", padx=5, pady=3)
        mf.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            mf, text="Mod Updates:", font=ctk.CTkFont(weight="bold")
        ).grid(row=0, column=0, padx=(12, 6), pady=(10, 4))

        self.mod_status_label = ctk.CTkLabel(mf, text="Not checked", text_color="gray")
        self.mod_status_label.grid(row=0, column=1, padx=4, pady=(10, 4), sticky="w")

        ctk.CTkButton(
            mf, text="Check for Updates", width=165,
            command=self._check_mods_threaded,
        ).grid(row=0, column=2, padx=10, pady=(10, 4))

        self.last_check_label = ctk.CTkLabel(
            mf, text="Last checked: Never",
            text_color="gray", font=ctk.CTkFont(size=12),
        )
        self.last_check_label.grid(
            row=1, column=0, columnspan=3, padx=12, pady=(0, 4), sticky="w"
        )

        # Mod list — hidden until there are updates
        self._mod_list_header = ctk.CTkLabel(
            mf, text="Mods that need updating:",
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self._mod_list_header.grid(
            row=2, column=0, columnspan=3, padx=12, pady=(4, 2), sticky="w"
        )
        self._mod_list_header.grid_remove()

        self._mod_list_frame = ctk.CTkScrollableFrame(mf, height=100)
        self._mod_list_frame.grid(
            row=3, column=0, columnspan=3, padx=10, pady=(0, 10), sticky="ew"
        )
        self._mod_list_frame.grid_columnconfigure(0, weight=1)
        self._mod_list_frame.grid_remove()

        # Server update section
        suf = ctk.CTkFrame(parent)
        suf.grid(row=3, column=0, sticky="ew", padx=5, pady=3)
        suf.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            suf, text="Server Update:", font=ctk.CTkFont(weight="bold")
        ).grid(row=0, column=0, padx=(12, 6), pady=(10, 4))

        self.server_update_status_label = ctk.CTkLabel(
            suf, text="Not checked", text_color="gray"
        )
        self.server_update_status_label.grid(
            row=0, column=1, padx=4, pady=(10, 4), sticky="w"
        )

        ctk.CTkButton(
            suf, text="Check Now", width=120,
            command=self._check_server_update_threaded,
        ).grid(row=0, column=2, padx=10, pady=(10, 4))

        self.server_update_last_check_label = ctk.CTkLabel(
            suf, text="Last checked: Never",
            text_color="gray", font=ctk.CTkFont(size=12),
        )
        self.server_update_last_check_label.grid(
            row=1, column=0, columnspan=3, padx=12, pady=(0, 4), sticky="w"
        )

        # Countdown
        self.countdown_label = ctk.CTkLabel(
            parent, text="",
            text_color="#ff9900",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.countdown_label.grid(row=4, column=0, pady=4)

        # Activity log
        ctk.CTkLabel(
            parent, text="Activity Log:", font=ctk.CTkFont(weight="bold")
        ).grid(row=5, column=0, padx=8, pady=(4, 0), sticky="w")

        self.log_box = ctk.CTkTextbox(parent, wrap="word", state="disabled")
        self.log_box.grid(row=6, column=0, sticky="nsew", padx=5, pady=(2, 8))

    # ---------- RCON Console tab (#4) ----------

    def _build_rcon_tab(self, parent: ctk.CTkFrame):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        irow = ctk.CTkFrame(parent)
        irow.grid(row=0, column=0, sticky="ew", padx=5, pady=(8, 4))
        irow.grid_columnconfigure(0, weight=1)

        self.rcon_input = ctk.CTkEntry(
            irow,
            placeholder_text='Enter RCON command  (e.g.  players  |  save  |  servermsg "text")…',
        )
        self.rcon_input.grid(row=0, column=0, padx=(10, 5), pady=10, sticky="ew")
        self.rcon_input.bind("<Return>", lambda _e: self._send_rcon_command())

        ctk.CTkButton(
            irow, text="Send", width=80,
            command=self._send_rcon_command,
        ).grid(row=0, column=1, padx=(5, 10), pady=10)

        self.rcon_output = ctk.CTkTextbox(parent, wrap="word", state="disabled")
        self.rcon_output.grid(row=1, column=0, sticky="nsew", padx=5, pady=(0, 4))

        ctk.CTkButton(
            parent, text="Clear Output", width=120,
            command=lambda: self._clear_textbox(self.rcon_output),
        ).grid(row=2, column=0, pady=(0, 8))

    # ---------- Server Log tab (#5) ----------

    def _build_log_tab(self, parent: ctk.CTkFrame):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        ctrl = ctk.CTkFrame(parent)
        ctrl.grid(row=0, column=0, sticky="ew", padx=5, pady=(8, 4))
        ctrl.grid_columnconfigure(0, weight=1)

        self._log_file_label = ctk.CTkLabel(
            ctrl, text="Not tailing any log file.",
            text_color="gray", font=ctk.CTkFont(size=12),
        )
        self._log_file_label.grid(row=0, column=0, padx=10, pady=8, sticky="w")

        ctk.CTkButton(
            ctrl, text="Restart Tail", width=110,
            command=self._restart_log_tail,
        ).grid(row=0, column=1, padx=5, pady=8)

        ctk.CTkButton(
            ctrl, text="Clear", width=80,
            command=lambda: self._clear_textbox(self.server_log_box),
        ).grid(row=0, column=2, padx=(0, 10), pady=8)

        self.server_log_box = ctk.CTkTextbox(parent, wrap="word", state="disabled")
        self.server_log_box.grid(row=1, column=0, sticky="nsew", padx=5, pady=(0, 8))

    # ---------- Settings tab ----------

    def _build_settings_tab(self, parent: ctk.CTkFrame):
        parent.grid_columnconfigure(1, weight=1)

        base_rows = [
            ("PZ Server Folder:",            "server_dir",              "dir"),
            ("Zomboid Log Folder:",          "zomboid_dir",             "dir"),
            ("RCON Executable:",             "rcon_path",               "file"),
            ("Server Name:",                 "server_name",             None),
            ("Server IP:",                   "server_ip",               None),
            ("Password:",                    "password",                "password"),
            ("Check Interval (min):",        "check_interval",          None),
            ("SteamCMD Path:",               "steamcmd_path",           "file"),
            ("Server Update Interval (min):", "server_update_interval", None),
            ("SteamCMD Timeout (sec):",      "steamcmd_timeout",        None),
        ]

        self._settings_entries: dict[str, ctk.CTkEntry] = {}

        for i, (label, key, kind) in enumerate(base_rows):
            ctk.CTkLabel(parent, text=label).grid(
                row=i, column=0, padx=(10, 5), pady=8, sticky="e"
            )
            entry = ctk.CTkEntry(parent, show="*" if kind == "password" else "")
            entry.grid(row=i, column=1, padx=5, pady=8, sticky="ew")
            self._settings_entries[key] = entry

            if kind == "dir":
                ctk.CTkButton(
                    parent, text="Browse", width=80,
                    command=lambda e=entry: self._pick_dir(e),
                ).grid(row=i, column=2, padx=(5, 10), pady=8)
            elif kind == "file":
                ctk.CTkButton(
                    parent, text="Browse", width=80,
                    command=lambda e=entry: self._pick_file(e),
                ).grid(row=i, column=2, padx=(5, 10), pady=8)

        # --- Scheduled daily restart (#14) ---
        srow = len(base_rows)
        ctk.CTkLabel(parent, text="Scheduled Restart:").grid(
            row=srow, column=0, padx=(10, 5), pady=8, sticky="e"
        )

        sched_inner = ctk.CTkFrame(parent, fg_color="transparent")
        sched_inner.grid(row=srow, column=1, padx=5, pady=8, sticky="w")

        hours   = ["Disabled"] + [f"{h:02d}" for h in range(24)]
        minutes = [f"{m:02d}" for m in range(0, 60, 5)]

        self._sched_hour = ctk.CTkOptionMenu(sched_inner, values=hours, width=100)
        self._sched_hour.grid(row=0, column=0, padx=(0, 4))

        ctk.CTkLabel(sched_inner, text=":").grid(row=0, column=1, padx=2)

        self._sched_minute = ctk.CTkOptionMenu(sched_inner, values=minutes, width=80)
        self._sched_minute.grid(row=0, column=2, padx=(4, 10))

        ctk.CTkLabel(
            sched_inner,
            text="24 h — choose 'Disabled' to turn off",
            text_color="gray", font=ctk.CTkFont(size=11),
        ).grid(row=0, column=3)

        ctk.CTkButton(
            parent, text="Save Configuration", command=self._save_config
        ).grid(row=srow + 1, column=0, columnspan=3, pady=20)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _load_config_into_ui(self):
        cfg = self.config
        e = self._settings_entries

        def put(key: str, val):
            e[key].delete(0, "end")
            e[key].insert(0, str(val))

        put("server_dir",              cfg.server_dir)
        put("zomboid_dir",             cfg.zomboid_dir)
        put("rcon_path",               cfg.rcon_path)
        put("server_name",             cfg.server_name)
        put("server_ip",               cfg.server_ip)
        put("password",                cfg.password)
        put("check_interval",          cfg.check_interval)
        put("steamcmd_path",           cfg.steamcmd_path)
        put("server_update_interval",  cfg.server_update_interval)
        put("steamcmd_timeout",        cfg.steamcmd_timeout)

        if cfg.last_update:
            self.last_check_label.configure(text=f"Last checked: {cfg.last_update}")

        if cfg.last_server_update_check:
            self.server_update_last_check_label.configure(
                text=f"Last checked: {cfg.last_server_update_check}"
            )

        # Restore scheduled restart dropdowns
        sched = cfg.scheduled_restart.strip()
        if sched:
            try:
                sh, sm = sched.split(":")
                sm_rounded = f"{(int(sm) // 5) * 5:02d}"
                self._sched_hour.set(sh)
                self._sched_minute.set(sm_rounded)
            except ValueError:
                self._sched_hour.set("Disabled")
        else:
            self._sched_hour.set("Disabled")

    def _save_config(self):
        old_zomboid_dir = self.config.zomboid_dir
        e = self._settings_entries

        self.config.server_dir    = e["server_dir"].get()
        self.config.zomboid_dir   = e["zomboid_dir"].get()
        self.config.rcon_path     = e["rcon_path"].get()
        self.config.server_name   = e["server_name"].get()
        self.config.server_ip     = e["server_ip"].get()
        self.config.password      = e["password"].get()
        try:
            self.config.check_interval = int(e["check_interval"].get())
        except ValueError:
            self.config.check_interval = 60
        self.config.steamcmd_path = e["steamcmd_path"].get()
        try:
            self.config.server_update_interval = int(e["server_update_interval"].get())
        except ValueError:
            self.config.server_update_interval = 60
        try:
            self.config.steamcmd_timeout = int(e["steamcmd_timeout"].get())
        except ValueError:
            self.config.steamcmd_timeout = 600

        hour_val = self._sched_hour.get()
        self.config.scheduled_restart = (
            "" if hour_val == "Disabled"
            else f"{hour_val}:{self._sched_minute.get()}"
        )

        self.config.save()
        self._log("Configuration saved.")

        # Restart log tail if the directory changed
        if self.config.zomboid_dir != old_zomboid_dir:
            self._restart_log_tail()

    # ------------------------------------------------------------------
    # Server control
    # ------------------------------------------------------------------

    def _check_server_status_threaded(self):
        self.status_label.configure(text="Checking…", text_color="orange")
        threading.Thread(target=self._check_server_status, daemon=True).start()

    def _check_server_status(self) -> bool:
        try:
            running = self.server.is_running()
        except Exception as exc:
            self.after(0, lambda: self.status_label.configure(
                text=f"Error: {exc}", text_color="red"
            ))
            return False

        if running:
            self.after(0, lambda: self.status_label.configure(
                text="Running  ●", text_color="#22cc55"
            ))
        else:
            self.after(0, lambda: self.status_label.configure(
                text="Stopped  ●", text_color="#cc2222"
            ))
        return running

    def _start_server(self):
        self._log("Checking for existing server process…")

        def _do():
            try:
                if self.server.is_running():
                    self.after(0, lambda: self._log(
                        "Server is already running — not starting a second instance."
                    ))
                    self.after(0, lambda: self.status_label.configure(
                        text="Running  ●", text_color="#22cc55"
                    ))
                    return
                self.server.start()
                self.after(0, lambda: self._log("Server is launching…"))
                self.after(0, lambda: self.status_label.configure(
                    text="Starting…", text_color="orange"
                ))
                self.after(5000, self._check_server_status_threaded)
            except Exception as exc:
                self.after(0, lambda: self._log(f"Start failed: {exc}"))

        threading.Thread(target=_do, daemon=True).start()

    def _stop_server(self):
        self._log("Sending stop command…")

        def _do():
            try:
                self.server.stop()
                self.after(0, lambda: self._log("Stop command sent."))
                self.after(6000, self._check_server_status_threaded)
            except Exception as exc:
                self.after(0, lambda: self._log(f"Stop failed: {exc}"))

        threading.Thread(target=_do, daemon=True).start()

    def _restart_server(self):
        self._log("Restarting server…")

        def _do():
            try:
                self.server.stop()
                import time as _time
                _time.sleep(6)
                self.server.start()
                self.after(0, lambda: self._log("Server restarted."))
                self.after(5000, self._check_server_status_threaded)
            except Exception as exc:
                self.after(0, lambda: self._log(f"Restart failed: {exc}"))

        threading.Thread(target=_do, daemon=True).start()

    # ------------------------------------------------------------------
    # Auto-refresh server status (#6)
    # ------------------------------------------------------------------

    def _start_status_poll(self):
        self._poll_status_job = self.after(_STATUS_POLL_MS, self._poll_status)

    def _poll_status(self):
        threading.Thread(target=self._check_server_status, daemon=True).start()
        self._poll_status_job = self.after(_STATUS_POLL_MS, self._poll_status)

    # ------------------------------------------------------------------
    # Scheduled daily restart (#14)
    # ------------------------------------------------------------------

    def _start_sched_poll(self):
        self._sched_job = self.after(_SCHED_POLL_MS, self._check_scheduled_restart)

    def _check_scheduled_restart(self):
        sched = self.config.scheduled_restart.strip()
        if sched:
            try:
                sh, sm = map(int, sched.split(":"))
                now = datetime.now()
                today = now.date()
                if (
                    now.hour == sh
                    and now.minute == sm
                    and self._last_sched_restart_date != today
                ):
                    self._last_sched_restart_date = today
                    self._log(f"Scheduled daily restart at {sched} triggered.")
                    self._restart_server()
            except ValueError:
                pass
        self._sched_job = self.after(_SCHED_POLL_MS, self._check_scheduled_restart)

    # ------------------------------------------------------------------
    # Check cycle helpers (shared by mod + server update pipelines)
    # ------------------------------------------------------------------

    def _resume_check_cycle(
        self, last_ts: str, interval_min: int, check_fn, label: str
    ) -> str:
        """
        Schedule the first run of a periodic check cycle on startup.
        Returns the after() job ID.

          - Never run  → first check in 10 s
          - Overdue    → check in 10 s
          - Time remaining → schedule for the remaining portion of the interval
        """
        if not last_ts:
            self._log(
                f"No previous {label} check found — scheduling first check in 10 s."
            )
            return self.after(10_000, check_fn)
        try:
            last = datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S")
            elapsed = (datetime.now() - last).total_seconds()
            remaining = interval_min * 60 - elapsed
            if remaining <= 30:
                self._log(f"{label} check is overdue — checking in 10 s.")
                return self.after(10_000, check_fn)
            mins, secs = divmod(int(remaining), 60)
            self._log(
                f"Resuming {label} check schedule — next check in {mins}m {secs}s."
            )
            return self.after(int(remaining * 1000), check_fn)
        except ValueError:
            return self.after(10_000, check_fn)

    # ------------------------------------------------------------------
    # Mod update checking (#1 & #3 — timer resumed on startup)
    # ------------------------------------------------------------------

    def _resume_auto_check(self):
        self._auto_check_job = self._resume_check_cycle(
            self.config.last_update,
            self.config.check_interval,
            self._check_mods_threaded,
            "mod",
        )

    def _check_mods_threaded(self):
        # Cancel any pending auto-check to prevent double-firing on manual trigger
        if self._auto_check_job:
            self.after_cancel(self._auto_check_job)
            self._auto_check_job = None

        self.mod_status_label.configure(text="Sending RCON command…", text_color="orange")
        self._log("Checking for mod updates…")
        threading.Thread(target=self._check_mods_worker, daemon=True).start()

    def _check_mods_worker(self):
        try:
            self.server.check_mods_need_update()
        except Exception as exc:
            self.after(0, lambda: self._log(f"RCON error: {exc}"))
            self.after(0, lambda: self.mod_status_label.configure(
                text=f"RCON error: {exc}", text_color="red"
            ))
            return

        zomboid_dir = self.config.zomboid_dir
        if not zomboid_dir:
            self.after(0, lambda: self.mod_status_label.configure(
                text="Zomboid log folder not set.", text_color="red"
            ))
            return

        parser = LogParser(zomboid_dir)
        log_file = parser.find_latest_log()
        if not log_file:
            self.after(0, lambda: self.mod_status_label.configure(
                text="No log file found in the specified folder.", text_color="red"
            ))
            return

        self.after(0, lambda: self._log(f"Monitoring: {os.path.basename(log_file)}"))
        self.after(0, lambda: self.mod_status_label.configure(
            text="Waiting for server response in log…", text_color="orange"
        ))

        status, mod_names = parser.monitor_for_mod_status(log_file)

        # Enrich raw workshop IDs with friendly names from the server config
        if status == "needs_update" and mod_names:
            mod_map = parser.load_server_mod_map(
                self.config.server_dir, self.config.server_name
            )
            if mod_map:
                mod_names = [
                    f"{mod_map[e.replace('Workshop ID: ', '')]}  "
                    f"(Workshop: {e.replace('Workshop ID: ', '')})"
                    if e.replace("Workshop ID: ", "") in mod_map else e
                    for e in mod_names
                ]

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.config.last_update = now
        self.config.save()

        if status == "needs_update":
            self.after(0, lambda: self._on_mods_need_update(mod_names, now))
        elif status == "up_to_date":
            self.after(0, lambda: self._on_mods_up_to_date(now))
        else:
            self.after(0, lambda: self._log(
                "Timed out waiting for mod status in log (300 s)."
            ))
            self.after(0, lambda: self.mod_status_label.configure(
                text="Timed out — no response in log.", text_color="red"
            ))

    def _on_mods_need_update(self, mod_names: list[str], timestamp: str):
        self.mod_status_label.configure(text="Updates available!", text_color="orange")
        self.last_check_label.configure(text=f"Last checked: {timestamp}")

        for widget in self._mod_list_frame.winfo_children():
            widget.destroy()

        if mod_names:
            self._mod_list_header.grid()
            self._mod_list_frame.grid()
            for name in mod_names:
                ctk.CTkLabel(
                    self._mod_list_frame, text=f"  •  {name}", anchor="w"
                ).grid(sticky="ew", padx=6, pady=2)
                self._log(f"  Mod: {name}")
        else:
            self._mod_list_header.grid_remove()
            self._mod_list_frame.grid_remove()
            self._log("  (mod names could not be extracted from log)")

        self._log("Mods need updating — starting 5-minute restart countdown.")
        self._start_restart_countdown()

    def _on_mods_up_to_date(self, timestamp: str):
        self.mod_status_label.configure(text="All mods up to date  ✓", text_color="#22cc55")
        self.last_check_label.configure(text=f"Last checked: {timestamp}")
        self._mod_list_header.grid_remove()
        self._mod_list_frame.grid_remove()
        self._log("All mods are up to date.")
        self._schedule_next_check()

    # ------------------------------------------------------------------
    # Restart countdown with in-game broadcasts (#2)
    # ------------------------------------------------------------------

    def _start_restart_countdown(
        self,
        seconds: int = 300,
        broadcast_messages: Optional[dict] = None,
        action=None,
        post_fn=None,
    ):
        self._countdown_remaining = seconds
        self._countdown_active = True
        self._countdown_broadcast_messages = (
            broadcast_messages if broadcast_messages is not None else _BROADCAST_AT
        )
        self._countdown_action = action if action is not None else self._restart_server
        self._countdown_post_fn = (
            post_fn if post_fn is not None else self._schedule_next_check
        )
        self._tick_countdown()

    def _tick_countdown(self):
        if not self._countdown_active:
            return

        remaining = self._countdown_remaining
        if remaining <= 0:
            self._countdown_active = False
            self.countdown_label.configure(text="")
            self._log("Restart countdown finished.")
            action = self._countdown_action or self._restart_server
            post_fn = self._countdown_post_fn or self._schedule_next_check
            action()
            post_fn()
            return

        mins, secs = divmod(remaining, 60)
        self.countdown_label.configure(
            text=f"Restarting in  {mins:02d}:{secs:02d}"
            f"  — checking player count each minute"
        )

        # Broadcast in-game warnings at key thresholds
        msgs = self._countdown_broadcast_messages or _BROADCAST_AT
        if remaining in msgs:
            msg = msgs[remaining]
            self._log(f"Broadcasting to players: {msg}")
            threading.Thread(
                target=lambda m=msg: self._broadcast_safe(m), daemon=True
            ).start()

        # Player count check every full minute
        if remaining % 60 == 0:
            threading.Thread(target=self._maybe_early_restart, daemon=True).start()

        self._countdown_remaining -= 1
        self.after(1000, self._tick_countdown)

    def _broadcast_safe(self, message: str):
        try:
            self.server.broadcast(message)
        except Exception as exc:
            self.after(0, lambda: self._log(f"Broadcast failed: {exc}"))

    def _maybe_early_restart(self):
        try:
            count = self.server.get_player_count()
            self.after(0, lambda: self._log(f"Players currently online: {count}"))
            if count == 0:
                self.after(0, lambda: self._log(
                    "No players online — triggering early restart."
                ))
                self._countdown_remaining = 0
        except Exception as exc:
            self.after(0, lambda: self._log(f"Player count check failed: {exc}"))

    def _schedule_next_check(self):
        if self._auto_check_job:
            self.after_cancel(self._auto_check_job)
        interval_ms = self.config.check_interval * 60 * 1000
        self._auto_check_job = self.after(interval_ms, self._check_mods_threaded)
        self._log(
            f"Next mod check scheduled in {self.config.check_interval} minute(s)."
        )

    # ------------------------------------------------------------------
    # Server update pipeline (mirrors mod update pipeline)
    # ------------------------------------------------------------------

    def _resume_server_update_check(self):
        self._server_update_check_job = self._resume_check_cycle(
            self.config.last_server_update_check,
            self.config.server_update_interval,
            self._check_server_update_threaded,
            "server update",
        )

    def _check_server_update_threaded(self):
        # Validate SteamCMD path before doing anything
        if not self.config.steamcmd_path or not os.path.isfile(self.config.steamcmd_path):
            self._log(
                "SteamCMD path not configured or not found — set it in Settings."
            )
            return

        # Skip if a countdown is already running
        if self._countdown_active:
            self._log(
                "Skipping server update check — restart already in progress."
            )
            interval_ms = self.config.server_update_interval * 60 * 1000
            self._server_update_check_job = self.after(
                interval_ms, self._check_server_update_threaded
            )
            return

        # Cancel any pending scheduled check to prevent double-firing
        if self._server_update_check_job:
            self.after_cancel(self._server_update_check_job)
            self._server_update_check_job = None

        self.server_update_status_label.configure(
            text="Checking…", text_color="orange"
        )
        self._log("Checking for server updates…")
        threading.Thread(target=self._check_server_update_worker, daemon=True).start()

    def _check_server_update_worker(self):
        result = self._server_update_checker.update_needed()

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.config.last_server_update_check = now
        self.config.save()

        if result is True:
            self.after(0, lambda: self._on_server_update_needed(now))
        elif result is False:
            self.after(0, lambda: self._on_server_up_to_date(now))
        else:
            # Check failed (None) — log and reschedule
            self.after(0, lambda: self.server_update_status_label.configure(
                text="Check failed — see log", text_color="red"
            ))
            self.after(0, lambda: self.server_update_last_check_label.configure(
                text=f"Last checked: {now} (check failed)"
            ))
            self.after(0, lambda: self._log(
                "Server update check failed — will retry next interval."
            ))
            self.after(0, self._schedule_next_server_update_check)

    def _on_server_update_needed(self, timestamp: str):
        self.server_update_status_label.configure(
            text="Update available!", text_color="orange"
        )
        self.server_update_last_check_label.configure(
            text=f"Last checked: {timestamp}"
        )
        self._log("Server update available — starting 5-minute restart countdown.")
        self._start_restart_countdown(
            broadcast_messages=_SERVER_UPDATE_BROADCAST_AT,
            action=self._run_server_update_then_restart,
            post_fn=self._schedule_next_server_update_check,
        )

    def _on_server_up_to_date(self, timestamp: str):
        self.server_update_status_label.configure(
            text="Server up to date  ✓", text_color="#22cc55"
        )
        self.server_update_last_check_label.configure(
            text=f"Last checked: {timestamp}"
        )
        self._log("Server is up to date.")
        self._schedule_next_server_update_check()

    def _schedule_next_server_update_check(self):
        if self._server_update_check_job:
            self.after_cancel(self._server_update_check_job)
        interval_ms = self.config.server_update_interval * 60 * 1000
        self._server_update_check_job = self.after(
            interval_ms, self._check_server_update_threaded
        )
        self._log(
            f"Next server update check scheduled in "
            f"{self.config.server_update_interval} minute(s)."
        )

    def _run_server_update_then_restart(self):
        """
        Stop server → wait for clean exit → run SteamCMD → restart.
        Always restarts even if SteamCMD fails (keeps players from being locked out).
        Called from the main thread; all blocking work done in a daemon thread.
        """
        self._log("Stopping server for SteamCMD update…")

        def _do():
            import time as _time

            # Stop the server
            try:
                self.server.stop()
                self.after(0, lambda: self._log(
                    "Stop command sent — waiting for server to exit…"
                ))
            except Exception as exc:
                self.after(0, lambda: self._log(f"Stop failed: {exc}"))
                return

            # Poll until stopped or 30s timeout (Issue 6A)
            stopped = False
            for _ in range(15):  # 15 × 2s = 30s
                _time.sleep(2)
                try:
                    if not self.server.is_running():
                        stopped = True
                        break
                except Exception:
                    pass

            if not stopped:
                self.after(0, lambda: self._log(
                    "Server did not stop within 30s — aborting SteamCMD update. "
                    "Stop the server manually and try again."
                ))
                self.after(0, self._schedule_next_server_update_check)
                return

            self.after(0, lambda: self._log(
                "Server stopped. Running SteamCMD update…"
            ))
            self.after(0, lambda: self.server_update_status_label.configure(
                text="Updating…", text_color="orange"
            ))

            # Run SteamCMD update
            success = self._server_update_checker.run_update()

            if success:
                self.after(0, lambda: self._log(
                    "SteamCMD update completed. Starting server…"
                ))
                self.after(0, lambda: self.server_update_status_label.configure(
                    text="Updated  ✓", text_color="#22cc55"
                ))
            else:
                self.after(0, lambda: self._log(
                    "Server update failed — restarting on current version. "
                    "Check the activity log and SteamCMD path."
                ))
                self.after(0, lambda: self.server_update_status_label.configure(
                    text="Update failed — restarted", text_color="red"
                ))

            # Always bring the server back up (Issue 2A)
            try:
                self.server.start()
                self.after(0, lambda: self._log("Server start command sent."))
                self.after(5000, self._check_server_status_threaded)
            except Exception as exc:
                self.after(0, lambda: self._log(f"Server start failed: {exc}"))

        threading.Thread(target=_do, daemon=True).start()

    # ------------------------------------------------------------------
    # RCON Console (#4)
    # ------------------------------------------------------------------

    def _send_rcon_command(self):
        command = self.rcon_input.get().strip()
        if not command:
            return
        self.rcon_input.delete(0, "end")
        self._rcon_append(f"> {command}")

        def _do():
            try:
                response = self.server.send_command(command)
                self.after(0, lambda r=response: self._rcon_append(r))
            except Exception as exc:
                self.after(0, lambda: self._rcon_append(f"Error: {exc}"))

        threading.Thread(target=_do, daemon=True).start()

    def _rcon_append(self, text: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.rcon_output.configure(state="normal")
        self.rcon_output.insert("end", f"[{timestamp}] {text}\n")
        self.rcon_output.see("end")
        self.rcon_output.configure(state="disabled")

    # ------------------------------------------------------------------
    # Live server log viewer (#5)
    # ------------------------------------------------------------------

    def _start_log_tail(self):
        if not self.config.zomboid_dir:
            return
        self._stop_log_tail()
        self._log_tailer = LogTailer(self.config.zomboid_dir, self._enqueue_log_line)
        self._log_tailer.start()

    def _stop_log_tail(self):
        if self._log_tailer:
            self._log_tailer.stop()
            self._log_tailer = None

    def _restart_log_tail(self):
        self._stop_log_tail()
        self._start_log_tail()

    def _enqueue_log_line(self, line: str):
        """Called from the LogTailer background thread — never touches the UI directly."""
        self._server_log_queue.put(line)

    def _drain_log_queue(self):
        """
        Runs on the main thread every 250 ms.
        Batches all pending log lines into a single UI insert so we don't
        flood the event loop with individual after() calls.
        """
        lines: list[str] = []
        try:
            while True:
                lines.append(self._server_log_queue.get_nowait())
        except queue.Empty:
            pass

        if lines:
            text = "".join(lines)
            self.server_log_box.configure(state="normal")
            self.server_log_box.insert("end", text)
            self.server_log_box.see("end")
            self.server_log_box.configure(state="disabled")

            if self._log_tailer and self._log_tailer.current_file:
                fn = os.path.basename(self._log_tailer.current_file)
                self._log_file_label.configure(
                    text=f"Tailing: {fn}", text_color="#22cc55"
                )

        self.after(250, self._drain_log_queue)

    # ------------------------------------------------------------------
    # System tray
    # ------------------------------------------------------------------

    def _minimize_to_tray(self):
        self.withdraw()
        if self._tray_icon:
            return

        image = self._make_tray_image()
        menu = pystray.Menu(
            pystray.MenuItem("Show", self._show_from_tray, default=True),
            pystray.MenuItem("Quit", self._quit_from_tray),
        )
        self._tray_icon = pystray.Icon(
            "PZ Manager", image, "PZ Server Manager", menu
        )
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def _show_from_tray(self, icon=None, item=None):
        self.after(0, self.deiconify)
        self.after(0, self.lift)
        self.after(0, self.focus_force)

    def _quit_from_tray(self, icon=None, item=None):
        self._stop_log_tail()
        if self._tray_icon:
            self._tray_icon.stop()
            self._tray_icon = None
        self.after(0, self.destroy)

    @staticmethod
    def _make_tray_image() -> Image.Image:
        img = Image.new("RGB", (64, 64), "#1a1a2e")
        draw = ImageDraw.Draw(img)
        draw.rectangle([4, 4, 60, 60], outline="#4a9eff", width=3)
        draw.rectangle([18, 18, 46, 46], fill="#4a9eff")
        return img

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        text = f"[{timestamp}] {message}\n"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        logger.info(message)

    def _clear_textbox(self, box: ctk.CTkTextbox):
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.configure(state="disabled")

    def _pick_dir(self, entry: ctk.CTkEntry):
        path = filedialog.askdirectory()
        if path:
            entry.delete(0, "end")
            entry.insert(0, path)

    def _pick_file(self, entry: ctk.CTkEntry):
        path = filedialog.askopenfilename(filetypes=[("Executable", "*.exe")])
        if path:
            entry.delete(0, "end")
            entry.insert(0, path)
