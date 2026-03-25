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
import yaml

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
    logger.debug("_config_dir: resolved base appdata path=%r", appdata)
    d = Path(appdata) / "PZServerManager"
    d.mkdir(parents=True, exist_ok=True)
    logger.debug("_config_dir: ensured directory exists at %s", d)
    return d


def _config_path() -> Path:
    path = _config_dir() / "pz_server_config.ini"
    logger.debug("_config_path: resolved config file path=%s", path)
    return path


def _legacy_config_path() -> Path:
    """Path to the old config next to the EXE (pre-v0.2 location)."""
    path = Path("pz_server_config.ini")
    logger.debug("_legacy_config_path: resolved legacy config path=%s", path)
    return path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class AppConfig:
    def __init__(self):
        logger.debug("AppConfig.__init__: initializing default configuration")
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
        self.steam_branch: str = "public"
        self._save_lock = threading.Lock()
        self.last_error = ""
        logger.debug(
            "AppConfig.__init__: defaults set "
            "server_dir=%r zomboid_dir=%r rcon_path=%r server_ip=%r server_name=%r "
            "check_interval=%d last_update=%r scheduled_restart=%r steamcmd_path=%r "
            "server_update_interval=%d steamcmd_timeout=%d "
            "last_server_update_check=%r steam_branch=%r",
            self.server_dir,
            self.zomboid_dir,
            self.rcon_path,
            self.server_ip,
            self.server_name,
            self.check_interval,
            self.last_update,
            self.scheduled_restart,
            self.steamcmd_path,
            self.server_update_interval,
            self.steamcmd_timeout,
            self.last_server_update_check,
            self.steam_branch,
            self.last_error,
        )

    def load(self):
        logger.debug("AppConfig.load: starting")
        # Migrate legacy config (next to EXE) to %APPDATA% on first run.
        legacy = _legacy_config_path()
        dest = _config_path()
        logger.debug("AppConfig.load: legacy=%s dest=%s", legacy, dest)

        if legacy.exists() and not dest.exists():
            logger.debug("AppConfig.load: legacy exists and dest missing; attempting migration")
            try:
                legacy.rename(dest)
                logger.info("Migrated config from %s to %s", legacy, dest)
            except OSError as exc:
                logger.warning("Config migration failed: %s — reading legacy path.", exc)
                dest = legacy
                logger.debug("AppConfig.load: falling back to legacy path=%s", dest)

        config = configparser.ConfigParser()
        if not dest.exists():
            logger.info("No config file found — using defaults.")
            logger.debug("AppConfig.load: no config file at %s", dest)
            # Still try to load the password from Credential Manager in case
            # the INI was deleted but the credential was not.
            self.password = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME) or ""
            logger.debug("AppConfig.load: password loaded from keyring; length=%d", len(self.password))
            return

        logger.debug("AppConfig.load: reading config from %s", dest)
        config.read(dest)
        logger.debug("AppConfig.load: sections=%s", config.sections())
        s = config["SETTINGS"] if "SETTINGS" in config else {}
        logger.debug("AppConfig.load: SETTINGS present=%s", "SETTINGS" in config)

        self.server_dir = s.get("server_dir", "")
        self.zomboid_dir = s.get("zomboid_dir", "")
        self.rcon_path = s.get("rcon_path", "")
        self.server_ip = s.get("server_ip", "")
        self.server_name = s.get("server_name", "")
        logger.debug(
            "AppConfig.load: loaded string settings "
            "server_dir=%r zomboid_dir=%r rcon_path=%r server_ip=%r server_name=%r",
            self.server_dir,
            self.zomboid_dir,
            self.rcon_path,
            self.server_ip,
            self.server_name,
        )

        try:
            self.check_interval = int(s.get("check_interval", "60"))
            logger.debug("AppConfig.load: check_interval=%d", self.check_interval)
        except ValueError:
            self.check_interval = 60
            logger.debug("AppConfig.load: invalid check_interval; defaulted to %d", self.check_interval)

        self.last_update = s.get("last_update", "")
        self.scheduled_restart = s.get("scheduled_restart", "")
        self.steamcmd_path = s.get("steamcmd_path", "")
        logger.debug(
            "AppConfig.load: loaded misc string settings "
            "last_update=%r scheduled_restart=%r steamcmd_path=%r",
            self.last_update,
            self.scheduled_restart,
            self.steamcmd_path,
        )

        try:
            self.server_update_interval = int(s.get("server_update_interval", "60"))
            logger.debug("AppConfig.load: server_update_interval=%d", self.server_update_interval)
        except ValueError:
            self.server_update_interval = 60
            logger.debug(
                "AppConfig.load: invalid server_update_interval; defaulted to %d",
                self.server_update_interval,
            )

        try:
            self.steamcmd_timeout = int(s.get("steamcmd_timeout", "600"))
            logger.debug("AppConfig.load: steamcmd_timeout=%d", self.steamcmd_timeout)
        except ValueError:
            self.steamcmd_timeout = 600
            logger.debug("AppConfig.load: invalid steamcmd_timeout; defaulted to %d", self.steamcmd_timeout)

        self.last_server_update_check = s.get("last_server_update_check", "")
        self.steam_branch = s.get("steam_branch", "public")
        logger.debug(
            "AppConfig.load: loaded final settings last_server_update_check=%r steam_branch=%r",
            self.last_server_update_check,
            self.steam_branch,
        )

        # If old config had a plaintext password, migrate it to Credential Manager
        # and remove it from the INI.
        legacy_pw = s.get("password", "")
        logger.debug("AppConfig.load: legacy plaintext password present=%s", bool(legacy_pw))
        if legacy_pw:
            try:
                keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, legacy_pw)
                logger.info("Migrated plaintext password to Windows Credential Manager.")
                logger.debug("AppConfig.load: migrated plaintext password; length=%d", len(legacy_pw))
            except Exception as exc:
                logger.warning("Could not migrate password to Credential Manager: %s", exc)
            # Set password before save() so it doesn't call delete_password() on the
            # credential we just migrated.
            self.password = legacy_pw
            logger.debug("AppConfig.load: saving config to remove plaintext password field")
            # Rewrite the INI without the password field.
            self.save()

        # Load the password from Credential Manager (never from the INI file).
        try:
            self.password = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME) or ""
            logger.debug("AppConfig.load: reloaded password from keyring; length=%d", len(self.password))
        except Exception as exc:
            logger.warning("Could not read password from Credential Manager: %s", exc)
            self.password = ""
            logger.debug("AppConfig.load: password cleared after keyring read failure")

        logger.info("Configuration loaded from %s", dest)
        logger.debug(
            "AppConfig.load: final state "
            "server_dir=%r zomboid_dir=%r rcon_path=%r server_ip=%r server_name=%r "
            "check_interval=%d last_update=%r scheduled_restart=%r steamcmd_path=%r "
            "server_update_interval=%d steamcmd_timeout=%d last_server_update_check=%r "
            "steam_branch=%r password_len=%d",
            self.server_dir,
            self.zomboid_dir,
            self.rcon_path,
            self.server_ip,
            self.server_name,
            self.check_interval,
            self.last_update,
            self.scheduled_restart,
            self.steamcmd_path,
            self.server_update_interval,
            self.steamcmd_timeout,
            self.last_server_update_check,
            self.steam_branch,
            len(self.password),
        )

    def save(self):
        logger.debug("AppConfig.save: attempting save")
        with self._save_lock:
            logger.debug("AppConfig.save: save lock acquired")
            self._save_locked()
        logger.debug("AppConfig.save: save complete")

    def _save_locked(self):
        logger.debug(
            "AppConfig._save_locked: persisting config "
            "server_dir=%r zomboid_dir=%r rcon_path=%r server_ip=%r server_name=%r "
            "check_interval=%d last_update=%r scheduled_restart=%r steamcmd_path=%r "
            "server_update_interval=%d steamcmd_timeout=%d last_server_update_check=%r "
            "steam_branch=%r password_len=%d",
            self.server_dir,
            self.zomboid_dir,
            self.rcon_path,
            self.server_ip,
            self.server_name,
            self.check_interval,
            self.last_update,
            self.scheduled_restart,
            self.steamcmd_path,
            self.server_update_interval,
            self.steamcmd_timeout,
            self.last_server_update_check,
            self.steam_branch,
            len(self.password),
        )

        # Persist the password to Windows Credential Manager first.
        try:
            if self.password:
                logger.debug("AppConfig._save_locked: saving password to keyring; length=%d", len(self.password))
                keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, self.password)
            else:
                logger.debug("AppConfig._save_locked: password empty; deleting stored keyring password if present")
                # Delete the stored credential if the password was cleared.
                try:
                    keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
                    logger.debug("AppConfig._save_locked: deleted keyring password")
                except keyring.errors.PasswordDeleteError:
                    logger.debug("AppConfig._save_locked: no keyring password existed to delete")
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
            "steam_branch": self.steam_branch,
        }
        logger.debug("AppConfig._save_locked: config payload=%s", dict(config["SETTINGS"]))

        dest = _config_path()
        dest_dir = os.path.dirname(dest)
        logger.debug("AppConfig._save_locked: dest=%s dest_dir=%s", dest, dest_dir)

        with tempfile.NamedTemporaryFile("w", dir=dest_dir, delete=False, suffix=".tmp") as f:
            tmp_path = f.name
            logger.debug("AppConfig._save_locked: writing temp file=%s", tmp_path)
            config.write(f)

        os.replace(tmp_path, dest)
        logger.debug("AppConfig._save_locked: replaced %s with temp file %s", dest, tmp_path)
        logger.info("Configuration saved to %s", dest)


