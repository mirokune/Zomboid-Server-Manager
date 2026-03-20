"""
backend.py — Project Zomboid Server Manager
Business logic: configuration, server control, log parsing, and log tailing.
"""

import os
import re
import subprocess
import threading
import time
import logging
import configparser
from datetime import datetime
from glob import glob
from typing import Callable, Optional

logger = logging.getLogger(__name__)

CONFIG_FILE = "pz_server_config.ini"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class AppConfig:
    def __init__(self):
        self.server_dir: str = ""
        self.zomboid_dir: str = ""
        self.rcon_path: str = ""
        self.server_ip: str = ""
        self.server_name: str = ""
        self.password: str = ""
        self.check_interval: int = 60
        self.last_update: str = ""
        self.scheduled_restart: str = ""  # "HH:MM" or "" to disable

    def load(self):
        config = configparser.ConfigParser()
        if not os.path.exists(CONFIG_FILE):
            logger.info("No config file found — using defaults.")
            return
        config.read(CONFIG_FILE)
        s = config["SETTINGS"] if "SETTINGS" in config else {}
        self.server_dir = s.get("server_dir", "")
        self.zomboid_dir = s.get("zomboid_dir", "")
        self.rcon_path = s.get("rcon_path", "")
        self.server_ip = s.get("server_ip", "")
        self.server_name = s.get("server_name", "")
        self.password = s.get("password", "")
        try:
            self.check_interval = int(s.get("check_interval", "60"))
        except ValueError:
            self.check_interval = 60
        self.last_update = s.get("last_update", "")
        self.scheduled_restart = s.get("scheduled_restart", "")
        logger.info("Configuration loaded.")

    def save(self):
        config = configparser.ConfigParser()
        config["SETTINGS"] = {
            "server_dir": self.server_dir,
            "zomboid_dir": self.zomboid_dir,
            "rcon_path": self.rcon_path,
            "server_ip": self.server_ip,
            "server_name": self.server_name,
            "password": self.password,
            "check_interval": str(self.check_interval),
            "last_update": self.last_update,
            "scheduled_restart": self.scheduled_restart,
        }
        with open(CONFIG_FILE, "w") as f:
            config.write(f)
        logger.info("Configuration saved.")


# ---------------------------------------------------------------------------
# Server management
# ---------------------------------------------------------------------------

class ServerManager:
    def __init__(self, config: AppConfig):
        self.config = config

    def is_running(self) -> bool:
        """Return True if a matching java.exe server process is running."""
        name = self.config.server_name
        if name:
            filter_clause = (
                f"$_.CommandLine -like '*-servername {name}*' -and "
                f"$_.CommandLine -like '*-Djava.awt.headless=true*'"
            )
        else:
            filter_clause = "$_.CommandLine -like '*-Djava.awt.headless=true*'"

        cmd = (
            f"powershell -Command \""
            f"Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.Name -eq 'java.exe' -and {filter_clause} }} | "
            f"Select-Object -First 1\""
        )
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return bool(result.stdout.strip())

    def start(self):
        """Launch the server via StartServer64.bat in a new console window."""
        if not self.config.server_dir:
            raise ValueError("Server directory not configured.")
        batch = os.path.join(self.config.server_dir, "StartServer64.bat")
        if not os.path.exists(batch):
            raise FileNotFoundError(
                f"StartServer64.bat not found in: {self.config.server_dir}"
            )
        subprocess.Popen(["cmd.exe", "/c", "start", batch], shell=True)
        logger.info("Server start command sent.")

    def stop(self):
        """Send the RCON quit command."""
        self._rcon("quit")
        logger.info("Server stop command sent.")

    def broadcast(self, message: str):
        """Send an in-game server-wide message via RCON servermsg."""
        self._rcon(f'servermsg "{message}"')
        logger.info("Broadcast sent: %s", message)

    def check_mods_need_update(self):
        """Send the checkModsNeedUpdate RCON command."""
        self._rcon("checkModsNeedUpdate")
        logger.info("checkModsNeedUpdate sent.")

    def get_player_count(self) -> int:
        """Return the number of connected players via RCON, or 0 on failure."""
        output = self._rcon("players", capture=True)
        if not output:
            return 0
        for line in output.splitlines():
            if "Players connected" in line:
                try:
                    return int(line.split("(")[1].split(")")[0])
                except (IndexError, ValueError):
                    pass
        return 0

    def send_command(self, command: str) -> str:
        """Send an arbitrary RCON command and return its trimmed output."""
        output = self._rcon(command, capture=True)
        return output.strip() if output else "(no output)"

    def _rcon(self, command: str, capture: bool = False) -> Optional[str]:
        cfg = self.config
        if not cfg.rcon_path:
            raise ValueError("RCON executable path not configured.")
        if not cfg.server_ip:
            raise ValueError("Server IP not configured.")
        if not cfg.password:
            raise ValueError("RCON password not configured.")

        args = [cfg.rcon_path, "-a", cfg.server_ip, "-p", cfg.password, command]
        try:
            result = subprocess.run(
                args, shell=True, capture_output=True, text=True, timeout=15
            )
            return result.stdout if capture else None
        except subprocess.TimeoutExpired:
            logger.warning("RCON command '%s' timed out.", command)
            return None


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

