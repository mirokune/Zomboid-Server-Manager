"""
test_backend.py — Unit tests for backend.py

Covers ServerUpdateChecker and AppConfig persistence for the new
SteamCMD server update pipeline fields.

Run with:  python -m pytest test_backend.py -v
       or: python -m unittest test_backend -v
"""

import configparser
import os
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from backend import AppConfig, LogParser, ServerUpdateChecker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**kwargs) -> AppConfig:
    """Return an AppConfig with the given fields set."""
    cfg = AppConfig()
    cfg.server_dir = kwargs.get("server_dir", r"C:\pz_server")
    cfg.steamcmd_path = kwargs.get("steamcmd_path", r"C:\steamcmd\steamcmd.exe")
    cfg.steamcmd_timeout = kwargs.get("steamcmd_timeout", 600)
    return cfg


APP_STATUS_OUTPUT_12345 = (
    "AppID 380870 scheduler :\n"
    " - update required : 0\n"
    "  BuildID : 12345\n"
    " disk usage : 10.5 GB\n"
)

# Full VDF output with depots > branches > public/unstable buildids.
STEAMCMD_OUTPUT_99999 = (
    '"380870"\n'
    '{\n'
    '\t"common"\n'
    '\t{\n'
    '\t\t"name"\t\t"Project Zomboid Dedicated Server"\n'
    '\t}\n'
    '\t"depots"\n'
    '\t{\n'
    '\t\t"branches"\n'
    '\t\t{\n'
    '\t\t\t"public"\n'
    '\t\t\t{\n'
    '\t\t\t\t"buildid"\t\t"99999"\n'
    '\t\t\t}\n'
    '\t\t\t"unstable"\n'
    '\t\t\t{\n'
    '\t\t\t\t"buildid"\t\t"88888"\n'
    '\t\t\t}\n'
    '\t\t}\n'
    '\t}\n'
    '}\n'
)
STEAMCMD_OUTPUT_NO_BUILDID = "Some SteamCMD output without the key we need.\n"


# ---------------------------------------------------------------------------
# ServerUpdateChecker.get_installed_buildid
# ---------------------------------------------------------------------------

class TestGetInstalledBuildid(unittest.TestCase):

    def _checker(self, **kwargs):
        return ServerUpdateChecker(_make_config(**kwargs))

    def _fake_run(self, stdout="", returncode=0):
        result = MagicMock()
        result.stdout = stdout
        result.stderr = ""
        result.returncode = returncode
        return result

    def test_returns_buildid_from_app_status(self):
        checker = self._checker()
        with patch("subprocess.run", return_value=self._fake_run(APP_STATUS_OUTPUT_12345)):
            result = checker.get_installed_buildid()
        self.assertEqual(result, "12345")

    def test_returns_none_when_no_buildid_in_output(self):
        checker = self._checker()
        with patch("subprocess.run", return_value=self._fake_run(STEAMCMD_OUTPUT_NO_BUILDID)):
            result = checker.get_installed_buildid()
        self.assertIsNone(result)

    def test_returns_none_on_timeout(self):
        checker = self._checker()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="steamcmd", timeout=30)):
            result = checker.get_installed_buildid()
        self.assertIsNone(result)

    def test_returns_none_on_exception(self):
        checker = self._checker()
        with patch("subprocess.run", side_effect=FileNotFoundError("steamcmd not found")):
            result = checker.get_installed_buildid()
        self.assertIsNone(result)

    def test_returns_none_when_steamcmd_path_missing(self):
        checker = self._checker(steamcmd_path="")
        result = checker.get_installed_buildid()
        self.assertIsNone(result)

    def test_returns_none_when_server_dir_missing(self):
        checker = self._checker(server_dir="")
        result = checker.get_installed_buildid()
        self.assertIsNone(result)

    def test_uses_server_dir_in_force_install_dir_arg(self):
        checker = self._checker(server_dir=r"D:\my_server")
        captured = []

        def fake_run(args, **kwargs):
            captured.extend(args)
            raise subprocess.TimeoutExpired(cmd="steamcmd", timeout=30)

        with patch("subprocess.run", side_effect=fake_run):
            checker.get_installed_buildid()

        self.assertIn("+force_install_dir", captured)
        norm = os.path.normpath(r"D:\my_server")
        self.assertIn(norm, captured)


