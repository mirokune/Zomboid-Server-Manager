"""
backend.py — Project Zomboid Server Manager
Business logic: configuration, server control, log parsing, and log tailing.
"""

import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import logging
import configparser
from datetime import datetime
from glob import glob

# On Windows, suppress the console window when spawning child processes.
_SUBPROCESS_FLAGS: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if sys.platform == "win32"
    else {}
)
from pathlib import Path
from typing import Callable, Optional

import keyring

logger = logging.getLogger(__name__)

_KEYRING_SERVICE = "PZServerManager"
_KEYRING_USERNAME = "rcon_password"

def _config_dir() -> Path:
    """Return %APPDATA%\\PZServerManager\\ — always writable, no admin needed."""
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    d = Path(appdata) / "PZServerManager"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _config_path() -> Path:
    return _config_dir() / "pz_server_config.ini"

def _legacy_config_path() -> Path:
    """Path to the old config next to the EXE (pre-v0.2 location)."""
    return Path("pz_server_config.ini")


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
        self.password: str = ""        # in-memory only; persisted to Credential Manager
        self.check_interval: int = 60
        self.last_update: str = ""
        self.scheduled_restart: str = ""  # "HH:MM" or "" to disable
        self.steamcmd_path: str = ""
        self.server_update_interval: int = 60
        self.steamcmd_timeout: int = 600
        self.last_server_update_check: str = ""
        self._save_lock = threading.Lock()

    def load(self):
        # Migrate legacy config (next to EXE) to %APPDATA% on first run.
        legacy = _legacy_config_path()
        dest = _config_path()
        if legacy.exists() and not dest.exists():
            try:
                legacy.rename(dest)
                logger.info("Migrated config from %s to %s", legacy, dest)
            except OSError as exc:
                logger.warning("Config migration failed: %s — reading legacy path.", exc)
                dest = legacy

        config = configparser.ConfigParser()
        if not dest.exists():
            logger.info("No config file found — using defaults.")
            # Still try to load the password from Credential Manager in case
            # the INI was deleted but the credential was not.
            self.password = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME) or ""
            return

        config.read(dest)
        s = config["SETTINGS"] if "SETTINGS" in config else {}
        self.server_dir = s.get("server_dir", "")
        self.zomboid_dir = s.get("zomboid_dir", "")
        self.rcon_path = s.get("rcon_path", "")
        self.server_ip = s.get("server_ip", "")
        self.server_name = s.get("server_name", "")
        try:
            self.check_interval = int(s.get("check_interval", "60"))
        except ValueError:
            self.check_interval = 60
        self.last_update = s.get("last_update", "")
        self.scheduled_restart = s.get("scheduled_restart", "")
        self.steamcmd_path = s.get("steamcmd_path", "")
        try:
            self.server_update_interval = int(s.get("server_update_interval", "60"))
        except ValueError:
            self.server_update_interval = 60
        try:
            self.steamcmd_timeout = int(s.get("steamcmd_timeout", "600"))
        except ValueError:
            self.steamcmd_timeout = 600
        self.last_server_update_check = s.get("last_server_update_check", "")

        # If old config had a plaintext password, migrate it to Credential Manager
        # and remove it from the INI.
        legacy_pw = s.get("password", "")
        if legacy_pw:
            try:
                keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, legacy_pw)
                logger.info("Migrated plaintext password to Windows Credential Manager.")
            except Exception as exc:
                logger.warning("Could not migrate password to Credential Manager: %s", exc)
            # Set password before save() so it doesn't call delete_password() on the
            # credential we just migrated.
            self.password = legacy_pw
            # Rewrite the INI without the password field.
            self.save()

        # Load the password from Credential Manager (never from the INI file).
        try:
            self.password = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME) or ""
        except Exception as exc:
            logger.warning("Could not read password from Credential Manager: %s", exc)
            self.password = ""

        logger.info("Configuration loaded from %s", dest)

    def save(self):
        with self._save_lock:
            self._save_locked()

    def _save_locked(self):
        # Persist the password to Windows Credential Manager first.
        try:
            if self.password:
                keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, self.password)
            else:
                # Delete the stored credential if the password was cleared.
                try:
                    keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
                except keyring.errors.PasswordDeleteError:
                    pass  # Credential didn't exist — nothing to delete.
        except Exception as exc:
            logger.warning("Could not save password to Credential Manager: %s", exc)

        config = configparser.ConfigParser()
        config["SETTINGS"] = {
            "server_dir": self.server_dir,
            "zomboid_dir": self.zomboid_dir,
            "rcon_path": self.rcon_path,
            "server_ip": self.server_ip,
            "server_name": self.server_name,
            # password intentionally omitted — stored in Windows Credential Manager
            "check_interval": str(self.check_interval),
            "last_update": self.last_update,
            "scheduled_restart": self.scheduled_restart,
            "steamcmd_path": self.steamcmd_path,
            "server_update_interval": str(self.server_update_interval),
            "steamcmd_timeout": str(self.steamcmd_timeout),
            "last_server_update_check": self.last_server_update_check,
        }
        dest = _config_path()
        dest_dir = os.path.dirname(dest)
        with tempfile.NamedTemporaryFile("w", dir=dest_dir, delete=False, suffix=".tmp") as f:
            tmp_path = f.name
            config.write(f)
        os.replace(tmp_path, dest)
        logger.info("Configuration saved to %s", dest)


