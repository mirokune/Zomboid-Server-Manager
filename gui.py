"""
gui.py — Project Zomboid Server Manager
PyQt6-based UI.  backend.py is intentionally untouched.

Architecture overview:
┌─ QMainWindow ──────────────────────────────────────────────────────┐
│  QTabWidget (styled via QSS to look like a toolbar tab bar)        │
│  ┌─ Server Control ──────────────────────────────────────────────┐ │
│  │  QSplitter (left 260 px fixed │ right flex)                   │ │
│  │  Left:  Status card / Countdown banner / Mod card / Game card │ │
│  │  Right: LogViewer (activity log, dominant)                    │ │
│  ├─ RCON Console ─────────────────────────────────────────────── │ │
│  ├─ Server Log ──────────────────────────────────────────────────│ │
│  └─ Settings ───────────────────────────────────────────────────  │ │
│  QStatusBar                                                        │ │
└────────────────────────────────────────────────────────────────────┘

Threading model:
  All backend calls run in daemon threads (QThreadPool / raw threading.Thread).
  UI updates cross the thread boundary via pyqtSignal (queued connection,
  automatic because emitter and receiver live in different Qt threads).
  LogTailer (backend, unchanged) is bridged via LogBridge(QObject).
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections import deque
from datetime import date, datetime
from typing import Optional

from PyQt6.QtCore import (
    QObject,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPixmap,
    QTextCharFormat,
    QTextCursor,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QSystemTrayIcon,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from backend import AppConfig, LogParser, LogTailer, ServerManager, ServerUpdateChecker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Broadcast thresholds (seconds remaining → in-game message)
# ---------------------------------------------------------------------------

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

_STATUS_POLL_MS = 60_000   # auto-refresh server status every 60 s
_SCHED_POLL_MS  = 60_000   # check scheduled restart every 60 s

# ---------------------------------------------------------------------------
# Log coloring: compiled once, applied per line in LogViewer.append_line()
# ---------------------------------------------------------------------------

_LOG_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"error|failed|exception", re.I), "error"),
    (re.compile(r"broadcast|warning|countdown",  re.I), "warn"),
    (re.compile(r"checking|resuming|scheduling", re.I), "info"),
    (re.compile(r"up to date|all mods|restarted|completed|saved", re.I), "success"),
]


def _classify_line(msg: str) -> str:
    for pattern, level in _LOG_PATTERNS:
        if pattern.search(msg):
            return level
    return "default"


# ---------------------------------------------------------------------------
# QSS stylesheet — one place to update the entire look
# ---------------------------------------------------------------------------

APP_STYLESHEET = """
/* ── Base ────────────────────────────────────────────────────────────── */
QMainWindow, QWidget {
    background: #1a1a1a;
    color: #d0d0d0;
    font-family: "Segoe UI", system-ui, sans-serif;
    font-size: 13px;
}

/* ── Tab bar (Portainer-style underline tabs) ─────────────────────────── */
QTabWidget::pane {
    border: none;
    background: #1a1a1a;
}
QTabBar::tab {
    background: transparent;
    color: #666;
    padding: 8px 18px;
    border: none;
    border-bottom: 2px solid transparent;
    margin-right: 2px;
}
QTabBar::tab:selected {
    color: #e0e0e0;
    border-bottom: 2px solid #4d9de0;
}
QTabBar::tab:hover:!selected {
    color: #aaa;
    background: #222;
}

/* ── GroupBox cards ──────────────────────────────────────────────────── */
QGroupBox {
    border: 1px solid #2e2e2e;
    border-radius: 6px;
    background: #1e1e1e;
    margin-top: 8px;
    padding-top: 8px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    color: #666;
    font-size: 11px;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    left: 10px;
    top: -1px;
}