# ---------------------------------------------------------------------------
# ServerUpdateChecker._parse_branch_buildid
# ---------------------------------------------------------------------------

class TestParseBranchBuildid(unittest.TestCase):

    def test_returns_buildid_for_public_branch(self):
        result = ServerUpdateChecker._parse_branch_buildid(STEAMCMD_OUTPUT_99999, "public")
        self.assertEqual(result, "99999")

    def test_returns_buildid_for_unstable_branch(self):
        result = ServerUpdateChecker._parse_branch_buildid(STEAMCMD_OUTPUT_99999, "unstable")
        self.assertEqual(result, "88888")

    def test_returns_none_when_branch_not_found(self):
        result = ServerUpdateChecker._parse_branch_buildid(STEAMCMD_OUTPUT_99999, "outdatedunstable")
        self.assertIsNone(result)

    def test_returns_none_when_depots_section_absent(self):
        text = '"380870"\n{\n\t"common"\n\t{\n\t\t"name"\t\t"PZ Server"\n\t}\n}\n'
        result = ServerUpdateChecker._parse_branch_buildid(text, "public")
        self.assertIsNone(result)

    def test_returns_none_on_empty_string(self):
        result = ServerUpdateChecker._parse_branch_buildid("", "public")
        self.assertIsNone(result)

    def test_handles_steamcmd_preamble_before_app_block(self):
        text = (
            "Steam>Logging in user 'anonymous' to Steam Public...\n"
            "Logged in OK\n"
            + STEAMCMD_OUTPUT_99999
        )
        result = ServerUpdateChecker._parse_branch_buildid(text, "public")
        self.assertEqual(result, "99999")


# ---------------------------------------------------------------------------
# ServerUpdateChecker.get_remote_buildid
# ---------------------------------------------------------------------------

class TestGetRemoteBuildid(unittest.TestCase):

    def _checker(self, **kwargs):
        return ServerUpdateChecker(_make_config(**kwargs))

    def _fake_run(self, stdout="", returncode=0):
        result = MagicMock()
        result.stdout = stdout
        result.stderr = ""
        result.returncode = returncode
        return result

    def test_returns_buildid_on_success(self):
        checker = self._checker()
        with patch("subprocess.run", return_value=self._fake_run(STEAMCMD_OUTPUT_99999)):
            result = checker.get_remote_buildid()
        self.assertEqual(result, "99999")

    def test_resets_failure_counter_on_success(self):
        checker = self._checker()
        checker._consecutive_failures = 3
        with patch("subprocess.run", return_value=self._fake_run(STEAMCMD_OUTPUT_99999)):
            checker.get_remote_buildid()
        self.assertEqual(checker._consecutive_failures, 0)

    def test_returns_none_when_no_buildid_in_output(self):
        checker = self._checker()
        with patch("subprocess.run", return_value=self._fake_run(STEAMCMD_OUTPUT_NO_BUILDID)):
            result = checker.get_remote_buildid()
        self.assertIsNone(result)

    def test_increments_failure_counter_on_no_match(self):
        checker = self._checker()
        with patch("subprocess.run", return_value=self._fake_run(STEAMCMD_OUTPUT_NO_BUILDID)):
            checker.get_remote_buildid()
        self.assertEqual(checker._consecutive_failures, 1)

    def test_returns_none_on_timeout(self):
        checker = self._checker()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="steamcmd", timeout=30)):
            result = checker.get_remote_buildid()
        self.assertIsNone(result)

    def test_increments_failure_counter_on_timeout(self):
        checker = self._checker()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="steamcmd", timeout=30)):
            checker.get_remote_buildid()
        self.assertEqual(checker._consecutive_failures, 1)

    def test_returns_none_when_steamcmd_path_empty(self):
        cfg = _make_config()
        cfg.steamcmd_path = ""
        checker = ServerUpdateChecker(cfg)
        result = checker.get_remote_buildid()
        self.assertIsNone(result)

    def test_escalates_warning_after_max_failures(self):
        checker = self._checker()
        checker._consecutive_failures = checker._MAX_FAILURES - 1
        with patch("subprocess.run", return_value=self._fake_run(STEAMCMD_OUTPUT_NO_BUILDID)):
            with self.assertLogs("backend", level="WARNING") as cm:
                checker.get_remote_buildid()
        self.assertTrue(any("consecutive" in line for line in cm.output))

    def test_no_warning_below_max_failures(self):
        checker = self._checker()
        checker._consecutive_failures = 0
        with patch("subprocess.run", return_value=self._fake_run(STEAMCMD_OUTPUT_NO_BUILDID)):
            # Should not log a WARNING — just increment counter
            import logging
            with self.assertRaises(AssertionError):
                with self.assertLogs("backend", level="WARNING"):
                    checker.get_remote_buildid()

    def test_buildid_found_mid_output(self):
        output = (
            "Steam>Logging in user 'anonymous' to Steam Public...\n"
            "Logged in OK\n"
            '"380870"\n'
            '{\n'
            '\t"depots"\n'
            '\t{\n'
            '\t\t"branches"\n'
            '\t\t{\n'
            '\t\t\t"public"\n'
            '\t\t\t{\n'
            '\t\t\t\t"buildid"\t\t"55555"\n'
            '\t\t\t}\n'
            '\t\t}\n'
            '\t}\n'
            '}\n'
        )
        checker = self._checker()
        with patch("subprocess.run", return_value=self._fake_run(output)):
            result = checker.get_remote_buildid()
        self.assertEqual(result, "55555")

    def test_returns_buildid_for_unstable_branch(self):
        cfg = _make_config()
        cfg.steam_branch = "unstable"
        checker = ServerUpdateChecker(cfg)
        with patch("subprocess.run", return_value=self._fake_run(STEAMCMD_OUTPUT_99999)):
            result = checker.get_remote_buildid()
        self.assertEqual(result, "88888")


