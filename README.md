# Project Zomboid Server Manager

A Windows desktop app for managing a Project Zomboid dedicated server. Handles mod update detection, scheduled restarts, SteamCMD game updates, live log tailing, and RCON — all from a system tray icon.

## Features

- **Mod update pipeline** — polls the server via RCON, parses the debug log for mod status, and automatically triggers a 5-minute player countdown + restart when mods need updating
- **Server update pipeline** — compares the installed SteamCMD build ID against the Steam depot on a configurable schedule; when an update is available, warns players and runs `steamcmd +app_update` before restarting
- **Scheduled daily restart** — configurable time (HH:MM) for a daily restart
- **Live log viewer** — tails the latest `DebugLog-server*.txt` in a dedicated tab, auto-switches when a new log file appears after a restart
- **RCON console** — send arbitrary RCON commands and see responses
- **System tray** — minimize to tray; close button hides the window rather than exiting

## Requirements

- Windows 10/11
- Python 3.11+
- [RCON CLI](https://github.com/gorcon/rcon-cli) (`rcon.exe`) for server communication
- [SteamCMD](https://developer.valvesoftware.com/wiki/SteamCMD) for server updates (optional — only needed for the server update pipeline)

## Installation

```bat
pip install -r requirements.txt
python main.py
```

## Configuration

All settings are saved to `pz_server_config.ini` next to `main.py`. Fill them in via the **Settings** tab:

| Field | Description |
|---|---|
| PZ Server Folder | Root directory of your PZ dedicated server (contains `StartServer64.bat`) |
| Zomboid Log Folder | `%USERPROFILE%\Zomboid\Logs` or your custom log path |
| RCON Executable | Path to `rcon.exe` |
| Server Name | The `-servername` value used when starting the server |
| Server IP | `host:port` for RCON (e.g. `127.0.0.1:27015`) |
| Password | RCON password |
| Check Interval (min) | How often to poll for mod updates (default: 60) |
| SteamCMD Path | Path to `steamcmd.exe` (required for server update pipeline) |
| Server Update Interval (min) | How often to check for game updates (default: 60) |
| SteamCMD Timeout (sec) | Max time allowed for a SteamCMD update run (default: 600) |
| Scheduled Restart | Daily restart time in 24h format, or Disabled |

## Mod update pipeline

```
Poll (every N min)
  └─ RCON: checkModsNeedUpdate
      └─ Parse DebugLog for result
          ├─ up to date  → schedule next check
          └─ needs update
              └─ 5-min countdown
                  ├─ Broadcast in-game at 5 / 2 / 1 min
                  ├─ Early restart if 0 players online
                  └─ Restart server
```

## Server update pipeline

```
Poll (every M min)
  └─ Read steamapps/appmanifest_380870.acf  (installed build ID)
  └─ steamcmd +app_info_print 380870        (remote build ID)
      ├─ up to date  → schedule next check
      └─ update available
          └─ 5-min countdown
              ├─ Broadcast in-game at 5 / 2 / 1 min
              ├─ Early restart if 0 players online
              └─ Stop server
                  └─ Poll until stopped (up to 30s)
                      └─ steamcmd +app_update 380870 validate
                          └─ Start server
                             (restarts on old version if SteamCMD fails)
```

## Running tests

```bat
python -m unittest test_backend -v
```

Tests cover `ServerUpdateChecker` (build ID parsing, failure escalation, update detection, `run_update` flags and timeout) and `AppConfig` persistence for the new fields.

## Project structure

```
main.py          Entry point; single-instance lock via socket
backend.py       AppConfig, ServerManager, LogParser, LogTailer, ServerUpdateChecker
gui.py           CustomTkinter UI (App class)
test_backend.py  Unit tests for backend logic
requirements.txt Python dependencies
TODOS.md         Deferred features
```
