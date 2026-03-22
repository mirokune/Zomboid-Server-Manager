# Changelog

All notable changes are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## Unreleased

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
- Three `QTimer.singleShot` calls inside daemon threads are now wrapped in `_invoke()` so they execute on the Qt main thread
- Log file is now written to `%APPDATA%\PZServerManager\pz_manager.log` instead of the current working directory

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