# ---------------------------------------------------------------------------
# ServerUpdateChecker.update_needed
# ---------------------------------------------------------------------------

class TestUpdateNeeded(unittest.TestCase):

    def _checker(self):
        return ServerUpdateChecker(_make_config())

    def test_returns_true_when_buildids_differ(self):
        checker = self._checker()
        with patch.object(checker, "get_installed_buildid", return_value="12345"), \
             patch.object(checker, "get_remote_buildid", return_value="99999"):
            self.assertTrue(checker.update_needed())

    def test_returns_false_when_buildids_match(self):
        checker = self._checker()
        with patch.object(checker, "get_installed_buildid", return_value="12345"), \
             patch.object(checker, "get_remote_buildid", return_value="12345"):
            self.assertFalse(checker.update_needed())

    def test_returns_none_when_installed_is_none(self):
        checker = self._checker()
        with patch.object(checker, "get_installed_buildid", return_value=None), \
             patch.object(checker, "get_remote_buildid", return_value="99999"):
            self.assertIsNone(checker.update_needed())

    def test_returns_none_when_remote_is_none(self):
        checker = self._checker()
        with patch.object(checker, "get_installed_buildid", return_value="12345"), \
             patch.object(checker, "get_remote_buildid", return_value=None):
            self.assertIsNone(checker.update_needed())

    def test_returns_none_when_both_are_none(self):
        checker = self._checker()
        with patch.object(checker, "get_installed_buildid", return_value=None), \
             patch.object(checker, "get_remote_buildid", return_value=None):
            self.assertIsNone(checker.update_needed())


# ---------------------------------------------------------------------------
# ServerUpdateChecker.run_update
# ---------------------------------------------------------------------------