/* ── Buttons ──────────────────────────────────────────────────────────── */
QPushButton {
    background: #2a2a2a;
    border: 1px solid #3a3a3a;
    border-radius: 5px;
    color: #ccc;
    padding: 6px 14px;
}
QPushButton:hover   { background: #333; border-color: #4a4a4a; }
QPushButton:pressed { background: #222; }
QPushButton:disabled { opacity: 0.4; }

QPushButton[variant="start"] { background: #1e4620; border-color: #2d6b32; color: #7ecf7e; }
QPushButton[variant="start"]:hover { background: #235225; }
QPushButton[variant="stop"]  { background: #3d1818; border-color: #6b2d2d; color: #cf7e7e; }
QPushButton[variant="stop"]:hover  { background: #4a1e1e; }
QPushButton[variant="warn"]  { background: #3d2d10; border-color: #6b4d20; color: #cfb07e; }
QPushButton[variant="warn"]:hover  { background: #4a3515; }

/* ── Inputs ───────────────────────────────────────────────────────────── */
QLineEdit, QComboBox {
    background: #252525;
    border: 1px solid #3a3a3a;
    border-radius: 4px;
    color: #d0d0d0;
    padding: 5px 8px;
    selection-background-color: #4d9de0;
}
QLineEdit:focus, QComboBox:focus { border-color: #4d9de0; }
QComboBox::drop-down { border: none; }
QComboBox QAbstractItemView { background: #252525; border: 1px solid #3a3a3a; }

/* ── Scrollbars ───────────────────────────────────────────────────────── */
QScrollBar:vertical {
    background: #1a1a1a; width: 8px; margin: 0;
}
QScrollBar::handle:vertical {
    background: #3a3a3a; border-radius: 4px; min-height: 20px;
}
QScrollBar::handle:vertical:hover { background: #4a4a4a; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

/* ── Log / text boxes ─────────────────────────────────────────────────── */
QTextEdit {
    background: #161616;
    border: 1px solid #272727;
    border-radius: 5px;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 12px;
    selection-background-color: #4d9de0;
}

/* ── Countdown banner ─────────────────────────────────────────────────── */
QFrame#countdown {
    background: #2d2010;
    border: 1px solid #6b4a10;
    border-radius: 5px;
}

/* ── Status bar ───────────────────────────────────────────────────────── */
QStatusBar {
    background: #111;
    border-top: 1px solid #2a2a2a;
    color: #555;
    font-size: 11px;
}
QStatusBar::item { border: none; }

/* ── Splitter handle ──────────────────────────────────────────────────── */
QSplitter::handle { background: #2e2e2e; width: 1px; }

/* ── Checkbox ─────────────────────────────────────────────────────────── */
QCheckBox { color: #ccc; spacing: 6px; }
QCheckBox::indicator {
    width: 14px; height: 14px;
    border: 1px solid #3a3a3a; border-radius: 3px;
    background: #252525;
}
QCheckBox::indicator:checked { background: #4d9de0; border-color: #4d9de0; }
"""


# ---------------------------------------------------------------------------
# LogBridge — routes LogTailer callbacks (raw thread) into Qt signals
# ---------------------------------------------------------------------------

class LogBridge(QObject):
    """
    Thin QObject adapter so LogTailer (which runs in a raw threading.Thread
    and calls line_callback from that thread) can safely deliver log lines
    to the main-thread UI.

    Qt automatically uses a QueuedConnection when emit() is called from a
    thread different from the receiver's thread — no explicit connection type
    needed.
    """
    line_received = pyqtSignal(str)


# ---------------------------------------------------------------------------
# LogViewer — shared log display widget used by both activity log + server log
# ---------------------------------------------------------------------------

class LogViewer(QTextEdit):
    """
    Read-only QTextEdit with:
      - Per-line timestamp prefix [HH:MM:SS]
      - Color coding via QTextCharFormat (O(1) per line, no HTML parsing)
      - Auto-scroll (only when already at bottom — respects manual scroll)
      - 5000-line cap via setMaximumBlockCount
    """

    COLORS: dict[str, str] = {
        "error":   "#e05252",
        "warn":    "#f0a030",
        "info":    "#4d9de0",
        "success": "#3ddc84",
        "default": "#b0b0b0",
    }

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.document().setMaximumBlockCount(5000)

    def append_line(self, msg: str, level: str = "default") -> None:
        """Append a timestamped, color-coded line. Safe to call from main thread only."""
        ts = datetime.now().strftime("%H:%M:%S")
        color = self.COLORS.get(level, self.COLORS["default"])

        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))

        # Timestamp in muted grey
        ts_fmt = QTextCharFormat()
        ts_fmt.setForeground(QColor("#444"))

        sb = self.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4

        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(f"[{ts}] ", ts_fmt)
        cursor.insertText(f"{msg}\n", fmt)

        if at_bottom:
            sb.setValue(sb.maximum())

    def clear_log(self) -> None:
        self.clear()


# ---------------------------------------------------------------------------
# RconLineEdit — QLineEdit with Up/Down command history
# ---------------------------------------------------------------------------

class RconLineEdit(QLineEdit):
    """
    QLineEdit subclass that adds terminal-style Up/Down command history.
    History is session-only (not persisted to disk).
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._history: deque[str] = deque(maxlen=50)
        self._history_pos: int = -1  # -1 = at current input

    def push_history(self, command: str) -> None:
        if command and (not self._history or self._history[0] != command):
            self._history.appendleft(command)
        self._history_pos = -1

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Up:
            if self._history and self._history_pos < len(self._history) - 1:
                self._history_pos += 1
                self.setText(self._history[self._history_pos])
                self.end(False)
        elif event.key() == Qt.Key.Key_Down:
            if self._history_pos > 0:
                self._history_pos -= 1
                self.setText(self._history[self._history_pos])
                self.end(False)
            elif self._history_pos == 0:
                self._history_pos = -1
                self.clear()
        else:
            super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------

class App(QMainWindow):
    """
    PyQt6 main window. QApplication is created in main.py before this is
    instantiated (idiomatic Qt — QApplication must exist before any QWidget).
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Project Zomboid Server Manager")
        self.resize(900, 720)
        self.setMinimumSize(660, 540)
        self.setWindowIcon(_make_icon())

        # Backend
        self.config = AppConfig()
        self.config.load()
        self.server = ServerManager(self.config)

        # State
        self._log_tailer: Optional[LogTailer] = None
        self._log_bridge = LogBridge()
        self._log_bridge.line_received.connect(self._on_server_log_line)

        self._countdown_remaining: int = 0
        self._countdown_active: bool = False
        self._countdown_broadcast_messages: dict = _BROADCAST_AT
        self._countdown_action = None
        self._countdown_post_fn = None

        self._last_sched_restart_date: Optional[date] = None
        self._server_running: Optional[bool] = None  # None = not yet known
        self._status_check_in_flight: bool = False   # prevents concurrent checks piling up
        self._auto_check_job: Optional[QTimer] = None
        self._server_update_check_job: Optional[QTimer] = None
        self._poll_status_job: Optional[QTimer] = None
        self._sched_job: Optional[QTimer] = None
        self._countdown_timer: Optional[QTimer] = None

        self._server_update_checker = ServerUpdateChecker(self.config)

        self._settings_dirty = False

        self.setStyleSheet(APP_STYLESHEET)
        self._build_ui()
        self._load_config_into_ui()
        self._setup_tray()

        # Deferred startup — let window render first
        QTimer.singleShot(400, self._check_server_status_threaded)
        if self._config_is_ready():
            QTimer.singleShot(600, self._resume_auto_check)
            QTimer.singleShot(700, self._resume_server_update_check)
        QTimer.singleShot(800, self._start_log_tail)
        self._start_status_poll()
        self._start_sched_poll()

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)  # removes box around tab bar
        main_layout.addWidget(self.tabs)

        self._build_control_tab()
        self._build_rcon_tab()
        self._build_log_tab()
        self._build_settings_tab()

        # Status bar
        self._statusbar = QStatusBar()
        self.setStatusBar(self._statusbar)
        self._sb_server = QLabel("● Server Unknown")
        self._sb_server.setStyleSheet("color: #555; margin-right: 16px;")
        self._sb_info = QLabel("")
        self._sb_info.setStyleSheet("color: #444;")
        self._sb_next_check = QLabel("")
        self._sb_next_check.setStyleSheet("color: #444; margin-left: 16px;")
        self._statusbar.addPermanentWidget(self._sb_server)
        self._statusbar.addPermanentWidget(self._sb_info)
        self._statusbar.addPermanentWidget(self._sb_next_check)

    # ── Server Control tab ────────────────────────────────────────────

    def _build_control_tab(self) -> None:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)

        # ── Left panel ──────────────────────────────────────────────
        left = QWidget()
        left.setFixedWidth(264)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(8)

        # Server status card
        status_box = QGroupBox("Server Status")
        status_inner = QVBoxLayout(status_box)
        status_inner.setContentsMargins(12, 16, 12, 12)
        status_inner.setSpacing(8)

        status_row = QHBoxLayout()
        self._status_dot = QLabel("●")
        self._status_dot.setStyleSheet("color: #666; font-size: 18px;")
        self._status_text = QLabel("Checking…")
        self._status_text.setStyleSheet("color: #aaa; font-size: 14px; font-weight: bold;")
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.setFixedWidth(72)
        self._refresh_btn.clicked.connect(self._check_server_status_threaded)
        status_row.addWidget(self._status_dot)
        status_row.addWidget(self._status_text)
        status_row.addStretch()
        status_row.addWidget(self._refresh_btn)
        status_inner.addLayout(status_row)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self._start_btn = QPushButton("Start")
        self._start_btn.setProperty("variant", "start")
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setProperty("variant", "stop")
        self._restart_btn = QPushButton("Restart")
        self._restart_btn.setProperty("variant", "warn")
        for btn in (self._start_btn, self._stop_btn, self._restart_btn):
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn_row.addWidget(btn)
        self._start_btn.clicked.connect(self._start_server)
        self._stop_btn.clicked.connect(self._stop_server)
        self._restart_btn.clicked.connect(self._restart_server)
        status_inner.addLayout(btn_row)
        left_layout.addWidget(status_box)

        # Countdown banner + spacer (swap to avoid reflow)
        self._countdown_spacer = QFrame()
        self._countdown_spacer.setFixedHeight(64)
        self._countdown_spacer.setVisible(True)
        left_layout.addWidget(self._countdown_spacer)

        self._countdown_frame = QFrame()
        self._countdown_frame.setObjectName("countdown")
        self._countdown_frame.setFixedHeight(64)
        self._countdown_frame.setVisible(False)
        cd_layout = QHBoxLayout(self._countdown_frame)
        cd_layout.setContentsMargins(12, 8, 12, 8)
        cd_left = QVBoxLayout()
        self._cd_label = QLabel("Restarting…")
        self._cd_label.setStyleSheet("color: #888; font-size: 11px;")
        self._cd_timer_label = QLabel("05:00")
        self._cd_timer_label.setStyleSheet(
            "color: #f0a030; font-size: 20px; font-weight: bold;"
        )
        cd_left.addWidget(self._cd_label)
        cd_left.addWidget(self._cd_timer_label)
        cd_right = QVBoxLayout()
        cd_right.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._cd_players_label = QLabel("Players")
        self._cd_players_label.setStyleSheet("color: #666; font-size: 11px; text-align: right;")
        self._cd_players_count = QLabel("–")
        self._cd_players_count.setStyleSheet(
            "color: #f0a030; font-size: 16px; font-weight: bold; text-align: right;"
        )
        cd_right.addWidget(self._cd_players_label)
        cd_right.addWidget(self._cd_players_count)
        self._cd_cancel_btn = QPushButton("Cancel Restart")
        self._cd_cancel_btn.setFixedHeight(28)
        self._cd_cancel_btn.setStyleSheet(
            "QPushButton { background: #444; color: #ccc; border: 1px solid #666;"
            " border-radius: 4px; padding: 0 10px; font-size: 11px; }"
            "QPushButton:hover { background: #555; color: #fff; }"
        )
        self._cd_cancel_btn.clicked.connect(self._cancel_countdown)
        cd_layout.addLayout(cd_left)
        cd_layout.addStretch()
        cd_layout.addLayout(cd_right)
        cd_layout.addSpacing(10)
        cd_layout.addWidget(self._cd_cancel_btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        left_layout.addWidget(self._countdown_frame)

        # Mod updates card
        mod_box = QGroupBox("Mod Updates")
        mod_inner = QVBoxLayout(mod_box)
        mod_inner.setContentsMargins(12, 16, 12, 12)
        mod_inner.setSpacing(4)

        mod_top = QHBoxLayout()
        self._mod_status_label = QLabel("Not checked")
        self._mod_status_label.setStyleSheet("color: #666;")
        self._check_mods_btn = QPushButton("Check Now")
        self._check_mods_btn.setFixedWidth(88)
        self._check_mods_btn.clicked.connect(self._check_mods_threaded)
        mod_top.addWidget(self._mod_status_label)
        mod_top.addStretch()
        mod_top.addWidget(self._check_mods_btn)
        mod_inner.addLayout(mod_top)

        self._mod_last_label = QLabel("Last checked: Never")
        self._mod_last_label.setStyleSheet("color: #444; font-size: 11px;")
        mod_inner.addWidget(self._mod_last_label)

        self._mod_list_widget = QLabel("")
        self._mod_list_widget.setWordWrap(True)
        self._mod_list_widget.setStyleSheet("color: #aaa; font-size: 11px; margin-top: 4px;")
        self._mod_list_widget.setVisible(False)
        mod_inner.addWidget(self._mod_list_widget)

        left_layout.addWidget(mod_box)

        # Game update card
        game_box = QGroupBox("Game Update")
        game_inner = QVBoxLayout(game_box)
        game_inner.setContentsMargins(12, 16, 12, 12)
        game_inner.setSpacing(4)

        game_top = QHBoxLayout()
        self._server_update_label = QLabel("Not checked")
        self._server_update_label.setStyleSheet("color: #666;")
        self._check_server_update_btn = QPushButton("Check Now")
        self._check_server_update_btn.setFixedWidth(88)
        self._check_server_update_btn.clicked.connect(self._check_server_update_threaded)
        game_top.addWidget(self._server_update_label)
        game_top.addStretch()
        game_top.addWidget(self._check_server_update_btn)
        game_inner.addLayout(game_top)

        self._server_update_last_label = QLabel("Last checked: Never")
        self._server_update_last_label.setStyleSheet("color: #444; font-size: 11px;")
        game_inner.addWidget(self._server_update_last_label)
        left_layout.addWidget(game_box)

        left_layout.addStretch()
        splitter.addWidget(left)

        # ── Right panel: activity log ────────────────────────────────
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        log_header = QHBoxLayout()
        log_title = QLabel("ACTIVITY LOG")
        log_title.setStyleSheet(
            "color: #555; font-size: 11px; font-weight: bold; letter-spacing: 0.5px;"
        )
        clear_log_btn = QPushButton("Clear")
        clear_log_btn.setFixedWidth(60)
        log_header.addWidget(log_title)
        log_header.addStretch()
        log_header.addWidget(clear_log_btn)
        right_layout.addLayout(log_header)

        self.activity_log = LogViewer()
        clear_log_btn.clicked.connect(self.activity_log.clear_log)
        right_layout.addWidget(self.activity_log)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        tab_layout.addWidget(splitter)
        self.tabs.addTab(tab, "Server Control")

    # ── RCON Console tab ──────────────────────────────────────────────

    def _build_rcon_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        input_row = QHBoxLayout()
        self.rcon_input = RconLineEdit()
        self.rcon_input.setPlaceholderText(
            'Enter RCON command  (e.g.  players  |  save  |  servermsg "text")…'
        )
        self.rcon_input.returnPressed.connect(self._send_rcon_command)
        send_btn = QPushButton("Send")
        send_btn.setFixedWidth(72)
        send_btn.clicked.connect(self._send_rcon_command)
        input_row.addWidget(self.rcon_input)
        input_row.addWidget(send_btn)
        layout.addLayout(input_row)

        self.rcon_output = LogViewer()
        layout.addWidget(self.rcon_output)

        clear_btn = QPushButton("Clear Output")
        clear_btn.setFixedWidth(110)
        clear_btn.clicked.connect(self.rcon_output.clear_log)
        bottom = QHBoxLayout()
        bottom.addStretch()
        bottom.addWidget(clear_btn)
        layout.addLayout(bottom)

        self.tabs.addTab(tab, "RCON Console")

    # ── Server Log tab ────────────────────────────────────────────────

    def _build_log_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        ctrl = QHBoxLayout()
        self._log_file_label = QLabel("Not tailing any log file.")
        self._log_file_label.setStyleSheet("color: #555; font-size: 11px;")
        restart_tail_btn = QPushButton("Restart Tail")
        restart_tail_btn.setFixedWidth(100)
        restart_tail_btn.clicked.connect(self._restart_log_tail)
        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(60)

        self._log_unconfigured = QWidget()
        unconf_layout = QHBoxLayout(self._log_unconfigured)
        unconf_layout.setContentsMargins(0, 0, 0, 0)
        unconf_lbl = QLabel("No log directory configured.")
        unconf_lbl.setStyleSheet("color: #555;")
        open_settings_btn = QPushButton("Open Settings")
        open_settings_btn.clicked.connect(lambda: self.tabs.setCurrentIndex(3))
        unconf_layout.addWidget(unconf_lbl)
        unconf_layout.addWidget(open_settings_btn)
        unconf_layout.addStretch()

        ctrl.addWidget(self._log_file_label)
        ctrl.addStretch()
        ctrl.addWidget(self._log_unconfigured)
        ctrl.addWidget(restart_tail_btn)
        ctrl.addWidget(clear_btn)
        layout.addLayout(ctrl)

        self.server_log = LogViewer()
        clear_btn.clicked.connect(self.server_log.clear_log)
        layout.addWidget(self.server_log)

        self.tabs.addTab(tab, "Server Log")
        self._update_log_tab_state()

    def _update_log_tab_state(self) -> None:
        has_dir = bool(self.config.zomboid_dir)
        self._log_file_label.setVisible(has_dir)
        self._log_unconfigured.setVisible(not has_dir)

    # ── Settings tab ──────────────────────────────────────────────────

    def _build_settings_tab(self) -> None:
        outer = QWidget()
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(12, 12, 12, 12)
        outer_layout.setSpacing(8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        form_layout = QVBoxLayout(inner)
        form_layout.setSpacing(16)
        scroll.setWidget(inner)
        outer_layout.addWidget(scroll, 1)

        self._settings_entries: dict[str, QLineEdit] = {}

        def _add_group(title: str, rows: list[tuple[str, str, str]]) -> None:
            box = QGroupBox(title)
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(12, 16, 12, 12)
            box_layout.setSpacing(0)
            for label_text, key, kind in rows:
                row = QHBoxLayout()
                row.setSpacing(8)
                lbl = QLabel(label_text)
                lbl.setFixedWidth(200)
                lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                lbl.setStyleSheet("color: #888;")
                entry = QLineEdit()
                if kind == "password":
                    entry.setEchoMode(QLineEdit.EchoMode.Password)
                entry.textChanged.connect(self._on_settings_changed)
                self._settings_entries[key] = entry
                row.addWidget(lbl)
                row.addWidget(entry)
                if kind == "dir":
                    btn = QPushButton("Browse")
                    btn.setFixedWidth(72)
                    btn.clicked.connect(lambda _checked=False, e=entry: self._pick_dir(e))
                    row.addWidget(btn)
                elif kind == "file":
                    btn = QPushButton("Browse")
                    btn.setFixedWidth(72)
                    btn.clicked.connect(lambda _checked=False, e=entry: self._pick_file(e))
                    row.addWidget(btn)
                else:
                    spacer = QWidget()
                    spacer.setFixedWidth(80)
                    row.addWidget(spacer)
                box_layout.addLayout(row)
                # spacer between rows
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.HLine)
                sep.setStyleSheet("color: #252525;")
                sep.setFixedHeight(1)
                box_layout.addWidget(sep)
            form_layout.addWidget(box)

        _add_group("Paths", [
            ("PZ Server Folder:",   "server_dir",   "dir"),
            ("Zomboid Log Folder:", "zomboid_dir",  "dir"),
            ("RCON Executable:",    "rcon_path",    "file"),
            ("SteamCMD Path:",      "steamcmd_path","file"),
        ])
        _add_group("Connection", [
            ("Server Name:", "server_name", None),
            ("Server IP:",   "server_ip",   None),
            ("Password:",    "password",    "password"),
        ])
        self._settings_entries["server_ip"].setPlaceholderText("127.0.0.1:27015")
        self._settings_entries["server_name"].setPlaceholderText("e.g. servertest")
        self._settings_entries["password"].setPlaceholderText("RCON password from servertest.ini")

        # Schedule & Timing group (manual, not via _add_group — has combo boxes)
        sched_box = QGroupBox("Schedule & Timing")
        sched_layout = QVBoxLayout(sched_box)
        sched_layout.setContentsMargins(12, 16, 12, 12)
        sched_layout.setSpacing(0)

        def _timing_row(label_text: str, key: str) -> None:
            row = QHBoxLayout()
            row.setSpacing(8)
            lbl = QLabel(label_text)
            lbl.setFixedWidth(200)
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            lbl.setStyleSheet("color: #888;")
            entry = QLineEdit()
            entry.textChanged.connect(self._on_settings_changed)
            self._settings_entries[key] = entry
            spacer = QWidget()
            spacer.setFixedWidth(80)
            row.addWidget(lbl)
            row.addWidget(entry)
            row.addWidget(spacer)
            sched_layout.addLayout(row)
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet("color: #252525;")
            sep.setFixedHeight(1)
            sched_layout.addWidget(sep)

        _timing_row("Check Interval (min):",         "check_interval")
        _timing_row("Server Update Interval (min):", "server_update_interval")
        _timing_row("SteamCMD Timeout (sec):",       "steamcmd_timeout")

        # Steam branch row
        branch_row = QHBoxLayout()
        branch_row.setSpacing(8)
        branch_lbl = QLabel("Steam Branch:")
        branch_lbl.setFixedWidth(200)
        branch_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        branch_lbl.setStyleSheet("color: #888;")
        self._steam_branch_combo = QComboBox()
        self._steam_branch_combo.addItems(["public", "unstable", "outdatedunstable"])
        self._steam_branch_combo.setFixedWidth(160)
        self._steam_branch_combo.currentTextChanged.connect(self._on_settings_changed)
        branch_hint = QLabel("Which Steam branch your server runs on")
        branch_hint.setStyleSheet("color: #444; font-size: 11px;")
        branch_row.addWidget(branch_lbl)
        branch_row.addWidget(self._steam_branch_combo)
        branch_row.addWidget(branch_hint)
        branch_row.addStretch()
        sched_layout.addLayout(branch_row)

        # Scheduled restart row
        sched_row = QHBoxLayout()
        sched_row.setSpacing(8)
        sched_lbl = QLabel("Scheduled Restart:")
        sched_lbl.setFixedWidth(200)
        sched_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        sched_lbl.setStyleSheet("color: #888;")
        hours = ["Disabled"] + [f"{h:02d}" for h in range(24)]
        minutes = [f"{m:02d}" for m in range(0, 60, 5)]
        self._sched_hour = QComboBox()
        self._sched_hour.addItems(hours)
        self._sched_hour.setFixedWidth(90)
        self._sched_hour.currentTextChanged.connect(self._on_settings_changed)
        colon = QLabel(":")
        colon.setFixedWidth(8)
        colon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sched_minute = QComboBox()
        self._sched_minute.addItems(minutes)
        self._sched_minute.setFixedWidth(72)
        self._sched_minute.currentTextChanged.connect(self._on_settings_changed)
        hint = QLabel("24 h — 'Disabled' to turn off")
        hint.setStyleSheet("color: #444; font-size: 11px;")
        sched_row.addWidget(sched_lbl)
        sched_row.addWidget(self._sched_hour)
        sched_row.addWidget(colon)
        sched_row.addWidget(self._sched_minute)
        sched_row.addWidget(hint)
        sched_row.addStretch()
        sched_layout.addLayout(sched_row)
        form_layout.addWidget(sched_box)
        form_layout.addStretch()

        # Save row
        save_row = QHBoxLayout()
        self._unsaved_label = QLabel("Unsaved changes")
        self._unsaved_label.setStyleSheet("color: #f0a030; font-size: 11px;")
        self._unsaved_label.setVisible(False)
        self._save_btn = QPushButton("Save Configuration")
        self._save_btn.setFixedWidth(160)
        self._save_btn.clicked.connect(self._save_config)
        save_row.addWidget(self._unsaved_label)
        save_row.addStretch()
        save_row.addWidget(self._save_btn)
        outer_layout.addLayout(save_row)

        self.tabs.addTab(outer, "Settings")

    # ------------------------------------------------------------------
    # System tray
    # ------------------------------------------------------------------

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning("System tray not available — close button will exit normally.")
            return

        self._tray = QSystemTrayIcon(_make_icon(), parent=self)
        self._tray.setToolTip("PZ Server Manager")

        tray_menu = QMenu()
        show_action = tray_menu.addAction("Show")
        show_action.triggered.connect(self._show_from_tray)
        quit_action = tray_menu.addAction("Quit")
        quit_action.triggered.connect(self._quit_from_tray)
        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def closeEvent(self, event) -> None:
        if hasattr(self, "_tray") and self._tray.isVisible():
            event.ignore()
            self.hide()
            self._tray.showMessage(
                "PZ Server Manager",
                "Running in background. Right-click the tray icon to quit.",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
        else:
            event.accept()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self) -> None:
        self._stop_log_tail()
        QApplication.instance().quit()

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _load_config_into_ui(self) -> None:
        cfg = self.config
        e = self._settings_entries

        def put(key: str, val) -> None:
            e[key].blockSignals(True)
            e[key].setText(str(val))
            e[key].blockSignals(False)

        put("server_dir",             cfg.server_dir)
        put("zomboid_dir",            cfg.zomboid_dir)
        put("rcon_path",              cfg.rcon_path)
        put("server_name",            cfg.server_name)
        put("server_ip",              cfg.server_ip)
        put("password",               cfg.password)
        put("check_interval",         str(cfg.check_interval))
        put("steamcmd_path",          cfg.steamcmd_path)
        put("server_update_interval", str(cfg.server_update_interval))
        put("steamcmd_timeout",       str(cfg.steamcmd_timeout))

        self._steam_branch_combo.blockSignals(True)
        idx = self._steam_branch_combo.findText(cfg.steam_branch)
        self._steam_branch_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._steam_branch_combo.blockSignals(False)

        if cfg.last_update:
            self._mod_last_label.setText(f"Last checked: {cfg.last_update}")
        if cfg.last_server_update_check:
            self._server_update_last_label.setText(
                f"Last checked: {cfg.last_server_update_check}"
            )

        # Scheduled restart dropdowns
        sched = cfg.scheduled_restart.strip()
        self._sched_hour.blockSignals(True)
        self._sched_minute.blockSignals(True)
        if sched:
            try:
                sh, sm = sched.split(":")
                sm_rounded = f"{(int(sm) // 5) * 5:02d}"
                idx_h = self._sched_hour.findText(sh)
                idx_m = self._sched_minute.findText(sm_rounded)
                if idx_h >= 0:
                    self._sched_hour.setCurrentIndex(idx_h)
                if idx_m >= 0:
                    self._sched_minute.setCurrentIndex(idx_m)
            except ValueError:
                self._sched_hour.setCurrentIndex(0)
        else:
            self._sched_hour.setCurrentIndex(0)
        self._sched_hour.blockSignals(False)
        self._sched_minute.blockSignals(False)

        self._settings_dirty = False
        self._unsaved_label.setVisible(False)
        self._update_status_bar()

    def _on_settings_changed(self) -> None:
        if not self._settings_dirty:
            self._settings_dirty = True
            self._unsaved_label.setVisible(True)

    def _save_config(self) -> None:
        old_zomboid_dir = self.config.zomboid_dir
        e = self._settings_entries

        self.config.server_dir    = e["server_dir"].text()
        self.config.zomboid_dir   = e["zomboid_dir"].text()
        self.config.rcon_path     = e["rcon_path"].text()
        self.config.server_name   = e["server_name"].text()
        self.config.server_ip     = e["server_ip"].text()
        self.config.password      = e["password"].text()
        try:
            self.config.check_interval = max(1, int(e["check_interval"].text()))
        except ValueError:
            self.config.check_interval = 60
        self.config.steamcmd_path = e["steamcmd_path"].text()
        try:
            self.config.server_update_interval = max(1, int(e["server_update_interval"].text()))
        except ValueError:
            self.config.server_update_interval = 60
        try:
            self.config.steamcmd_timeout = max(30, int(e["steamcmd_timeout"].text()))
        except ValueError:
            self.config.steamcmd_timeout = 600

        self.config.steam_branch = self._steam_branch_combo.currentText()

        hour_val = self._sched_hour.currentText()
        self.config.scheduled_restart = (
            "" if hour_val == "Disabled"
            else f"{hour_val}:{self._sched_minute.currentText()}"
        )

        self.config.save()
        self._settings_dirty = False
        self._unsaved_label.setVisible(False)
        self._log("Configuration saved.")
        self._update_status_bar()
        self._update_log_tab_state()

        # If config just became valid for the first time, start the check pipelines.
        if self._config_is_ready():
            if self._auto_check_job is None:
                self._resume_auto_check()
            if self._server_update_check_job is None:
                self._resume_server_update_check()

        if self.config.zomboid_dir != old_zomboid_dir:
            self._restart_log_tail()

    # ------------------------------------------------------------------
    # Server status
    # ------------------------------------------------------------------

    def _check_server_status_threaded(self) -> None:
        if self._status_check_in_flight:
            return
        self._status_check_in_flight = True
        self._status_text.setText("Checking…")
        self._status_dot.setStyleSheet("color: #f0a030; font-size: 18px;")
        threading.Thread(target=self._check_server_status, daemon=True).start()

    def _check_server_status(self) -> bool:
        import subprocess as _sp
        try:
            running = self.server.is_running()
        except _sp.TimeoutExpired:
            self._invoke(lambda: self._set_status_error(
                "PowerShell timed out — WMI may be busy. Retrying in 15 s."
            ))
            self._invoke(lambda: setattr(self, "_status_check_in_flight", False))
            self._invoke(lambda: QTimer.singleShot(15_000, self._check_server_status_threaded))
            return False
        except Exception as exc:
            self._invoke(lambda e=exc: self._set_status_error(f"{e} — Retrying in 15 s."))
            self._invoke(lambda: setattr(self, "_status_check_in_flight", False))
            self._invoke(lambda: QTimer.singleShot(15_000, self._check_server_status_threaded))
            return False
        self._invoke(lambda: setattr(self, "_status_check_in_flight", False))
        self._invoke(lambda r=running: self._set_status(r))
        return running

    def _set_status(self, running: bool) -> None:
        self._server_running = running
        if running:
            self._status_dot.setStyleSheet(
                "color: #3ddc84; font-size: 18px;"
            )
            self._status_text.setText("Running")
        else:
            self._status_dot.setStyleSheet(
                "color: #e05252; font-size: 18px;"
            )
            self._status_text.setText("Stopped")
        self._update_status_bar()

    def _set_status_error(self, msg: str) -> None:
        self._status_dot.setStyleSheet("color: #e05252; font-size: 18px;")
        self._status_text.setText("Error")
        self._log(f"Status check error: {msg}")

    def _update_status_bar(self) -> None:
        status = self._status_text.text()
        color = "#3ddc84" if status == "Running" else "#e05252" if status == "Stopped" else "#666"
        self._sb_server.setText(f"● {status}")
        self._sb_server.setStyleSheet(f"color: {color}; margin-right: 16px;")
        name = self.config.server_name or "–"
        ip   = self.config.server_ip   or "–"
        self._sb_info.setText(f"{name} @ {ip}")

    # ------------------------------------------------------------------
    # Server control buttons (disable during async, re-enable in finally)
    # ------------------------------------------------------------------

    def _set_control_buttons(self, enabled: bool) -> None:
        for btn in (self._start_btn, self._stop_btn, self._restart_btn):
            btn.setEnabled(enabled)

    def _start_server(self) -> None:
        self._log("Checking for existing server process…")
        self._set_control_buttons(False)

        def _do():
            try:
                if self.server.is_running():
                    self._invoke(lambda: self._log("Server is already running."))
                    self._invoke(lambda: self._set_status(True))
                    return
                self.server.start()
                self._invoke(lambda: self._log("Server is launching…"))
                self._invoke(lambda: self._status_text.setText("Starting…"))
                self._invoke(lambda: QTimer.singleShot(5000, self._check_server_status_threaded))
            except Exception as exc:
                self._invoke(lambda e=exc: self._log(f"Start failed: {e}"))
            finally:
                self._invoke(lambda: self._set_control_buttons(True))

        try:
            threading.Thread(target=_do, daemon=True).start()
        except Exception as exc:
            self._log(f"Could not spawn start thread: {exc}")
            self._set_control_buttons(True)

    def _stop_server(self) -> None:
        self._log("Sending stop command…")
        self._set_control_buttons(False)

        def _do():
            try:
                self.server.stop()
                self._invoke(lambda: self._log("Stop command sent."))
                self._invoke(lambda: QTimer.singleShot(6000, self._check_server_status_threaded))
            except Exception as exc:
                self._invoke(lambda e=exc: self._log(f"Stop failed: {e}"))
            finally:
                self._invoke(lambda: self._set_control_buttons(True))

        try:
            threading.Thread(target=_do, daemon=True).start()
        except Exception as exc:
            self._log(f"Could not spawn stop thread: {exc}")
            self._set_control_buttons(True)

    def _restart_server(self) -> None:
        self._log("Restarting server…")
        self._set_control_buttons(False)

        def _do():
            import time as _time
            try:
                self.server.stop()
                _time.sleep(6)
                self.server.start()
                self._invoke(lambda: self._log("Server restarted."))
                self._invoke(lambda: QTimer.singleShot(5000, self._check_server_status_threaded))
            except Exception as exc:
                self._invoke(lambda e=exc: self._log(f"Restart failed: {e}"))
            finally:
                self._invoke(lambda: self._set_control_buttons(True))

        try:
            threading.Thread(target=_do, daemon=True).start()
        except Exception as exc:
            self._log(f"Could not spawn restart thread: {exc}")
            self._set_control_buttons(True)

    # ------------------------------------------------------------------
    # Auto-refresh server status
    # ------------------------------------------------------------------

    def _start_status_poll(self) -> None:
        self._poll_status_job = QTimer(self)
        self._poll_status_job.setInterval(_STATUS_POLL_MS)
        self._poll_status_job.timeout.connect(
            lambda: threading.Thread(
                target=self._check_server_status, daemon=True
            ).start()
        )
        self._poll_status_job.start()

    # ------------------------------------------------------------------
    # Scheduled daily restart
    # ------------------------------------------------------------------

    def _start_sched_poll(self) -> None:
        self._sched_job = QTimer(self)
        self._sched_job.setInterval(_SCHED_POLL_MS)
        self._sched_job.timeout.connect(self._check_scheduled_restart)
        self._sched_job.start()

    def _check_scheduled_restart(self) -> None:
        sched = self.config.scheduled_restart.strip()
        if not sched:
            return
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
                if self._countdown_active:
                    self._log("Scheduled restart deferred — countdown already active.")
                else:
                    self._restart_server()
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # Check cycle helpers
    # ------------------------------------------------------------------

    def _resume_check_cycle(
        self, last_ts: str, interval_min: int, check_fn, label: str
    ) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(check_fn)

        if not last_ts:
            self._log(f"No previous {label} check — first check in 10 s.")
            timer.start(10_000)
            return timer

        try:
            last = datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S")
            elapsed = (datetime.now() - last).total_seconds()
            remaining = interval_min * 60 - elapsed
            if remaining <= 30:
                self._log(f"{label} check overdue — checking in 10 s.")
                timer.start(10_000)
            else:
                mins, secs = divmod(int(remaining), 60)
                self._log(f"Resuming {label} schedule — next check in {mins}m {secs}s.")
                timer.start(int(remaining * 1000))
        except ValueError:
            timer.start(10_000)

        return timer

    # ------------------------------------------------------------------
    # Config validity guard
    # ------------------------------------------------------------------

    def _config_is_ready(self) -> bool:
        """Return True only when the minimum required fields are filled in."""
        c = self.config
        return bool(c.server_dir and c.rcon_path and c.server_ip and c.password)

    # ------------------------------------------------------------------
    # Mod update pipeline
    # ------------------------------------------------------------------

    def _resume_auto_check(self) -> None:
        self._auto_check_job = self._resume_check_cycle(
            self.config.last_update,
            self.config.check_interval,
            self._check_mods_threaded,
            "mod",
        )

    def _check_mods_threaded(self) -> None:
        cfg = self.config
        if not (cfg.rcon_path and cfg.server_ip and cfg.password):
            missing = [
                name for name, val in [
                    ("RCON Executable", cfg.rcon_path),
                    ("Server IP",       cfg.server_ip),
                    ("Password",        cfg.password),
                ] if not val
            ]
            self._log(
                f"RCON not fully configured — missing: {', '.join(missing)}. "
                "Fill in the Connection group in Settings and click Save."
            )
            self._schedule_next_check()
            return
        if self._server_running is None:
            self._log("Server status not yet known — retrying mod check in 30 s.")
            QTimer.singleShot(30_000, self._check_mods_threaded)
            return
        if not self._server_running:
            self._log("Server is not running — skipping mod check.")
            self._schedule_next_check()
            return
        if self._auto_check_job:
            self._auto_check_job.stop()
        self._mod_status_label.setText("Sending RCON command…")
        self._mod_status_label.setStyleSheet("color: #f0a030;")
        self._log("Checking for mod updates…")
        threading.Thread(target=self._check_mods_worker, daemon=True).start()

    def _check_mods_worker(self) -> None:
        try:
            self.server.check_mods_need_update()
        except Exception as exc:
            self._invoke(lambda e=exc: self._log(f"RCON error: {e}"))
            self._invoke(lambda e=exc: self._set_mod_status(f"RCON error: {e}", "error"))
            return

        zomboid_dir = self.config.zomboid_dir
        if not zomboid_dir:
            self._invoke(lambda: self._set_mod_status("Zomboid log folder not set.", "error"))
            return

        parser = LogParser(zomboid_dir)
        log_file = parser.find_latest_log()
        if not log_file:
            self._invoke(lambda: self._set_mod_status("No log file found.", "error"))
            return

        self._invoke(lambda: self._log(f"Monitoring: {os.path.basename(log_file)}"))
        self._invoke(lambda: self._set_mod_status("Waiting for log response…", "warn"))

        status, mod_names = parser.monitor_for_mod_status(log_file)

        if status == "needs_update" and mod_names:
            mod_map = parser.load_server_mod_map(
                self.config.server_dir, self.config.server_name
            )
            if mod_map:
                mod_names = [
                    (
                        f"{mod_map[e.replace('Workshop ID: ', '')]}  "
                        f"(Workshop: {e.replace('Workshop ID: ', '')})"
                        if e.replace("Workshop ID: ", "") in mod_map
                        else e
                    )
                    for e in mod_names
                ]

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.config.last_update = now
        self.config.save()

        if status == "needs_update":
            self._invoke(lambda: self._on_mods_need_update(mod_names, now))
        elif status == "up_to_date":
            self._invoke(lambda: self._on_mods_up_to_date(now))
        else:
            self._invoke(lambda: self._log("Timed out waiting for mod status (300 s)."))
            self._invoke(lambda: self._set_mod_status("Timed out — no response.", "error"))

    def _set_mod_status(self, text: str, level: str = "default") -> None:
        colors = {"error": "#e05252", "warn": "#f0a030", "success": "#3ddc84", "default": "#888"}
        self._mod_status_label.setText(text)
        self._mod_status_label.setStyleSheet(f"color: {colors.get(level, '#888')};")

    def _on_mods_need_update(self, mod_names: list[str], timestamp: str) -> None:
        self._set_mod_status("Updates available!", "warn")
        self._mod_last_label.setText(f"Last checked: {timestamp}")
        if mod_names:
            self._mod_list_widget.setText("\n".join(f"• {n}" for n in mod_names))
            self._mod_list_widget.setVisible(True)
            for name in mod_names:
                self._log(f"  Mod: {name}")
        else:
            self._mod_list_widget.setVisible(False)
        self._log("Mods need updating — starting 5-minute restart countdown.")
        self._start_restart_countdown()

    def _on_mods_up_to_date(self, timestamp: str) -> None:
        self._set_mod_status("All mods up to date  ✓", "success")
        self._mod_last_label.setText(f"Last checked: {timestamp}")
        self._mod_list_widget.setVisible(False)
        self._log("All mods are up to date.")
        self._schedule_next_check()

    # ------------------------------------------------------------------
    # Restart countdown
    # ------------------------------------------------------------------

    def _start_restart_countdown(
        self,
        seconds: int = 300,
        broadcast_messages: Optional[dict] = None,
        action=None,
        post_fn=None,
    ) -> None:
        if self._countdown_active:
            self._log("Restart countdown already running — skipping duplicate start.")
            return
        self._countdown_remaining = seconds
        self._countdown_active = True
        self._countdown_broadcast_messages = broadcast_messages or _BROADCAST_AT
        self._countdown_action = action or self._restart_server
        self._countdown_post_fn = post_fn or self._schedule_next_check

        # Show banner, hide spacer
        self._countdown_spacer.setVisible(False)
        self._countdown_frame.setVisible(True)

        self._tick_countdown()

    def _tick_countdown(self) -> None:
        if not self._countdown_active:
            return

        remaining = self._countdown_remaining
        if remaining <= 0:
            self._countdown_active = False
            self._countdown_frame.setVisible(False)
            self._countdown_spacer.setVisible(True)
            self._log("Restart countdown finished.")
            action  = self._countdown_action or self._restart_server
            post_fn = self._countdown_post_fn or self._schedule_next_check
            action()
            post_fn()
            return

        mins, secs = divmod(remaining, 60)
        self._cd_timer_label.setText(f"{mins:02d}:{secs:02d}")

        msgs = self._countdown_broadcast_messages or _BROADCAST_AT
        if remaining in msgs:
            msg = msgs[remaining]
            self._log(f"Broadcasting to players: {msg}")
            threading.Thread(
                target=lambda m=msg: self._broadcast_safe(m), daemon=True
            ).start()

        if remaining % 60 == 0:
            threading.Thread(target=self._maybe_early_restart, daemon=True).start()

        self._countdown_remaining -= 1
        QTimer.singleShot(1000, self._tick_countdown)

    def _broadcast_safe(self, message: str) -> None:
        try:
            self.server.broadcast(message)
        except Exception as exc:
            self._invoke(lambda e=exc: self._log(f"Broadcast failed: {e}"))

    def _maybe_early_restart(self) -> None:
        if not self._countdown_active:
            return
        try:
            count = self.server.get_player_count()
            self._invoke(lambda: self._log(f"Players currently online: {count}"))
            self._invoke(lambda: self._cd_players_count.setText(str(count)))
            if count == 0:
                self._invoke(lambda: self._log("No players online — triggering early restart."))
                self._invoke(lambda: setattr(self, "_countdown_remaining", 0))
        except Exception as exc:
            self._invoke(lambda e=exc: self._log(f"Player count check failed: {e}"))

    def _cancel_countdown(self) -> None:
        """Cancel the active restart countdown without restarting the server."""
        self._countdown_active = False
        self._countdown_frame.setVisible(False)
        self._countdown_spacer.setVisible(True)
        self._log("Restart countdown cancelled by admin.")
        threading.Thread(
            target=lambda: self._broadcast_safe("Server restart has been cancelled."),
            daemon=True,
        ).start()
        post_fn = self._countdown_post_fn or self._schedule_next_check
        post_fn()

    def _schedule_next_check(self) -> None:
        if self._auto_check_job:
            self._auto_check_job.stop()
        interval_ms = self.config.check_interval * 60 * 1000
        self._auto_check_job = QTimer(self)
        self._auto_check_job.setSingleShot(True)
        self._auto_check_job.timeout.connect(self._check_mods_threaded)
        self._auto_check_job.start(interval_ms)
        self._log(f"Next mod check in {self.config.check_interval} minute(s).")

    # ------------------------------------------------------------------
    # Server update pipeline
    # ------------------------------------------------------------------

    def _resume_server_update_check(self) -> None:
        self._server_update_check_job = self._resume_check_cycle(
            self.config.last_server_update_check,
            self.config.server_update_interval,
            self._check_server_update_threaded,
            "server update",
        )

    def _check_server_update_threaded(self) -> None:
        if not self.config.server_dir:
            self._log("Server folder not configured — open Settings to get started.")
            return
        if not self.config.steamcmd_path or not os.path.isfile(self.config.steamcmd_path):
            self._log("SteamCMD path not configured or not found — set it in Settings.")
            return
        if self._countdown_active:
            self._log("Skipping server update check — restart already in progress.")
            self._schedule_next_server_update_check()
            return
        if self._server_update_check_job:
            self._server_update_check_job.stop()
        self._server_update_label.setText("Checking…")
        self._server_update_label.setStyleSheet("color: #f0a030;")
        self._log("Checking for server updates…")
        threading.Thread(target=self._check_server_update_worker, daemon=True).start()

    def _check_server_update_worker(self) -> None:
        result = self._server_update_checker.update_needed()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.config.last_server_update_check = now
        self.config.save()

        if result is True:
            self._invoke(lambda: self._on_server_update_needed(now))
        elif result is False:
            self._invoke(lambda: self._on_server_up_to_date(now))
        else:
            self._invoke(lambda: self._server_update_label.setText("Check failed — see log"))
            self._invoke(lambda: self._server_update_label.setStyleSheet("color: #e05252;"))
            self._invoke(lambda: self._log("Server update check failed."))
            self._invoke(self._schedule_next_server_update_check)

    def _on_server_update_needed(self, timestamp: str) -> None:
        self._server_update_label.setText("Update available!")
        self._server_update_label.setStyleSheet("color: #f0a030;")
        self._server_update_last_label.setText(f"Last checked: {timestamp}")
        self._log("Server update available — starting 5-minute restart countdown.")
        self._start_restart_countdown(
            broadcast_messages=_SERVER_UPDATE_BROADCAST_AT,
            action=self._run_server_update_then_restart,
            post_fn=self._schedule_next_server_update_check,
        )

    def _on_server_up_to_date(self, timestamp: str) -> None:
        self._server_update_label.setText("Server up to date  ✓")
        self._server_update_label.setStyleSheet("color: #3ddc84;")
        self._server_update_last_label.setText(f"Last checked: {timestamp}")
        self._log("Server is up to date.")
        self._schedule_next_server_update_check()

    def _schedule_next_server_update_check(self) -> None:
        if self._server_update_check_job:
            self._server_update_check_job.stop()
        interval_ms = self.config.server_update_interval * 60 * 1000
        self._server_update_check_job = QTimer(self)
        self._server_update_check_job.setSingleShot(True)
        self._server_update_check_job.timeout.connect(self._check_server_update_threaded)
        self._server_update_check_job.start(interval_ms)
        self._log(f"Next server update check in {self.config.server_update_interval} minute(s).")

    def _run_server_update_then_restart(self) -> None:
        self._log("Stopping server for SteamCMD update…")

        def _do():
            import time as _time
            try:
                self.server.stop()
                self._invoke(lambda: self._log("Stop command sent — waiting for server to exit…"))
            except Exception as exc:
                self._invoke(lambda e=exc: self._log(f"Stop failed: {e}"))
                return

            stopped = False
            for _ in range(15):
                _time.sleep(2)
                try:
                    if not self.server.is_running():
                        stopped = True
                        break
                except Exception:
                    pass

            if not stopped:
                self._invoke(lambda: self._log(
                    "Server did not stop within 30 s — aborting SteamCMD update."
                ))
                self._invoke(self._schedule_next_server_update_check)
                return

            self._invoke(lambda: self._log("Server stopped. Running SteamCMD update…"))
            self._invoke(lambda: self._server_update_label.setText("Updating…"))
            self._invoke(lambda: self._server_update_label.setStyleSheet("color: #f0a030;"))

            success = self._server_update_checker.run_update()

            if success:
                self._invoke(lambda: self._log("SteamCMD update completed. Starting server…"))
                self._invoke(lambda: self._server_update_label.setText("Updated  ✓"))
                self._invoke(lambda: self._server_update_label.setStyleSheet("color: #3ddc84;"))
            else:
                self._invoke(lambda: self._log(
                    "Server update failed — restarting on current version."
                ))
                self._invoke(lambda: self._server_update_label.setText("Update failed — restarted"))
                self._invoke(lambda: self._server_update_label.setStyleSheet("color: #e05252;"))

            try:
                self.server.start()
                self._invoke(lambda: self._log("Server start command sent."))
                self._invoke(lambda: QTimer.singleShot(5000, self._check_server_status_threaded))
            except Exception as exc:
                self._invoke(lambda e=exc: self._log(f"Server start failed: {e}"))

        threading.Thread(target=_do, daemon=True).start()

    # ------------------------------------------------------------------
    # RCON Console
    # ------------------------------------------------------------------

    def _send_rcon_command(self) -> None:
        command = self.rcon_input.text().strip()
        if not command:
            return
        self.rcon_input.clear()
        self.rcon_input.push_history(command)
        self.rcon_output.append_line(f"> {command}", "info")

        def _do():
            try:
                response = self.server.send_command(command)
                self._invoke(lambda r=response: self.rcon_output.append_line(r))
            except Exception as exc:
                self._invoke(lambda e=exc: self.rcon_output.append_line(f"Error: {e}", "error"))

        threading.Thread(target=_do, daemon=True).start()

    # ------------------------------------------------------------------
    # Live server log viewer
    # ------------------------------------------------------------------

    def _start_log_tail(self) -> None:
        if not self.config.zomboid_dir:
            self._update_log_tab_state()
            return
        self._stop_log_tail()
        self._log_tailer = LogTailer(self.config.zomboid_dir, self._on_log_line_from_thread)
        self._log_tailer.start()

    def _stop_log_tail(self) -> None:
        if self._log_tailer:
            self._log_tailer.stop()
            self._log_tailer = None

    def _restart_log_tail(self) -> None:
        self._stop_log_tail()
        self._start_log_tail()

    def _on_log_line_from_thread(self, line: str) -> None:
        """Called from LogTailer's background thread — routes via LogBridge signal."""
        self._log_bridge.line_received.emit(line)

    def _on_server_log_line(self, line: str) -> None:
        """Received on the main thread via Qt queued connection."""
        stripped = line.rstrip("\n")
        if not stripped:
            return
        level = _classify_line(stripped)
        self.server_log.append_line(stripped, level)

        if self._log_tailer and self._log_tailer.current_file:
            fn = os.path.basename(self._log_tailer.current_file)
            self._log_file_label.setText(f"Tailing: {fn}")
            self._log_file_label.setStyleSheet("color: #3ddc84; font-size: 11px;")

    # ------------------------------------------------------------------
    # Activity log helper
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        level = _classify_line(message)
        self.activity_log.append_line(message, level)
        logger.info(message)

    # ------------------------------------------------------------------
    # File pickers
    # ------------------------------------------------------------------

    def _pick_dir(self, entry: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Folder")
        if path:
            entry.setText(path)

    def _pick_file(self, entry: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select File", "", "Executable (*.exe);;All files (*)"
        )
        if path:
            entry.setText(path)

    # ------------------------------------------------------------------
    # Thread-safe UI dispatch
    # ------------------------------------------------------------------

    def _invoke(self, fn) -> None:
        """
        Schedule fn() to run on the main thread via a zero-delay QTimer.
        Safe to call from any thread.
        """
        QTimer.singleShot(0, fn)


# ---------------------------------------------------------------------------
# Programmatic tray/window icon — no external file needed
# ---------------------------------------------------------------------------

def _make_icon() -> QIcon:
    """
    Draw a minimal server-manager icon: dark background with a green square
    (representing a running process / terminal block).
    """
    px = QPixmap(QSize(32, 32))
    px.fill(QColor("#1a1a2e"))
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    # Outer ring
    p.setBrush(QColor("#4d9de0"))
    p.drawRoundedRect(2, 2, 28, 28, 4, 4)
    # Inner square (darker)
    p.setBrush(QColor("#1a1a2e"))
    p.drawRoundedRect(6, 6, 20, 20, 3, 3)
    # Green dot
    p.setBrush(QColor("#3ddc84"))
    p.drawEllipse(11, 11, 10, 10)
    p.end()
    return QIcon(px)