# ---------------------------------------------------------------------------
# Server management
# ---------------------------------------------------------------------------

class ServerManager:
    def __init__(self, config: AppConfig):
        self.config = config

    def is_running(self) -> bool:
        """Return True if a matching java.exe server process is running.

        Raises subprocess.TimeoutExpired if PowerShell does not respond within
        10 seconds, so callers are never blocked indefinitely.
        """
        name = self.config.server_name
        if name:
            # Escape single-quotes for PowerShell -like patterns ('' = literal ')
            safe_name = name.replace("'", "''")
            filter_clause = (
                f"$_.CommandLine -like '*-servername {safe_name}*' -and "
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
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=10
        )
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
        safe = message.replace('"', '\\"')
        self._rcon(f'servermsg "{safe}"')
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

        # Write password to a temp YAML config so it never appears in the process list.
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False
            ) as f:
                tmp = f.name
                f.write(f"address: {cfg.server_ip}\npassword: {cfg.password}\n")
            args = [cfg.rcon_path, "--config", tmp, command]
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=15, **_SUBPROCESS_FLAGS
            )
            return result.stdout if capture else None
        except subprocess.TimeoutExpired:
            logger.warning("RCON command '%s' timed out.", command)
            return None
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

class LogParser:
    def __init__(self, log_directory: str):
        self.log_directory = log_directory

    def find_latest_log(self) -> Optional[str]:
        """Find the most recently modified DebugLog-server* file.

        Searches subdirectories too (PZ creates dated subfolders like
        Logs/2024-01-15_14-00/DebugLog-server.txt).
        """
        for pattern in (
            "**/DebugLog-server*.txt",
            "**/DebugLog-server*",
            "DebugLog-server*.txt",
            "DebugLog-server*",
        ):
            files = glob(os.path.join(self.log_directory, pattern), recursive=True)
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
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
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


# ---------------------------------------------------------------------------
# Server update checking via SteamCMD
# ---------------------------------------------------------------------------