class TestRunUpdate(unittest.TestCase):

    def _checker(self, **kwargs):
        return ServerUpdateChecker(_make_config(**kwargs))

    def _fake_run(self, returncode=0):
        result = MagicMock()
        result.returncode = returncode
        result.stdout = "Success: App '380870' fully installed.\n"
        result.stderr = ""
        return result

    def test_returns_true_on_exit_code_zero(self):
        checker = self._checker()
        with patch("subprocess.run", return_value=self._fake_run(0)):
            self.assertTrue(checker.run_update())

    def test_returns_false_on_nonzero_exit_code(self):
        checker = self._checker()
        with patch("subprocess.run", return_value=self._fake_run(1)):
            self.assertFalse(checker.run_update())

    def test_returns_false_on_timeout(self):
        checker = self._checker()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="steamcmd", timeout=600)):
            self.assertFalse(checker.run_update())

    def test_uses_normpath_for_force_install_dir(self):
        checker = self._checker(server_dir="C:/pz/server")
        captured_args = []

        def fake_run(args, **kwargs):
            captured_args.extend(args)
            return self._fake_run(0)

        with patch("subprocess.run", side_effect=fake_run):
            checker.run_update()

        # normpath should have been applied — Windows uses backslashes
        force_install_idx = captured_args.index("+force_install_dir")
        install_dir = captured_args[force_install_idx + 1]
        self.assertEqual(install_dir, os.path.normpath("C:/pz/server"))

    def test_includes_validate_in_command(self):
        checker = self._checker()
        captured_args = []

        def fake_run(args, **kwargs):
            captured_args.extend(args)
            return self._fake_run(0)

        with patch("subprocess.run", side_effect=fake_run):
            checker.run_update()

        self.assertTrue(any("validate" in str(a) for a in captured_args))

    def test_uses_configured_timeout(self):
        checker = self._checker(steamcmd_timeout=120)
        captured_kwargs = {}

        def fake_run(args, **kwargs):
            captured_kwargs.update(kwargs)
            return self._fake_run(0)

        with patch("subprocess.run", side_effect=fake_run):
            checker.run_update()

        self.assertEqual(captured_kwargs.get("timeout"), 120)

    def test_non_public_branch_adds_beta_flag(self):
        cfg = _make_config()
        cfg.steam_branch = "unstable"
        checker = ServerUpdateChecker(cfg)
        captured_args = []

        def fake_run(args, **kwargs):
            captured_args.extend(args)
            return self._fake_run(0)

        with patch("subprocess.run", side_effect=fake_run):
            checker.run_update()

        self.assertIn("-beta", captured_args)
        beta_idx = captured_args.index("-beta")
        self.assertEqual(captured_args[beta_idx + 1], "unstable")

    def test_public_branch_omits_beta_flag(self):
        checker = self._checker()  # steam_branch defaults to "public"
        captured_args = []

        def fake_run(args, **kwargs):
            captured_args.extend(args)
            return self._fake_run(0)

        with patch("subprocess.run", side_effect=fake_run):
            checker.run_update()

        self.assertNotIn("-beta", captured_args)


# ---------------------------------------------------------------------------
# AppConfig persistence — new fields
# ---------------------------------------------------------------------------