# ---------------------------------------------------------------------------
# Server management
# ---------------------------------------------------------------------------

class ServerManager:
    def __init__(self, config: AppConfig):
        self.config = config
        logger.debug("ServerManager.__init__: config id=%s", id(config))

    def is_running(self) -> bool:
        """Return True if a matching java.exe server process is running.

        Raises subprocess.TimeoutExpired if PowerShell does not respond within
        10 seconds, so callers are never blocked indefinitely.
        """
        name = self.config.server_name
        logger.debug("ServerManager.is_running: server_name=%r", name)

        if name:
            # Escape single-quotes for PowerShell -like patterns ('' = literal ')
            safe_name = name.replace("'", "''")
            filter_clause = (
                f"$_.CommandLine -like '*{safe_name}*' -and "
                f"$_.CommandLine -like '*-Djava.awt.headless=true*'"
            )
            logger.debug("ServerManager.is_running: using named filter_clause=%s", filter_clause)
        else:
            filter_clause = "$_.CommandLine -like '*-Djava.awt.headless=true*'"
            logger.debug("ServerManager.is_running: using generic filter_clause=%s", filter_clause)

        cmd = (
            f"powershell -Command \""
            f"Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.Name -eq 'java.exe' -and {filter_clause} }} | "
            f"Select-Object -First 1\""
        )
        logger.debug("ServerManager.is_running: command=%s", cmd)

        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=10
        )
        logger.debug(
            "ServerManager.is_running: returncode=%s stdout=%r stderr=%r",
            result.returncode,
            result.stdout[:1000],
            result.stderr[:1000],
        )
        running = bool(result.stdout.strip())
        logger.debug("ServerManager.is_running: running=%s", running)
        return running

    def start(self):
        """Launch the server via StartServer64.bat <server_name> in its own console window."""
        logger.debug(
            "ServerManager.start: server_dir=%r server_name=%r",
            self.config.server_dir,
            self.config.server_name,
        )

        if not self.config.server_dir:
            raise ValueError("Server directory not configured.")

        if not self.config.server_name:
            raise ValueError("Server name not configured.")

        server_dir = os.path.normpath(self.config.server_dir)
        batch = os.path.normpath(os.path.join(server_dir, "StartServer64.bat"))
        server_name = self.config.server_name.strip()

        logger.debug("ServerManager.start: normalized server_dir=%r", server_dir)
        logger.debug("ServerManager.start: normalized batch=%r", batch)
        logger.debug("ServerManager.start: normalized server_name=%r", server_name)

        if not os.path.isdir(server_dir):
            raise FileNotFoundError(f"Server directory not found: {server_dir}")

        if not os.path.isfile(batch):
            raise FileNotFoundError(f"StartServer64.bat not found: {batch}")

        if not server_name:
            raise ValueError("Server name is blank.")

        args = ["cmd.exe", "/c", batch, "-servername", f"{server_name}"]
        logger.debug(
            "ServerManager.start: launching args=%r cwd=%r creationflags=CREATE_NEW_CONSOLE",
            args,
            server_dir,
        )

        proc = subprocess.Popen(
            args,
            cwd=server_dir,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        logger.debug("ServerManager.start: subprocess launched pid=%s", proc.pid)

        logger.info("Server start command sent for server name %r.", server_name)

    def stop(self):
        """Send the RCON quit command."""
        logger.debug("ServerManager.stop: issuing quit command")
        self._rcon("quit")
        logger.info("Server stop command sent.")

    def broadcast(self, message: str):
        """Send an in-game server-wide message via RCON servermsg."""
        logger.debug("ServerManager.broadcast: message=%r", message)
        safe = message.replace('"', '\\"')
        logger.debug("ServerManager.broadcast: escaped message=%r", safe)
        cmd = f'servermsg "{safe}"'
        logger.debug("ServerManager.broadcast: command=%r", cmd)
        self._rcon(cmd)
        logger.info("Broadcast sent: %s", message)

    def check_mods_need_update(self) -> bool:
        logger.debug("ServerManager.check_mods_need_update: issuing checkModsNeedUpdate")
        result = self._rcon("checkModsNeedUpdate", capture=False)
        if result is None:
            logger.error("ServerManager.check_mods_need_update: failed to send command")
            return False
        logger.info("ServerManager.check_mods_need_update: command sent successfully")
        return True

    def get_player_count(self) -> int:
        """Return the number of connected players via RCON, or 0 on failure."""
        logger.debug("ServerManager.get_player_count: requesting players list")
        output = self._rcon("players", capture=True)
        logger.debug("ServerManager.get_player_count: raw output=%r", output[:2000] if output else output)
        if not output:
            logger.debug("ServerManager.get_player_count: no output; returning 0")
            return 0
        for line in output.splitlines():
            logger.debug("ServerManager.get_player_count: parsing line=%r", line)
            if "Players connected" in line:
                try:
                    count = int(line.split("(")[1].split(")")[0])
                    logger.debug("ServerManager.get_player_count: parsed count=%d", count)
                    return count
                except (IndexError, ValueError) as exc:
                    logger.debug("ServerManager.get_player_count: parse failed for line=%r error=%s", line, exc)
                    pass
        logger.debug("ServerManager.get_player_count: no player count line found; returning 0")
        return 0

    def send_command(self, command: str) -> str:
        """Send an arbitrary RCON command and return its trimmed output."""
        logger.debug("ServerManager.send_command: command=%r", command)
        output = self._rcon(command, capture=True)
        logger.debug("ServerManager.send_command: raw output=%r", output[:2000] if output else output)
        final = output.strip() if output else "(no output)"
        logger.debug("ServerManager.send_command: final output=%r", final)
        return final

    def _rcon(self, command: str, capture: bool = False) -> str | None:
        rcon_path = self.config.rcon_path
        address = self.config.server_ip
        password = self.config.password

        if not rcon_path:
            logger.error("ServerManager._rcon: RCON executable not configured")
            return None
        if not address:
            logger.error("ServerManager._rcon: server IP/address not configured")
            return None
        if not password:
            logger.error("ServerManager._rcon: password not configured")
            return None

        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(suffix=".yaml")
            os.close(fd)

            cfg = {
                "default": {
                    "address": str(address),
                    "password": str(password),
                }
            }

            with open(temp_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)

            args = [
                rcon_path,
                "-c",
                temp_path,
                command,
            ]

            logger.debug("ServerManager._rcon: running args=%r", args)

            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=False,
            )

            logger.debug(
                "ServerManager._rcon: returncode=%s stdout=%r stderr=%r",
                proc.returncode,
                proc.stdout,
                proc.stderr,
            )

            if proc.returncode != 0:
                logger.error(
                    "ServerManager._rcon: command failed: %s",
                    (proc.stderr or proc.stdout).strip(),
                )
                return None

            return proc.stdout if capture else ""

        except Exception:
            logger.exception("ServerManager._rcon: unexpected failure")
            return None

        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    logger.warning("ServerManager._rcon: could not delete temp config %s", temp_path)


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

