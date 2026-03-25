"""
main.py — Project Zomboid Server Manager
Entry point. Prevents duplicate instances via a local socket lock,
then launches the PyQt6 UI.
"""

import os
import socket
import sys
import logging
from pathlib import Path


def _log_path() -> str:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    d = Path(appdata) / "PZServerManager"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / "pz_manager.log")


def configure_logging(debug: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # let handlers decide what they emit

    # Prevent duplicate handlers if configure_logging gets called twice
    if root.handlers:
        root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    )

    # File handler: keep everything
    file_handler = logging.FileHandler(_log_path(), encoding="utf-8")
    file_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    file_handler.setFormatter(fmt)

    # Console handler: quieter by default
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if debug else logging.WARNING)
    console_handler.setFormatter(fmt)

    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Optional: quiet very noisy third-party modules
    logging.getLogger("keyring").setLevel(logging.WARNING)

# Arbitrary port used as a single-instance mutex.
# Only one process can bind to this address at a time.
_LOCK_PORT = 47832


def _acquire_lock() -> socket.socket | None:
    """
    Try to bind a socket on localhost to act as a process-level mutex.
    Returns the bound socket on success, or None if another instance is running.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        sock.bind(("127.0.0.1", _LOCK_PORT))
        sock.listen(1)
        return sock
    except OSError:
        sock.close()
        return None


def main():
    debug_mode = "--debug" in sys.argv
    configure_logging(debug=debug_mode)

    logger = logging.getLogger(__name__)
    logger.info("Starting PZ Server Manager")
    lock = _acquire_lock()
    if lock is None:
        # Another instance is already running — alert and exit.
        # Try Qt message box first (QApplication may not exist yet, so fall back).
        try:
            from PyQt6.QtWidgets import QApplication, QMessageBox
            _qa = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.information(
                None,
                "Already Running",
                "PZ Server Manager is already running.\n\nCheck your system tray.",
            )
        except Exception:
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0,
                    "PZ Server Manager is already running.\n\n"
                    "Check your system tray.",
                    "Already Running",
                    0x40 | 0x1000,  # MB_ICONINFORMATION | MB_SETFOREGROUND
                )
            except Exception:
                print("PZ Server Manager is already running.")
        sys.exit(0)

    # DPI scaling — must be set before QApplication is created.
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import Qt

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    # Import here so we only pay the cost if we're the first instance.
    # QApplication must be created before App (which builds the window).
    qa = QApplication(sys.argv)

    from gui import App

    app = App()
    app.show()
    try:
        exit_code = qa.exec()
    finally:
        lock.close()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