class TestAppConfigPersistence(unittest.TestCase):

    def test_new_fields_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from pathlib import Path
            config_file = Path(tmpdir) / "pz_server_config.ini"

            cfg = AppConfig()
            cfg.steamcmd_path = r"C:\steamcmd\steamcmd.exe"
            cfg.server_update_interval = 120
            cfg.steamcmd_timeout = 300
            cfg.last_server_update_check = "2026-03-20 15:00:00"

            import backend
            _no_legacy = Path(tmpdir) / "nonexistent_legacy.ini"
            with patch.object(backend, "_config_path", return_value=config_file), \
                 patch.object(backend, "_legacy_config_path", return_value=_no_legacy), \
                 patch("backend.keyring.set_password"), \
                 patch("backend.keyring.get_password", return_value=""), \
                 patch("backend.keyring.delete_password"):
                cfg.save()

                loaded = AppConfig()
                loaded.load()

            self.assertEqual(loaded.steamcmd_path, r"C:\steamcmd\steamcmd.exe")
            self.assertEqual(loaded.server_update_interval, 120)
            self.assertEqual(loaded.steamcmd_timeout, 300)
            self.assertEqual(loaded.last_server_update_check, "2026-03-20 15:00:00")

    def test_steam_branch_round_trips(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from pathlib import Path
            config_file = Path(tmpdir) / "pz_server_config.ini"

            cfg = AppConfig()
            cfg.steam_branch = "unstable"

            import backend
            _no_legacy = Path(tmpdir) / "nonexistent_legacy.ini"
            with patch.object(backend, "_config_path", return_value=config_file), \
                 patch.object(backend, "_legacy_config_path", return_value=_no_legacy), \
                 patch("backend.keyring.set_password"), \
                 patch("backend.keyring.get_password", return_value=""), \
                 patch("backend.keyring.delete_password"):
                cfg.save()

                loaded = AppConfig()
                loaded.load()

            self.assertEqual(loaded.steam_branch, "unstable")

    def test_defaults_on_fresh_config(self):
        cfg = AppConfig()
        self.assertEqual(cfg.steamcmd_path, "")
        self.assertEqual(cfg.server_update_interval, 60)
        self.assertEqual(cfg.steamcmd_timeout, 600)
        self.assertEqual(cfg.last_server_update_check, "")
        self.assertEqual(cfg.steam_branch, "public")

    def test_invalid_integer_fields_fall_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from pathlib import Path
            config_file = Path(tmpdir) / "pz_server_config.ini"
            # Write a config with bad integer values
            parser = configparser.ConfigParser()
            parser["SETTINGS"] = {
                "server_update_interval": "not_a_number",
                "steamcmd_timeout": "also_bad",
            }
            with open(config_file, "w") as f:
                parser.write(f)

            import backend
            _no_legacy = Path(tmpdir) / "nonexistent_legacy.ini"
            with patch.object(backend, "_config_path", return_value=config_file), \
                 patch.object(backend, "_legacy_config_path", return_value=_no_legacy), \
                 patch("backend.keyring.get_password", return_value=""):
                cfg = AppConfig()
                cfg.load()

            self.assertEqual(cfg.server_update_interval, 60)
            self.assertEqual(cfg.steamcmd_timeout, 600)


# ---------------------------------------------------------------------------
# ServerManager — new security fixes
# ---------------------------------------------------------------------------

class TestServerManagerSecurity(unittest.TestCase):

    def _manager(self, **kwargs) -> "ServerManager":
        from backend import ServerManager
        cfg = AppConfig()
        cfg.rcon_path = kwargs.get("rcon_path", r"C:\rcon.exe")
        cfg.server_ip = kwargs.get("server_ip", "127.0.0.1:27015")
        cfg.password = kwargs.get("password", "secret")
        cfg.server_name = kwargs.get("server_name", "myserver")
        return ServerManager(cfg)

    def test_broadcast_escapes_double_quotes(self):
        """broadcast() must escape embedded double-quotes in the message."""
        from backend import ServerManager
        mgr = self._manager()
        captured = []
        with patch.object(ServerManager, "_rcon", side_effect=lambda cmd, **kw: captured.append(cmd)):
            mgr.broadcast('Hello "world"')
        self.assertEqual(captured[0], r'servermsg "Hello \"world\""')

    def test_rcon_uses_temp_config_file(self):
        """_rcon() must not pass the password as a command-line argument."""
        from backend import ServerManager
        mgr = self._manager()
        called_args = []
        fake_result = MagicMock()
        fake_result.stdout = ""
        with patch("subprocess.run", side_effect=lambda args, **kw: called_args.append(args) or fake_result):
            mgr._rcon("players")
        # Password must NOT appear anywhere in the subprocess args list
        self.assertTrue(len(called_args) > 0)
        flat_args = " ".join(str(a) for a in called_args[0])
        self.assertNotIn("secret", flat_args)
        # The --config flag must be present
        self.assertIn("--config", called_args[0])

    def test_is_running_escapes_single_quotes_in_server_name(self):
        """is_running() must escape single-quotes in server_name for PS -like filter."""
        from backend import ServerManager
        mgr = self._manager(server_name="it's my server")
        captured = []
        fake_result = MagicMock()
        fake_result.stdout = ""
        with patch("subprocess.run", side_effect=lambda cmd, **kw: captured.append(cmd) or fake_result):
            mgr.is_running()
        # The PowerShell command string should contain the escaped form ('' not ')
        self.assertIn("it''s my server", captured[0])


# ---------------------------------------------------------------------------
# AppConfig.save() — atomic write + lock
# ---------------------------------------------------------------------------

class TestAppConfigAtomicSave(unittest.TestCase):

    def test_save_is_atomic_no_partial_write(self):
        """save() writes to a temp file then os.replace — never a partial INI."""
        import backend
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "pz_server_config.ini"
            no_legacy = Path(tmpdir) / "nope.ini"

            cfg = AppConfig()
            cfg.server_dir = r"C:\pz"
            cfg.server_ip = "127.0.0.1:27015"

            with patch.object(backend, "_config_path", return_value=config_file), \
                 patch.object(backend, "_legacy_config_path", return_value=no_legacy), \
                 patch("backend.keyring.set_password"), \
                 patch("backend.keyring.delete_password"):
                cfg.save()

            self.assertTrue(config_file.exists())
            # No .tmp files left over
            tmp_files = list(Path(tmpdir).glob("*.tmp"))
            self.assertEqual(len(tmp_files), 0)


# ---------------------------------------------------------------------------
# LogParser.find_latest_log
# ---------------------------------------------------------------------------

class TestFindLatestLog(unittest.TestCase):

    def test_finds_log_in_top_level_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log = os.path.join(tmpdir, "DebugLog-server.txt")
            open(log, "w").close()
            result = LogParser(tmpdir).find_latest_log()
            self.assertEqual(os.path.abspath(result), os.path.abspath(log))

    def test_finds_log_in_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = os.path.join(tmpdir, "2024-01-15_14-00")
            os.makedirs(subdir)
            log = os.path.join(subdir, "DebugLog-server.txt")
            open(log, "w").close()
            result = LogParser(tmpdir).find_latest_log()
            self.assertEqual(os.path.abspath(result), os.path.abspath(log))

    def test_returns_most_recent_when_multiple_logs_exist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_dir = os.path.join(tmpdir, "2024-01-14_12-00")
            new_dir = os.path.join(tmpdir, "2024-01-15_14-00")
            os.makedirs(old_dir)
            os.makedirs(new_dir)
            old_log = os.path.join(old_dir, "DebugLog-server.txt")
            new_log = os.path.join(new_dir, "DebugLog-server.txt")
            open(old_log, "w").close()
            import time as _time
            _time.sleep(0.01)
            open(new_log, "w").close()
            result = LogParser(tmpdir).find_latest_log()
            self.assertEqual(os.path.abspath(result), os.path.abspath(new_log))

    def test_returns_none_when_no_logs_exist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = LogParser(tmpdir).find_latest_log()
            self.assertIsNone(result)

    def test_ignores_non_matching_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "server.log"), "w").close()
            open(os.path.join(tmpdir, "DebugLog-client.txt"), "w").close()
            result = LogParser(tmpdir).find_latest_log()
            self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Poll-guard logic tests  (no Qt import — uses a lightweight stub)