class LogParser:
    def __init__(self, log_directory: str):
        self.log_directory = log_directory
        self._logged_first_success = False
        self._logged_missing_dir = False
        self._logged_no_files = False
        self._last_latest_log: Optional[str] = None

    def find_latest_log(self) -> Optional[str]:
        """
        Find the newest DebugLog file in the root log directory only.
        No subfolder or recursive searching.
        """
        if not self.log_directory or not os.path.isdir(self.log_directory):
            if not self._logged_missing_dir:
                logger.warning(
                    "LogParser.find_latest_log: log_directory missing or not a directory: %r",
                    self.log_directory,
                )
                self._logged_missing_dir = True
            self._logged_no_files = False
            return None

        self._logged_missing_dir = False

        patterns = [
            "*DebugLog-server.txt",
            "*DebugLog-server*",
        ]

        matches: list[str] = []
        for pattern in patterns:
            search_path = os.path.join(self.log_directory, pattern)
            matches.extend(glob(search_path))

        files = [p for p in matches if os.path.isfile(p)]
        if not files:
            if not self._logged_no_files:
                logger.warning(
                    "LogParser.find_latest_log: no files found in root directory %r",
                    self.log_directory,
                )
                self._logged_no_files = True
            return None

        self._logged_no_files = False

        latest = max(files, key=os.path.getmtime)

        if not self._logged_first_success:
            logger.info("LogParser.find_latest_log: using log file %s", latest)
            self._logged_first_success = True
            self._last_latest_log = latest
        elif latest != self._last_latest_log:
            logger.info("LogParser.find_latest_log: switched to log file %s", latest)
            self._last_latest_log = latest

        return latest

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

                    if progress_cb:
                        for line in new_lines:
                            try:
                                progress_cb(line.rstrip("\n"))
                            except Exception as exc:
                                logger.warning(
                                    "LogParser.monitor_for_mod_status: progress_cb failed: %s",
                                    exc,
                                )

                    for i, line in enumerate(collected):
                        if "Mods need update" in line:
                            mod_names = self._extract_mod_names(collected, i)
                            logger.info(
                                "LogParser.monitor_for_mod_status: mods need update (%d item(s))",
                                len(mod_names),
                            )
                            return "needs_update", mod_names

                        if "Mods updated" in line or "No mods need updating" in line:
                            logger.info(
                                "LogParser.monitor_for_mod_status: mods are up to date"
                            )
                            return "up_to_date", []

                    time.sleep(2)

        except FileNotFoundError:
            logger.error("LogParser.monitor_for_mod_status: log file not found: %s", log_file)
        except OSError as exc:
            logger.error(
                "LogParser.monitor_for_mod_status: failed reading %s: %s",
                log_file,
                exc,
            )
        except Exception:
            logger.exception("LogParser.monitor_for_mod_status: unexpected error")

        logger.warning(
            "LogParser.monitor_for_mod_status: timed out after %d seconds waiting for mod status",
            timeout,
        )
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

        inline_ids = re.findall(r"workshopID[=:](\d+)", trigger, re.IGNORECASE)
        for wid in inline_ids:
            add(f"Workshop ID: {wid}")

        for line in search_lines[1:]:
            if re.search(r"Mods updated|checkMods|No mods need", line, re.IGNORECASE):
                break

            mod_match = re.search(r"ModID[=:]\s*(\S+)", line, re.IGNORECASE)
            if mod_match:
                add(mod_match.group(1))
                continue

            line_ids = re.findall(r"workshopID[=:](\d+)", line, re.IGNORECASE)
            for wid in line_ids:
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
                    mapping = dict(zip(wids, mods))
                    logger.info("Loaded mod map from %s (%d entries).", path, len(mapping))
                    return mapping
            except OSError as exc:
                logger.warning("Could not read server config %s: %s", path, exc)
            except Exception:
                logger.exception(
                    "LogParser.load_server_mod_map: unexpected error reading %s",
                    path,
                )

        return {}

