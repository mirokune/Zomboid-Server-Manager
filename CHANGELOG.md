# Changelog

All notable changes are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## v0.1.2 — 2026-03-23

### Added
- **Steam Branch selector** in Settings — choose `public`, `unstable`, or `outdatedunstable`; the update checker now compares against the correct branch's remote build ID instead of always comparing against `public`
- Branch-aware `+app_update`: when a non-public branch is selected, SteamCMD is now invoked with `-beta <branch>` so updates install from the correct branch instead of silently downgrading to `public`

### Changed
- `get_installed_buildid()` now calls SteamCMD `+app_status` instead of reading `appmanifest_380870.acf` from disk — reports the same build ID Steam uses, works regardless of install location, and guards against misidentifying unrelated `BuildID` tokens in SteamCMD preamble output
- VDF parsing in `get_remote_buildid()` now navigates `depots > branches > <branch> > buildid` rather than grabbing the first `buildid` in the `common` section

### Fixed
- Scheduled restart no longer fires when the server is already stopped (pre-existing bug)
- Status poll and scheduled-restart poll no longer start unconditionally at app startup — both now require config to be valid and the server to be running first, eliminating false-positive "server running" readings on unconfigured installs

## v0.1.1 — 2026-03-22

### Added
- **Cancel Restart** button in the countdown banner — admins can abort a pending restart and broadcast "Server restart has been cancelled." to players in-game

### Fixed
- Migration bug: RCON password was erased from Windows Credential Manager on first run after upgrading from a plaintext-config build
- `_rcon()`: RCON password is now passed via a temporary YAML config file instead of as a command-line argument, so it never appears in the process list
- `_rcon()`: removed `shell=True` — special characters in the server IP or password could break execution
- SteamCMD `+app_update` command was passed as a single string instead of three separate tokens; `validate` flag was never applied
- 11 lambda/exc closures in `gui.py` caused `NameError` when exceptions were reported on the Qt main thread after the except block exited
- Startup guards: mod check and server update check no longer fire before server status is known or config is set; both pipelines now start immediately after the first valid config save
- SteamCMD subprocess no longer opens a visible console window on startup (added `CREATE_NO_WINDOW` flag on Windows)
- PowerShell injection: single-quotes in `server_name` are now escaped before being interpolated into the `is_running()` WMI filter
- Double-quotes in broadcast messages are now escaped, preventing malformed RCON `servermsg` commands
- Config save is now atomic (write to `.tmp` + `os.replace`) and serialized with a lock — prevents partial writes or race conditions when mod and server update threads save simultaneously
- `_start_restart_countdown()` now guards against double-start; a second call while a countdown is running is ignored
- `_maybe_early_restart()` checks `_countdown_active` before proceeding and writes `_countdown_remaining` via `_invoke` (cross-thread write fix)
- `_check_scheduled_restart()` defers the restart if a countdown is already active
- Three `QTimer.singleShot` calls inside daemon threads are now wrapped in `_invoke()` so they execute on the Qt main thread; server update pipeline post-start timer now correctly routed via `_invoke`
- Log file is now written to `%APPDATA%\PZServerManager\pz_manager.log` instead of the current working directory
- `is_running()` now has a 10-second PowerShell timeout — server-control buttons can never be stuck indefinitely; thread-spawn failure now re-enables buttons immediately
- Log subdirectory search: `find_latest_log` now searches dated subfolders (PZ creates logs under `Logs/YYYY-MM-DD_HH-MM/`) — fixes "No log file found" on most setups
- Status check retry: `_check_server_status` now schedules a 15-second retry on failure so `_server_running` resolves faster, unblocking the mod-check pipeline; added in-flight guard to prevent concurrent checks from piling up under sustained WMI failure
- RCON settings fields now show placeholder text for server IP, server name, and password
- Zero-interval guard: `check_interval` and `server_update_interval` clamped to ≥ 1 min; `steamcmd_timeout` clamped to ≥ 30 s — prevents QTimer busy-loop if 0 is entered

## v0.1.0 — 2026-03-21

### Added
- PyQt6 GUI rewrite — dark theme, status indicator, activity log with color-coded
  timestamps, and a clean two-column Server Control layout
- **Server Control tab** — status badge (Running/Stopped/Checking), Start/Stop/Restart
  buttons with disabled states during async operations, mod update panel, server update
  panel, countdown banner with player count
- **RCON Console tab** — RCON command input with Up/Down arrow history (last 50 commands),
  Send button, Clear Output button
- **Server Log tab** — live log tail of `DebugLog-server*.txt`; shows actionable message
  when log directory is not configured
- **Settings tab** — grouped fields (Paths / Connection / Schedule & Timing), Browse
  buttons, unsaved-changes indicator, masked password field
- **Mod update pipeline** — polls server via RCON, parses debug log for mod status,
  triggers 5-minute countdown with in-game broadcasts at 5/2/1 minutes
- **Server update pipeline** — compares installed SteamCMD build ID to Steam depot;
  runs `+app_update` before restart when update available
- **Scheduled daily restart** — configurable HH:MM in Settings
- **System tray** — close button hides to tray; double-click to restore; right-click menu
- **Single-instance lock** — prevents duplicate app instances via socket mutex
- Windows DPI scaling support (125%/150% displays render crisply)
- Portable EXE and installer release artifacts via GitHub Actions