# ---------------------------------------------------------------------------

class _FakeManager:
    """Minimal stub for testing poll-guard logic without importing Qt."""

    def __init__(self, server_dir="", rcon_path="", server_ip="", password="",
                 server_running=None):
        cfg = AppConfig()
        cfg.server_dir = server_dir
        cfg.rcon_path = rcon_path
        cfg.server_ip = server_ip
        cfg.password = password
        self.config = cfg
        self._server_running = server_running
        self._poll_status_job = None
        self._sched_job = None
        self._started = []
        self._log_messages = []
        self._last_sched_restart_date = None
        self._countdown_active = False
        self._restart_called = False

    # --- methods under test (inlined from gui.py to avoid Qt import) ---

    def _config_is_ready(self):
        c = self.config
        return bool(c.server_dir and c.rcon_path and c.server_ip and c.password)

    def _start_status_poll(self):
        self._started.append("status")
        self._poll_status_job = object()

    def _start_sched_poll(self):
        self._started.append("sched")
        self._sched_job = object()

    def _log(self, msg):
        self._log_messages.append(msg)

    def _restart_server(self):
        self._restart_called = True

    def _set_status(self, running: bool):
        """Mirrors gui.py PZManager._set_status poll-guard logic."""
        self._server_running = running
        if running and self._config_is_ready():
            if self._poll_status_job is None:
                self._start_status_poll()
            if self._sched_job is None:
                self._start_sched_poll()

    def _save_config_poll_guard(self):
        """Mirrors the poll-start block added to gui.py _save_config."""
        if self._config_is_ready():
            if self._server_running:
                if self._poll_status_job is None:
                    self._start_status_poll()
                if self._sched_job is None:
                    self._start_sched_poll()

    def _check_scheduled_restart_guard(self, server_running):
        """Mirrors the new running-guard in gui.py _check_scheduled_restart."""
        self._server_running = server_running
        if not self._server_running:
            self._log("Scheduled restart skipped — server not running.")
            return
        if self._countdown_active:
            self._log("Scheduled restart deferred — countdown already active.")
        else:
            self._restart_server()