class LogParser:
    def __init__(self, log_directory: str):
        self.log_directory = log_directory

    def find_latest_log(self) -> Optional[str]:
        """Find the most recently modified DebugLog-server* file."""
        for pattern in ("DebugLog-server*.txt", "DebugLog-server*"):
            files = glob(os.path.join(self.log_directory, pattern))
            if files:
                return max(files, key=os.path.getmtime)
        return None

    def monitor_for_mod_status(
        self,
        log_file: str,
        timeout: int = 300,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> tuple[Optional[str], list[str]]:
        """
        Tail the log file from its current end, waiting for a mod update result.

        Returns
        -------
        (status, mod_names)
            status    : 'needs_update' | 'up_to_date' | None (timed out)
            mod_names : list of mod identifiers that need updating
        """
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                f.seek(0, os.SEEK_END)
                collected: list[str] = []
                start = time.monotonic()

                while time.monotonic() - start < timeout:
                    new_lines = f.readlines()
                    if not new_lines:
                        time.sleep(2)
                        continue

                    collected.extend(new_lines)

                    for i, line in enumerate(collected):
                        if "Mods need update" in line:
                            mod_names = self._extract_mod_names(collected, i)
                            return "needs_update", mod_names
                        if "Mods updated" in line or "No mods need updating" in line:
                            return "up_to_date", []

                    time.sleep(2)

        except FileNotFoundError:
            logger.error("Log file not found: %s", log_file)

        return None, []

    def _extract_mod_names(self, lines: list[str], found_idx: int) -> list[str]:
        """
        Extract mod IDs / workshop IDs from the lines at and after the
        'Mods need update' match, handling multiple PZ log formats.
        """
        seen: set[str] = set()
        results: list[str] = []

        def add(name: str):
            name = name.strip().rstrip(",;")
            if name and name not in seen:
                seen.add(name)
                results.append(name)

        search_lines = lines[found_idx: found_idx + 60]
        trigger = search_lines[0] if search_lines else ""

        # Inline format: "Mods need update: workshopID=111,workshopID=222"
        for wid in re.findall(r"workshopID[=:](\d+)", trigger, re.IGNORECASE):
            add(f"Workshop ID: {wid}")

        for line in search_lines[1:]:
            # Stop at the next distinct log event
            if re.search(r"Mods updated|checkMods|No mods need", line, re.IGNORECASE):
                break

            # Format: "ModID: SomeMod"  (may follow "WorkshopID: 12345")
            mod_match = re.search(r"ModID[=:]\s*(\S+)", line, re.IGNORECASE)
            if mod_match:
                add(mod_match.group(1))
                continue

            # Standalone workshop IDs on their own line
            for wid in re.findall(r"workshopID[=:](\d+)", line, re.IGNORECASE):
                add(f"Workshop ID: {wid}")

        return results

    def load_server_mod_map(
        self, server_dir: str, server_name: str
    ) -> dict[str, str]:
        """
        Build a {workshop_id: mod_id} mapping from the PZ server .ini so
        workshop IDs in the log can be displayed with friendly names.

        PZ stores server configs at:
          %USERPROFILE%\\Zomboid\\Server\\<servername>.ini
        or inside the server directory itself.
        """
        if not server_name:
            return {}

        user_profile = os.environ.get("USERPROFILE", "")
        candidates = [
            os.path.join(server_dir, f"{server_name}.ini"),
            os.path.join(user_profile, "Zomboid", "Server", f"{server_name}.ini"),
        ]

        for path in candidates:
            if not os.path.exists(path):
                continue
            try:
                content = open(path, "r", encoding="utf-8", errors="replace").read()
                mods_m = re.search(r"^Mods=(.+)$", content, re.MULTILINE)
                wids_m = re.search(r"^WorkshopItems=(.+)$", content, re.MULTILINE)
                if mods_m and wids_m:
                    mods = [m.strip() for m in mods_m.group(1).split(";")]
                    wids = [w.strip() for w in wids_m.group(1).split(";")]
                    logger.info("Loaded mod map from %s (%d entries).", path, len(mods))
                    return dict(zip(wids, mods))
            except OSError as exc:
                logger.warning("Could not read server config %s: %s", path, exc)

        return {}


# ---------------------------------------------------------------------------
# Live log tailing
# ---------------------------------------------------------------------------

class LogTailer:
    """
    Tails the latest PZ DebugLog-server* file in a background thread,
    calling `line_callback` for each new line appended to the file.
    Automatically switches to a newer log file when one appears
    (e.g. after a server restart creates a new log).
    """

    def __init__(self, log_directory: str, line_callback: Callable[[str], None]):
        self.log_directory = log_directory
        self.current_file: Optional[str] = None
        self._callback = line_callback
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        parser = LogParser(self.log_directory)
        fh = None
        try:
            while not self._stop.is_set():
                latest = parser.find_latest_log()

                if latest != self.current_file:
                    if fh:
                        fh.close()
                        fh = None
                    self.current_file = latest
                    if latest:
                        try:
                            fh = open(latest, "r", encoding="utf-8", errors="replace")
                            fh.seek(0, os.SEEK_END)
                            self._callback(
                                f"\n─── Now tailing: {os.path.basename(latest)} ───\n"
                            )
                        except OSError as exc:
                            logger.warning(
                                "LogTailer: cannot open %s: %s", latest, exc
                            )
                            fh = None

                if fh:
                    try:
                        lines = fh.readlines()
                        for line in lines:
                            if self._stop.is_set():
                                return
                            self._callback(line)
                    except OSError:
                        fh = None

                self._stop.wait(timeout=0.5)
        finally:
            if fh:
                try:
                    fh.close()
                except OSError:
                    pass
