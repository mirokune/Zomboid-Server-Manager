# Changelog

All notable changes are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## Unreleased

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