# ---------------------------------------------------------------------------
# Live log tailing
# ---------------------------------------------------------------------------

class LogTailer:
    """
    Tails the latest PZ DebugLog-server* file in a background thread,
    calling `line_callback` for each new line appended to the file.
    Automatically switches to a newer log file when one appears.
    """

    _INITIAL_RETRY_DELAY = 1.0
    _MAX_RETRY_DELAY = 60.0
    _MAX_MISSING_RETRIES = 10

    def __init__(self, log_directory: str, line_callback: Callable[[str], None]):
        self.log_directory = log_directory
        self.current_file: Optional[str] = None
        self._callback = line_callback
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._has_logged_first_success = False

    def start(self):
        if self._thread and self._thread.is_alive():
            logger.debug("LogTailer.start: tailer already running; ignoring duplicate start")
            return

        logger.debug("LogTailer.start: starting tailer thread")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="LogTailer")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _emit(self, message: str) -> None:
        try:
            self._callback(message)
        except Exception as exc:
            logger.warning("LogTailer._emit: callback failed: %s", exc)

    def _run(self):
        logger.debug("LogTailer._run: entering loop for directory=%r", self.log_directory)
        parser = LogParser(self.log_directory)
        fh = None
        missing_retries = 0
        retry_delay = self._INITIAL_RETRY_DELAY

        try:
            while not self._stop.is_set():
                latest = parser.find_latest_log()

                if not latest:
                    if fh:
                        try:
                            fh.close()
                        except OSError:
                            pass
                        fh = None
                        self.current_file = None

                    missing_retries += 1

                    if missing_retries >= self._MAX_MISSING_RETRIES:
                        msg = (
                            f"Could not find any log file in {self.log_directory} "
                            f"after {self._MAX_MISSING_RETRIES} attempts."
                        )
                        logger.error("LogTailer._run: %s", msg)
                        self._emit(f"ERROR: {msg}")
                        return

                    logger.warning(
                        "LogTailer._run: no log file found in %r (attempt %d/%d)",
                        self.log_directory,
                        missing_retries,
                        self._MAX_MISSING_RETRIES,
                    )

                    if self._stop.wait(timeout=retry_delay):
                        return

                    retry_delay = min(retry_delay * 2, self._MAX_RETRY_DELAY)
                    continue

                missing_retries = 0
                retry_delay = self._INITIAL_RETRY_DELAY

                if latest != self.current_file:
                    if fh:
                        try:
                            fh.close()
                        except OSError:
                            pass
                        fh = None

                    self.current_file = latest

                    try:
                        fh = open(latest, "r", encoding="utf-8", errors="replace")

                        fh.seek(0, os.SEEK_END)
                        size = fh.tell()
                        start_pos = max(0, size - 8192)
                        fh.seek(start_pos, os.SEEK_SET)

                        if start_pos > 0:
                            fh.readline()  # discard partial line

                        if not self._has_logged_first_success:
                            logger.info("LogTailer._run: now tailing %s", latest)
                            self._has_logged_first_success = True
                        else:
                            logger.info("LogTailer._run: switched to new log file %s", latest)

                        self._emit(f"\n─── Now tailing: {os.path.basename(latest)} ───\n")

                        initial_lines = fh.readlines()
                        for line in initial_lines:
                            self._emit(line)

                    except OSError as exc:
                        logger.error("LogTailer._run: cannot open log file %r: %s", latest, exc)
                        fh = None
                        self.current_file = None

                        if self._stop.wait(timeout=1.0):
                            return
                        continue

                if fh:
                    try:
                        lines = fh.readlines()
                        if lines:
                            for line in lines:
                                if self._stop.is_set():
                                    return
                                self._emit(line)
                    except OSError as exc:
                        logger.error("LogTailer._run: read failed for %r: %s", self.current_file, exc)
                        fh = None
                        self.current_file = None

                if self._stop.wait(timeout=0.5):
                    return

        except Exception:
            logger.exception("LogTailer._run: unexpected failure")
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
      installed buildid  ←  steamcmd +app_status 380870 (reads local depot DB)
      remote buildid     ←  steamcmd +app_info_print 380870 (depots > branches > {branch})

    If installed != remote → update needed.
    If either is None (parse failure) → unknown, do not auto-update.
    """

    APP_ID = "380870"  # PZ dedicated server on Steam
    LOCAL_BUILD_MISSING = "__LOCAL_BUILD_MISSING__"
    def __init__(self, config: AppConfig):
        self.config = config
        self._consecutive_failures: int = 0
        self._MAX_FAILURES: int = 5
        self.last_error: str = ""
        self.last_output: str = ""
        logger.debug(
            "ServerUpdateChecker.__init__: config id=%s app_id=%s max_failures=%d",
            id(config),
            self.APP_ID,
            self._MAX_FAILURES,
        )

    def get_installed_buildid(self) -> Optional[str]:
        """
        Query SteamCMD for the locally installed buildid.

        Returns:
            - buildid string if found
            - LOCAL_BUILD_MISSING if SteamCMD reports no local build
            - None on other failures
        """
        logger.debug(
            "ServerUpdateChecker.get_installed_buildid: steamcmd_path=%r server_dir=%r",
            self.config.steamcmd_path,
            self.config.server_dir,
        )
        if not self.config.steamcmd_path or not self.config.server_dir:
            logger.debug("ServerUpdateChecker.get_installed_buildid: missing steamcmd_path or server_dir")
            return None

        server_dir_norm = os.path.normpath(self.config.server_dir)
        logger.debug("ServerUpdateChecker.get_installed_buildid: normalized server_dir=%r", server_dir_norm)

        try:
            cmd = [
                self.config.steamcmd_path,
                "+force_install_dir", server_dir_norm,
                "+login", "anonymous",
                "+app_status", self.APP_ID,
                "+quit",
            ]
            logger.debug("ServerUpdateChecker.get_installed_buildid: cmd=%s", cmd)

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                **_SUBPROCESS_FLAGS,
            )

            logger.debug(
                "ServerUpdateChecker.get_installed_buildid: returncode=%s stdout_len=%d stderr_len=%d",
                result.returncode,
                len(result.stdout),
                len(result.stderr),
            )

            output = (result.stdout or "") + "\n" + (result.stderr or "")
            logger.debug(
                "ServerUpdateChecker.get_installed_buildid: output sample=%r",
                output[:2000],
            )

            lower_output = output.lower()

            missing_markers = [
                "local build",
                "cannot be found",
                "not installed",
                "no local content",
                "install state: none",
            ]

            if "local build" in lower_output and "cannot be found" in lower_output:
                logger.info(
                    "ServerUpdateChecker.get_installed_buildid: local build missing for app %s in %r",
                    self.APP_ID,
                    server_dir_norm,
                )
                return self.LOCAL_BUILD_MISSING

            # Guard: only match BuildID lines after the AppID header so we
            # never pick up SteamCMD's own installer BuildID in the preamble.
            in_app_section = False
            for line in output.splitlines():
                logger.debug(
                    "ServerUpdateChecker.get_installed_buildid: scanning line=%r in_app_section=%s",
                    line,
                    in_app_section,
                )
                if f"AppID {self.APP_ID}" in line:
                    in_app_section = True
                    logger.debug("ServerUpdateChecker.get_installed_buildid: entered app section")
                if in_app_section:
                    m = re.search(r'\bBuildID\b\s*:?\s*(\d+)', line, re.IGNORECASE)
                    if m:
                        buildid = m.group(1)
                        logger.debug("ServerUpdateChecker.get_installed_buildid: matched buildid=%s", buildid)
                        return buildid

            logger.debug("SteamCMD app_status output (no BuildID found):\n%s", output[:2000])

        except subprocess.TimeoutExpired:
            logger.warning("SteamCMD app_status timed out after 30s.")
            logger.debug("ServerUpdateChecker.get_installed_buildid: timeout")
        except Exception as exc:
            logger.warning("SteamCMD get_installed_buildid failed: %s", exc)
            logger.debug("ServerUpdateChecker.get_installed_buildid: exception=%s", exc)

        logger.debug("ServerUpdateChecker.get_installed_buildid: returning None")
        return None

    @staticmethod
    def _parse_branch_buildid(text: str, branch: str) -> Optional[str]:
        """
        Parse the buildid for a specific Steam branch from +app_info_print VDF output.

        Navigates the path: depots > branches > {branch} > buildid

        VDF structure (depth-tracked via { / } counts):

            "depots"
            {
                "branches"
                {
                    "public"
                    {
                        "buildid"    "12345"
                    }
                    "unstable"
                    {
                        "buildid"    "67890"
                    }
                }
            }

        Returns None if the branch key or buildid is absent.
        """
        logger.debug(
            "ServerUpdateChecker._parse_branch_buildid: branch=%r text_len=%d",
            branch,
            len(text),
        )
        WANT_DEPOTS, WANT_BRANCHES, WANT_BRANCH, WANT_BUILDID = range(4)
        state = WANT_DEPOTS
        depth = 0
        # Per-state entry depths so closing-brace transitions stay accurate
        # even after resets (e.g. WANT_BUILDID → WANT_BRANCHES reuse the
        # correct depth for the WANT_BRANCHES level, not the WANT_BUILDID level).
        depth_branches = 0  # depth at which we entered WANT_BRANCHES
        depth_branch = 0    # depth at which we entered WANT_BRANCH
        depth_buildid = 0   # depth at which we entered WANT_BUILDID

        for line in text.splitlines():
            stripped = line.strip()
            logger.debug(
                "ServerUpdateChecker._parse_branch_buildid: state=%d depth=%d line=%r",
                state,
                depth,
                stripped,
            )
            if stripped == "{":
                depth += 1
                logger.debug("ServerUpdateChecker._parse_branch_buildid: depth incremented to %d", depth)
                continue
            if stripped == "}":
                depth -= 1
                logger.debug("ServerUpdateChecker._parse_branch_buildid: depth decremented to %d", depth)
                if state == WANT_BRANCHES and depth < depth_branches:
                    state = WANT_DEPOTS
                    logger.debug("ServerUpdateChecker._parse_branch_buildid: state -> WANT_DEPOTS")
                elif state == WANT_BRANCH and depth < depth_branch:
                    state = WANT_BRANCHES
                    logger.debug("ServerUpdateChecker._parse_branch_buildid: state -> WANT_BRANCHES")
                elif state == WANT_BUILDID and depth < depth_buildid:
                    # Branch block closed without finding buildid — try next sibling.
                    state = WANT_BRANCH
                    logger.debug("ServerUpdateChecker._parse_branch_buildid: state -> WANT_BRANCH")
                continue

            if state == WANT_DEPOTS:
                if re.search(r'^\s*"depots"\s*$', line):
                    state = WANT_BRANCHES
                    depth_branches = depth + 1
                    logger.debug(
                        "ServerUpdateChecker._parse_branch_buildid: matched depots state=%d depth_branches=%d",
                        state,
                        depth_branches,
                    )
            elif state == WANT_BRANCHES:
                if re.search(r'^\s*"branches"\s*$', line):
                    state = WANT_BRANCH
                    depth_branch = depth + 1
                    logger.debug(
                        "ServerUpdateChecker._parse_branch_buildid: matched branches state=%d depth_branch=%d",
                        state,
                        depth_branch,
                    )
            elif state == WANT_BRANCH:
                if re.search(rf'^\s*"{re.escape(branch)}"\s*$', line):
                    state = WANT_BUILDID
                    depth_buildid = depth + 1
                    logger.debug(
                        "ServerUpdateChecker._parse_branch_buildid: matched branch=%r state=%d depth_buildid=%d",
                        branch,
                        state,
                        depth_buildid,
                    )
            elif state == WANT_BUILDID:
                m = re.search(r'^\s*"buildid"\s+"(\d+)"', line)
                if m:
                    buildid = m.group(1)
                    logger.debug("ServerUpdateChecker._parse_branch_buildid: matched buildid=%s", buildid)
                    return buildid

        logger.debug("ServerUpdateChecker._parse_branch_buildid: no buildid found for branch=%r", branch)
        return None

    def get_remote_buildid(self) -> Optional[str]:
        """
        Query SteamCMD for the remote buildid on the configured Steam branch.

        Runs: steamcmd +login anonymous +app_info_print 380870 +quit

        Parses VDF output for: depots > branches > {steam_branch} > buildid

        Timeout: 30s. On failure, increments _consecutive_failures.
        After _MAX_FAILURES consecutive failures, logs a WARNING.
        On success, resets _consecutive_failures.
        """
        logger.debug(
            "ServerUpdateChecker.get_remote_buildid: steamcmd_path=%r steam_branch=%r failures=%d",
            self.config.steamcmd_path,
            getattr(self.config, "steam_branch", None),
            self._consecutive_failures,
        )
        if not self.config.steamcmd_path:
            logger.debug("ServerUpdateChecker.get_remote_buildid: missing steamcmd_path")
            return None

        branch = getattr(self.config, "steam_branch", "public") or "public"
        logger.debug("ServerUpdateChecker.get_remote_buildid: normalized branch=%r", branch)
        try:
            cmd = [
                self.config.steamcmd_path,
                "+login", "anonymous",
                "+app_info_print", self.APP_ID,
                "+quit",
            ]
            logger.debug("ServerUpdateChecker.get_remote_buildid: cmd=%s", cmd)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                **_SUBPROCESS_FLAGS,
            )
            logger.debug(
                "ServerUpdateChecker.get_remote_buildid: returncode=%s stdout_len=%d stderr_len=%d",
                result.returncode,
                len(result.stdout),
                len(result.stderr),
            )

            output = result.stdout + result.stderr
            logger.debug(
                "ServerUpdateChecker.get_remote_buildid: output sample=%r",
                output[:2000],
            )
            buildid = self._parse_branch_buildid(output, branch)
            logger.debug("ServerUpdateChecker.get_remote_buildid: parsed buildid=%r", buildid)
            if buildid is not None:
                self._consecutive_failures = 0
                logger.debug("ServerUpdateChecker.get_remote_buildid: reset consecutive failures")
                return buildid

            logger.debug(
                "SteamCMD output (no buildid for branch '%s'):\n%s", branch, output[:2000]
            )
        except subprocess.TimeoutExpired:
            logger.warning("SteamCMD app_info_print timed out after 30s.")
            logger.debug("ServerUpdateChecker.get_remote_buildid: timeout")
        except Exception as exc:
            logger.warning("SteamCMD get_remote_buildid failed: %s", exc)
            logger.debug("ServerUpdateChecker.get_remote_buildid: exception=%s", exc)

        self._consecutive_failures += 1
        logger.debug(
            "ServerUpdateChecker.get_remote_buildid: incremented consecutive failures to %d",
            self._consecutive_failures,
        )
        if self._consecutive_failures >= self._MAX_FAILURES:
            logger.warning(
                "Server update checks failing (%d consecutive) — "
                "verify SteamCMD path and network access.",
                self._consecutive_failures,
            )
        logger.debug("ServerUpdateChecker.get_remote_buildid: returning None")
        return None

    def update_needed(self) -> Optional[bool]:
        """
        Compare installed vs. remote buildid.

        Returns:
        True   — buildids differ OR local build is missing (fresh install needed)
        False  — buildids match
        None   — remote buildid missing or another check failed
        """
        logger.debug("ServerUpdateChecker.update_needed: starting comparison")

        installed = self.get_installed_buildid()
        logger.debug("ServerUpdateChecker.update_needed: installed=%r", installed)

        remote = self.get_remote_buildid()
        logger.debug("ServerUpdateChecker.update_needed: remote=%r", remote)

        if installed == self.LOCAL_BUILD_MISSING or installed is None:
            logger.info(
                "ServerUpdateChecker.update_needed: local build missing; treating as install needed"
            )
            return True

        if remote is None:
            logger.debug(
                "ServerUpdateChecker.update_needed: remote build ids unavailable; returning None"
            )
            return None

        needed = installed != remote
        logger.debug(
            "ServerUpdateChecker.update_needed: installed=%r remote=%r needed=%s",
            installed,
            remote,
            needed,
        )
        return needed


    def run_update(self, progress_cb: Optional[Callable[[str], None]] = None) -> bool:
        """
        Run SteamCMD app_update for the configured Project Zomboid server branch
        and stream live SteamCMD output to progress_cb as lines arrive.
        """
        self.last_error = ""
        self.last_output = ""

        logger.debug(
            "ServerUpdateChecker.run_update: steamcmd_path=%r server_dir=%r branch=%r timeout=%d progress_cb_set=%s",
            self.config.steamcmd_path,
            self.config.server_dir,
            self.config.steam_branch,
            self.config.steamcmd_timeout,
            progress_cb is not None,
        )

        def emit(line: str) -> None:
            logger.debug("ServerUpdateChecker.run_update.emit: %r", line)
            if progress_cb:
                try:
                    progress_cb(line)
                except Exception as exc:
                    logger.debug(
                        "ServerUpdateChecker.run_update: progress_cb failed for line=%r error=%s",
                        line,
                        exc,
                    )

        if not self.config.steamcmd_path:
            self.last_error = "SteamCMD path is not configured."
            logger.error(self.last_error)
            emit(self.last_error)
            return False

        if not self.config.server_dir:
            self.last_error = "Server directory is not configured."
            logger.error(self.last_error)
            emit(self.last_error)
            return False

        steamcmd_path = os.path.normpath(self.config.steamcmd_path)
        steamcmd_dir = os.path.dirname(steamcmd_path)
        server_dir_norm = os.path.normpath(self.config.server_dir)
        branch = (self.config.steam_branch or "public").strip()

        logger.debug(
            "ServerUpdateChecker.run_update: server_dir_norm=%r exists=%s",
            server_dir_norm,
            os.path.isdir(server_dir_norm),
        )
        logger.debug(
            "ServerUpdateChecker.run_update: steamcmd_path_exists=%s",
            os.path.isfile(steamcmd_path),
        )
        logger.debug(
            "ServerUpdateChecker.run_update: cwd=%r exists=%s",
            steamcmd_dir,
            os.path.isdir(steamcmd_dir),
        )

        if not os.path.isfile(steamcmd_path):
            self.last_error = f"SteamCMD executable not found: {steamcmd_path}"
            logger.error(self.last_error)
            emit(self.last_error)
            return False

        if not os.path.isdir(server_dir_norm):
            try:
                os.makedirs(server_dir_norm, exist_ok=True)
                logger.info("Created missing server directory: %s", server_dir_norm)
                emit(f"Created missing server directory: {server_dir_norm}")
            except Exception as exc:
                self.last_error = f"Could not create server directory {server_dir_norm}: {exc}"
                logger.error(self.last_error)
                emit(self.last_error)
                return False

        if not os.path.isdir(steamcmd_dir):
            self.last_error = f"SteamCMD working directory not found: {steamcmd_dir}"
            logger.error(self.last_error)
            emit(self.last_error)
            return False

        command_parts = [
            f'"{steamcmd_path}"',
            f'+force_install_dir "{server_dir_norm}"',
            '+login anonymous',
            f'+app_update {self.APP_ID}',
        ]

        if branch and branch != "public":
            command_parts.append(f'-beta {branch}')

        command_parts.append("validate")
        command_parts.append("+quit")

        command_str = " ".join(command_parts)

        logger.debug("ServerUpdateChecker.run_update: command_str=%r", command_str)

        bat_path = os.path.join(steamcmd_dir, "_pzserver_update_tmp.bat")
        bat_contents = "\r\n".join([
            "@echo off",
            "setlocal",
            f'cd /d "{steamcmd_dir}"',
            command_str,
            "exit /b %ERRORLEVEL%",
            "",
        ])

        try:
            with open(bat_path, "w", encoding="utf-8", newline="") as f:
                f.write(bat_contents)

            logger.debug("ServerUpdateChecker.run_update: wrote temp batch file=%r", bat_path)
            logger.debug("ServerUpdateChecker.run_update: batch contents=%r", bat_contents)

            emit(f"Running SteamCMD update for branch '{branch}'...")
            emit(f"Working directory: {steamcmd_dir}")
            emit(f"Install directory: {server_dir_norm}")

            proc = subprocess.Popen(
                ["cmd.exe", "/c", bat_path],
                cwd=steamcmd_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
            )

            logger.debug("ServerUpdateChecker.run_update: spawned process pid=%s", proc.pid)

            output_lines: list[str] = []
            start = time.monotonic()

            assert proc.stdout is not None

            buffer = ""

            while True:
                if time.monotonic() - start > self.config.steamcmd_timeout:
                    proc.kill()
                    self.last_error = (
                        f"SteamCMD update timed out after {self.config.steamcmd_timeout} seconds."
                    )
                    logger.warning(self.last_error)
                    emit(self.last_error)
                    self.last_output = "\n".join(output_lines)
                    return False

                chunk = proc.stdout.read(1)

                if chunk:
                    text = chunk.decode("utf-8", errors="replace")
                    buffer += text

                    while True:
                        nl_pos = buffer.find("\n")
                        cr_pos = buffer.find("\r")

                        positions = [p for p in (nl_pos, cr_pos) if p != -1]
                        if not positions:
                            break

                        pos = min(positions)
                        line = buffer[:pos].strip()
                        buffer = buffer[pos + 1 :]

                        if line:
                            output_lines.append(line)
                            emit(line)

                elif proc.poll() is not None:
                    break
                else:
                    time.sleep(0.05)

            if buffer.strip():
                tail = buffer.strip()
                output_lines.append(tail)
                emit(tail)

            returncode = proc.returncode if proc.returncode is not None else -1
            combined_output = "\n".join(output_lines).strip()
            self.last_output = combined_output

            logger.debug(
                "ServerUpdateChecker.run_update: process finished returncode=%s output_len=%d output_tail=%r",
                returncode,
                len(combined_output),
                combined_output[-2000:],
            )

            if returncode != 0:
                error_line = ""
                for line in output_lines:
                    s = line.strip()
                    if "Error!" in s or "state is" in s or "failed" in s.lower():
                        error_line = s
                        break

                self.last_error = (
                    f"SteamCMD exited with code {returncode}: "
                    f"{error_line or 'update failed'}"
                )
                logger.error("SteamCMD exited with code %s. Output: %s", returncode, combined_output)
                emit(self.last_error)
                return False

            logger.info("SteamCMD update completed successfully for branch %r.", branch)
            emit("SteamCMD update completed successfully.")
            return True

        except Exception as exc:
            self.last_error = f"SteamCMD update failed: {exc}"
            logger.exception("ServerUpdateChecker.run_update failed")
            emit(self.last_error)
            return False

        finally:
            try:
                if os.path.exists(bat_path):
                    os.remove(bat_path)
                    logger.debug("ServerUpdateChecker.run_update: removed temp batch file=%r", bat_path)
            except Exception as exc:
                logger.warning("Could not remove temp batch file %r: %s", bat_path, exc)
    """
    def run_update(self) -> bool:
        
        Run SteamCMD app_update for the configured Project Zomboid server branch
        through a temporary batch file, to mirror a manual working Windows shell run.
       
        self.last_error = ""
        self.last_output = ""

        logger.debug(
            "ServerUpdateChecker.run_update: steamcmd_path=%r server_dir=%r branch=%r timeout=%d",
            self.config.steamcmd_path,
            self.config.server_dir,
            self.config.steam_branch,
            self.config.steamcmd_timeout,
        )

        if not self.config.steamcmd_path:
            self.last_error = "SteamCMD path is not configured."
            logger.error(self.last_error)
            return False

        if not self.config.server_dir:
            self.last_error = "Server directory is not configured."
            logger.error(self.last_error)
            return False

        steamcmd_path = os.path.normpath(self.config.steamcmd_path)
        steamcmd_dir = os.path.dirname(steamcmd_path)
        server_dir_norm = os.path.normpath(self.config.server_dir)
        branch = (self.config.steam_branch or "public").strip()

        logger.debug(
            "ServerUpdateChecker.run_update: server_dir_norm=%r exists=%s",
            server_dir_norm,
            os.path.isdir(server_dir_norm),
        )
        logger.debug(
            "ServerUpdateChecker.run_update: steamcmd_path_exists=%s",
            os.path.isfile(steamcmd_path),
        )
        logger.debug(
            "ServerUpdateChecker.run_update: cwd=%r exists=%s",
            steamcmd_dir,
            os.path.isdir(steamcmd_dir),
        )

        if not os.path.isfile(steamcmd_path):
            self.last_error = f"SteamCMD executable not found: {steamcmd_path}"
            logger.error(self.last_error)
            return False

        if not os.path.isdir(server_dir_norm):
            self.last_error = f"Server directory not found: {server_dir_norm}"
            logger.error(self.last_error)
            return False

        if not os.path.isdir(steamcmd_dir):
            self.last_error = f"SteamCMD working directory not found: {steamcmd_dir}"
            logger.error(self.last_error)
            return False

        # Build the exact manual-style SteamCMD command.
        command_parts = [
            f'"{steamcmd_path}"',
            f'+force_install_dir "{server_dir_norm}"',
            '+login anonymous',
            f'+app_update {self.APP_ID}',
        ]

        if branch and branch != "public":
            command_parts.append(f'-beta {branch}')

        command_parts.append('validate')
        command_parts.append('+quit')

        command_str = " ".join(command_parts)

        logger.debug("ServerUpdateChecker.run_update: command_str=%r", command_str)

        bat_path = os.path.join(steamcmd_dir, "_pzserver_update_tmp.bat")
        #bat_path = os.path.abspath(os.path.join(steamcmd_dir, "_pzserver_update_tmp.bat"))
        bat_contents = "\r\n".join([
            "@echo off",
            "setlocal",
            f'cd /d "{steamcmd_dir}"',
            command_str,
            "exit /b %ERRORLEVEL%",
            "",
        ])

        try:
            with open(bat_path, "w", encoding="utf-8", newline="") as f:
                f.write(bat_contents)

            logger.debug("ServerUpdateChecker.run_update: wrote temp batch file=%r", bat_path)
            logger.debug("ServerUpdateChecker.run_update: batch contents=%r", bat_contents)
            
            result = subprocess.run(
                ["cmd.exe", "/c", bat_path],
                capture_output=True,
                text=True,
                timeout=self.config.steamcmd_timeout,
                cwd=steamcmd_dir,
            )
                       

            self.last_error = ""
            self.last_output = f"Launched visible batch: {bat_path}"
            logger.info("ServerUpdateChecker.run_update: launched visible SteamCMD batch for inspection")

            combined_output = (
                (result.stdout or "")
                + ("\n" if result.stdout and result.stderr else "")
                + (result.stderr or "")
            ).strip()
            self.last_output = combined_output

            logger.debug(
                "ServerUpdateChecker.run_update: returncode=%s stdout_len=%d stderr_len=%d stdout_tail=%r stderr_tail=%r",
                result.returncode,
                len(result.stdout or ""),
                len(result.stderr or ""),
                (result.stdout or "")[-2000:],
                (result.stderr or "")[-2000:],
            )
            logger.debug(
                "ServerUpdateChecker.run_update: SteamCMD batch process finished with returncode=%s",
                result.returncode,
            )

            if result.returncode != 0:
                error_line = ""
                for line in combined_output.splitlines():
                    s = line.strip()
                    if "Error!" in s or "state is" in s or "failed" in s.lower():
                        error_line = s
                        break

                self.last_error = (
                    f"SteamCMD exited with code {result.returncode}: "
                    f"{error_line or 'update failed'}"
                )

                logger.error(
                    "SteamCMD exited with code %s.\nOutput: %s",
                    result.returncode,
                    combined_output,
                )
                logger.debug("ServerUpdateChecker.run_update: last_error=%r", self.last_error)
                logger.debug("ServerUpdateChecker.run_update: returning False due to non-zero exit code")
                return False

            logger.info("SteamCMD update completed successfully for branch %r.", branch)
            logger.debug("ServerUpdateChecker.run_update: returning True")
            return True

        except subprocess.TimeoutExpired:
            self.last_error = (
                f"SteamCMD update timed out after {self.config.steamcmd_timeout} seconds."
            )
            logger.warning(self.last_error)
            logger.debug("ServerUpdateChecker.run_update: returning False due to timeout")
            return False

        except Exception as exc:
            self.last_error = f"SteamCMD update failed: {exc}"
            logger.exception("ServerUpdateChecker.run_update failed")
            logger.debug("ServerUpdateChecker.run_update: last_error=%r", self.last_error)
            logger.debug("ServerUpdateChecker.run_update: returning False due to exception")
            return False

        finally:
            try:
                if os.path.exists(bat_path):
                    os.remove(bat_path)
                    logger.debug("ServerUpdateChecker.run_update: removed temp batch file=%r", bat_path)
            except Exception as exc:
                logger.warning("Could not remove temp batch file %r: %s", bat_path, exc)
    """