def _ready_manager(**kwargs):
    """Return a _FakeManager with all required config fields set."""
    return _FakeManager(
        server_dir=r"C:\pz_server",
        rcon_path=r"C:\rcon.exe",
        server_ip="127.0.0.1",
        password="secret",
        **kwargs,
    )


class TestPollGuard(unittest.TestCase):
    """Tests for the poll-start guard logic in _set_status and _save_config."""

    # --- _set_status paths ---

    def test_set_status_starts_both_polls_when_running_and_config_ready(self):
        m = _ready_manager()
        m._set_status(True)
        self.assertIn("status", m._started)
        self.assertIn("sched", m._started)
        self.assertIsNotNone(m._poll_status_job)
        self.assertIsNotNone(m._sched_job)

    def test_set_status_skips_polls_when_server_stopped(self):
        m = _ready_manager()
        m._set_status(False)
        self.assertEqual(m._started, [])
        self.assertIsNone(m._poll_status_job)
        self.assertIsNone(m._sched_job)

    def test_set_status_skips_polls_when_config_not_ready(self):
        m = _FakeManager()  # no config fields set
        m._set_status(True)
        self.assertEqual(m._started, [])

    def test_set_status_double_start_guard_status_poll(self):
        m = _ready_manager()
        sentinel = object()
        m._poll_status_job = sentinel  # already started
        m._set_status(True)
        # status poll must not be re-created
        self.assertIs(m._poll_status_job, sentinel)
        self.assertNotIn("status", m._started)

    def test_set_status_double_start_guard_sched_poll(self):
        m = _ready_manager()
        sentinel = object()
        m._sched_job = sentinel  # already started
        m._set_status(True)
        self.assertIs(m._sched_job, sentinel)
        self.assertNotIn("sched", m._started)

    def test_set_status_second_call_does_not_double_start(self):
        m = _ready_manager()
        m._set_status(True)
        count_after_first = len(m._started)
        m._set_status(True)
        self.assertEqual(len(m._started), count_after_first)

    # --- _save_config poll-guard paths ---

    def test_save_config_starts_polls_when_running_and_config_valid(self):
        m = _ready_manager(server_running=True)
        m._save_config_poll_guard()
        self.assertIn("status", m._started)
        self.assertIn("sched", m._started)

    def test_save_config_skips_polls_when_server_stopped(self):
        m = _ready_manager(server_running=False)
        m._save_config_poll_guard()
        self.assertEqual(m._started, [])

    def test_save_config_skips_polls_when_server_unknown(self):
        m = _ready_manager(server_running=None)
        m._save_config_poll_guard()
        self.assertEqual(m._started, [])

    def test_save_config_skips_polls_when_config_not_ready(self):
        m = _FakeManager(server_running=True)  # no config fields
        m._save_config_poll_guard()
        self.assertEqual(m._started, [])

    def test_save_config_skips_if_polls_already_running(self):
        m = _ready_manager(server_running=True)
        m._poll_status_job = object()
        m._sched_job = object()
        m._save_config_poll_guard()
        self.assertEqual(m._started, [])


class TestSchedRestartGuard(unittest.TestCase):
    """Tests for the server-running guard in _check_scheduled_restart."""

    def test_skips_restart_when_server_not_running(self):
        m = _ready_manager()
        m._check_scheduled_restart_guard(server_running=False)
        self.assertFalse(m._restart_called)
        self.assertTrue(any("not running" in msg for msg in m._log_messages))

    def test_runs_restart_when_server_running(self):
        m = _ready_manager()
        m._check_scheduled_restart_guard(server_running=True)
        self.assertTrue(m._restart_called)

    def test_defers_restart_when_countdown_active(self):
        m = _ready_manager()
        m._countdown_active = True
        m._check_scheduled_restart_guard(server_running=True)
        self.assertFalse(m._restart_called)
        self.assertTrue(any("deferred" in msg for msg in m._log_messages))


if __name__ == "__main__":
    unittest.main()