class ServerUpdateChecker:
    """
    Detects and applies Project Zomboid dedicated server updates via SteamCMD.

    Build ID comparison flow:
      installed buildid  ←  steamapps/appmanifest_380870.acf
      remote buildid     ←  steamcmd +app_info_print 380870

    If installed != remote → update needed.
    If either is None (parse failure) → unknown, do not auto-update.
    """

    APP_ID = "380870"  # PZ dedicated server on Steam

    def __init__(self, config: AppConfig):
        self.config = config
        self._consecutive_failures: int = 0
        self._MAX_FAILURES: int = 5

    def get_installed_buildid(self) -> Optional[str]:
        """
        Read buildid from <server_dir>/steamapps/appmanifest_380870.acf.
        Returns None if the file is missing or the buildid key is absent.
        """
        acf_path = os.path.join(
            self.config.server_dir,
            "steamapps",
            f"appmanifest_{self.APP_ID}.acf",
        )
        try:
            with open(acf_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = re.search(r'"buildid"\s+"(\d+)"', line)
                    if m:
                        return m.group(1)
        except OSError:
            logger.debug("appmanifest not found at: %s", acf_path)
        return None

    def get_remote_buildid(self) -> Optional[str]:
        """
        Query SteamCMD for the current depot buildid.

        Parses each output line with:
            re.search(r'^\\s+"buildid"\\s+"(\\d+)"', line)

        Timeout: 30s. On failure, increments _consecutive_failures.
        After _MAX_FAILURES consecutive failures, logs a WARNING.
        On success, resets _consecutive_failures.
        """
        if not self.config.steamcmd_path:
            return None
        try:
            result = subprocess.run(
                [
                    self.config.steamcmd_path,
                    "+login", "anonymous",
                    "+app_info_print", self.APP_ID,
                    "+quit",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                **_SUBPROCESS_FLAGS,
            )
            output = result.stdout + result.stderr
            # Only match buildid lines that appear after the app ID header in the
            # output, so we never pick up a stray match from SteamCMD preamble text.
            in_app_section = False
            for line in output.splitlines():
                if f'"{self.APP_ID}"' in line:
                    in_app_section = True
                if in_app_section:
                    m = re.search(r'^\s+"buildid"\s+"(\d+)"', line)
                    if m:
                        self._consecutive_failures = 0
                        return m.group(1)
            logger.debug("SteamCMD output (no buildid found):\n%s", output[:2000])
        except subprocess.TimeoutExpired:
            logger.warning("SteamCMD app_info_print timed out after 30s.")
        except Exception as exc:
            logger.warning("SteamCMD get_remote_buildid failed: %s", exc)

        self._consecutive_failures += 1
        if self._consecutive_failures >= self._MAX_FAILURES:
            logger.warning(
                "Server update checks failing (%d consecutive) — "
                "verify SteamCMD path and network access.",
                self._consecutive_failures,
            )
        return None

    def update_needed(self) -> Optional[bool]:
        """
        Compare installed vs. remote buildid.

        Returns:
          True   — buildids differ (update available)
          False  — buildids match (up to date)
          None   — either buildid is None (check failed; caller must NOT auto-update)
        """
        installed = self.get_installed_buildid()
        remote = self.get_remote_buildid()
        if installed is None or remote is None:
            return None
        return installed != remote

    def run_update(self) -> bool:
        """
        Run SteamCMD to update the server files in place.

        Precondition: server process must already be stopped.

        Uses +force_install_dir with an os.path.normpath'd path (handles
        forward-slash input on Windows). +validate is included by default
        to detect and repair partial downloads.

        Timeout: config.steamcmd_timeout (default 600s).
        Returns True on exit code 0, False on failure or timeout.
        """
        server_dir_norm = os.path.normpath(self.config.server_dir)
        try:
            result = subprocess.run(
                [
                    self.config.steamcmd_path,
                    "+login", "anonymous",
                    "+force_install_dir", server_dir_norm,
                    "+app_update", self.APP_ID, "validate",
                    "+quit",
                ],
                capture_output=True,
                text=True,
                timeout=self.config.steamcmd_timeout,
                **_SUBPROCESS_FLAGS,
            )
            if result.returncode == 0:
                logger.info("SteamCMD update completed successfully.")
                return True
            logger.error(
                "SteamCMD exited with code %d.\nOutput: %s",
                result.returncode,
                result.stdout[-2000:],
            )
            return False
        except subprocess.TimeoutExpired:
            logger.error(
                "SteamCMD update timed out after %ds.", self.config.steamcmd_timeout
            )
            return False
        except Exception as exc:
            logger.error("SteamCMD run_update failed: %s", exc)
            return False
