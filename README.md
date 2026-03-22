# PZ Server Manager

![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Latest Release](https://img.shields.io/github/v/release/mirokune/Zomboid-Server-Manager?label=release)

A Windows desktop app for managing a **Project Zomboid dedicated server**. Monitors for mod and game updates, warns players in-game before restarting, and gives you a live RCON console — all from a system tray icon.

<!-- Screenshots: add after first run -->
<!-- ![Server Control tab](docs/screenshots/server-control.png) -->

---

## Features

- **Mod update detection** — polls the server via RCON on a schedule, detects when mods need updating, warns players in-game at 5/2/1 minute intervals, then automatically restarts
- **Game update detection** — compares your installed SteamCMD build against the Steam depot; if a new version is out, warns players and runs `+app_update` before restarting
- **Scheduled daily restart** — set a daily restart time (HH:MM) in Settings
- **Live log viewer** — tails the latest `DebugLog-server*.txt` in real time; auto-switches when the server creates a new log file after restart
- **RCON console** — send any RCON command and see the response; Up/Down arrow keys cycle through the last 50 commands
- **System tray** — runs in the background; the close button hides the window rather than exiting

---

## Download

### Option 1: Installer (recommended for most users)

1. Go to the [**Releases page**](https://github.com/mirokune/Zomboid-Server-Manager/releases)
2. Download **PZServerManager-Setup.exe**
3. Run it — it installs the app and creates a Start Menu shortcut

> **Note:** If you install to `C:\Program Files\` and the app can't save its config,
> right-click the shortcut → *Run as Administrator* once, or use the portable EXE instead.

### Option 2: Portable EXE (no installation)

1. Download **PZServerManager.exe** from the [Releases page](https://github.com/mirokune/Zomboid-Server-Manager/releases)
2. Put it anywhere (Desktop, a dedicated server folder, etc.)
3. Double-click to run — your config is saved in the same folder

### Option 3: Run from source (developers)

Requires Python 3.11+.

```bat
pip install -r requirements.txt
python main.py
```

---

## First-time setup

1. Launch the app — it opens to the **Server Control** tab
2. Click the **Settings** tab
3. Fill in the fields (see [Configuration reference](#configuration-reference) below)
4. Click **Save Configuration**
5. Switch back to **Server Control** and click **Refresh** — the status should show Running or Stopped

---

## Configuration reference

| Field | Description |
|---|---|
| PZ Server Folder | Root directory of your PZ dedicated server (the folder that contains `StartServer64.bat`) |
| Zomboid Log Folder | Usually `C:\Users\YourName\Zomboid\Logs` — where the `DebugLog-server*.txt` files are written |
| RCON Executable | Full path to `rcon.exe` — download from [gorcon/rcon-cli](https://github.com/gorcon/rcon-cli/releases) |
| Server Name | The `-servername` value you use when starting your server (matches your server save folder name) |
| Server IP | `host:port` for RCON — usually `127.0.0.1:27015` |
| Password | Your RCON password (set in `servertest.ini` as `RCONPassword=`) |
| Check Interval (min) | How often to poll for mod updates (default: 60) |
| SteamCMD Path | Full path to `steamcmd.exe` — only needed for the server update pipeline |
| Server Update Interval (min) | How often to check for game updates (default: 60) |
| SteamCMD Timeout (sec) | Max seconds allowed for a SteamCMD update run (default: 600) |
| Scheduled Restart | `Disabled`, or pick a daily restart time in 24h format |

---

## How it works

### Mod update pipeline

```
Every N minutes
  └─ RCON: checkModsNeedUpdate
      └─ Parse DebugLog-server*.txt for result
          ├─ Up to date  → schedule next check
          └─ Needs update
              ├─ Broadcast: "Server restarting in 5 minutes for mod updates"
              ├─ Wait — check player count
              │   └─ 0 players → restart immediately
              ├─ Broadcast at 2 minutes and 1 minute
              └─ Stop → Start server
```

### Server update pipeline

```
Every M minutes
  └─ Read steamapps/appmanifest_380870.acf  (installed build ID)
  └─ steamcmd +app_info_print 380870        (remote build ID from Steam)
      ├─ Up to date  → schedule next check
      └─ Update available
          ├─ Broadcast: "Server restarting in 5 minutes for a game update"
          ├─ Wait — check player count / early restart if 0 players
          ├─ Broadcast at 2 minutes and 1 minute
          └─ Stop server
              └─ steamcmd +app_update 380870 +validate +quit
                  └─ Start server
                     (falls back to starting on old version if SteamCMD fails)
```

---

## FAQ

**Q: Does this work on Linux?**
A: No — the PZ dedicated server runs on Windows (`StartServer64.bat`), and `rcon.exe` is a Windows binary. This tool is Windows-only.

**Q: Do I need Python installed to use the EXE?**
A: No — the EXE bundles everything. Just download and run.

**Q: Where is my config saved?**
A: `%APPDATA%\PZServerManager\pz_server_config.ini` (e.g. `C:\Users\YourName\AppData\Roaming\PZServerManager\`). This location is always writable — no admin rights needed, regardless of where you installed the app. Your RCON password is stored separately in **Windows Credential Manager** and never written to the config file.

**Q: How do I view or remove the stored RCON password?**
A: Open **Credential Manager** in Windows (search "Credential Manager" in the Start menu), click **Windows Credentials**, and look for `PZServerManager`. You can edit or delete it there.

**Q: The app says "RCON connection failed" — what do I do?**
A: Check that: (1) the server is running, (2) RCON is enabled in your server config (`RCONPassword` and `RCONPort` are set), (3) the IP/port in Settings matches, and (4) the path to `rcon.exe` is correct.

**Q: The app is slow to open the first time — is that normal?**
A: Yes. The portable EXE extracts itself to a temp folder on first launch (this is normal for single-file Windows apps). Subsequent launches are faster once Windows caches the files. The installer version does not have this delay.

**Q: Can I use this without SteamCMD?**
A: Yes — SteamCMD is only needed for the server update pipeline. If you don't set a SteamCMD path, that pipeline is skipped; everything else works normally.

**Q: The countdown started but I need to cancel it — what do I do?**
A: Restart the app. There is no in-app cancel button yet (see [TODOS.md](TODOS.md)).

---

## Running tests (developers)

```bat
python -m pytest test_backend.py -v
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to run from source, build the EXE locally, and submit a PR.

## License

MIT — see [LICENSE](LICENSE).
