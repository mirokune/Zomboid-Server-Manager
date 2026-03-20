"""
main.py — Project Zomboid Server Manager
Entry point. Prevents duplicate instances via a local socket lock,
then launches the CustomTkinter UI.
"""

import socket
import sys
import logging

# Configure logging before any other imports
logging.basicConfig(
    filename="pz_manager.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

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
    lock = _acquire_lock()
    if lock is None:
        # Another instance is already running — alert and exit.
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

    # Import here so we only pay the cost if we're the first instance
    from gui import App

    app = App()
    try:
        app.mainloop()
    finally:
        lock.close()


if __name__ == "__main__":
    main()
