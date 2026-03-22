# TODOS

## Pending

### Skip-validate toggle for SteamCMD updates
**What:** Add a "Skip +validate" checkbox in the Settings tab for the server update pipeline.
**Why:** `+validate` re-verifies all game files on every update, adding 2–5 minutes on fast connections. Admins who prioritize speed over correctness should be able to disable it.
**Pros:** Faster updates for admins with reliable connections/storage.
**Cons:** Risk of partial downloads going undetected; +validate is the safe default.
**Context:** `run_update()` currently hardcodes `+app_update 380870 validate +quit`. The fix is a `config.steamcmd_skip_validate: bool = False` field and a conditional in `run_update()`. The Settings checkbox mirrors the pattern of other boolean settings.
**Depends on:** Server update pipeline (this PR) must be merged first.

### Auto-restart on update toggle (both pipelines)
**What:** Add a per-pipeline "auto-restart when update detected" checkbox in Settings. When disabled, the pipeline detects the update and shows a notification but does not start the countdown.
**Why:** Admins running scheduled events or peak-hour sessions may want to defer an update rather than having it fire automatically.
**Pros:** Gives admins control without losing visibility.
**Cons:** Adds a config field + conditional per pipeline. Slightly more complex UI.
**Context:** Both the mod update pipeline and the server update pipeline currently auto-trigger the countdown unconditionally. The toggle would gate the `_on_mods_need_update()` / `_on_server_update_needed()` calls. If disabled, log "Update available — auto-restart is off. Click Restart Server to apply."
**Depends on:** Server update pipeline (this PR) must be merged first.

### GUI test suite (pytest-qt)
**What:** Write `test_gui.py` covering the 10 new codepaths introduced by the PyQt6 rewrite.
**Why:** The GUI rewrite introduces new behavior (LogViewer coloring, RCON history, settings dirty state, button disable/re-enable, LogBridge threading) that cannot be verified by the existing `test_backend.py`. Without tests, regressions in these paths are silent until a user reports them.
**Pros:** Automated regression coverage for the new GUI layer; makes future refactors safe.
**Cons:** Requires `pytest-qt` in dev dependencies; tests need a display (or `QT_QPA_PLATFORM=offscreen` on headless CI).
**Context:** Full test plan is at `~/.gstack/projects/Zomboid-Server-Manager/root-master-test-plan-20260321-002411.md`. Add `pytest-qt` to `requirements.txt` (dev section). Tests cover: LogViewer color/timestamp/scroll/max-blocks, RCON history Up/Down/empty/max-50, settings dirty state, button disabled on async, LogBridge cross-thread signal. Also cover the startup guard paths added in the mod-check guard fix: (1) RCON not configured → log + reschedule, (2) `_server_running is None` → retry in 30s, (3) server stopped → log + reschedule.
**Depends on:** PyQt6 GUI rewrite must be complete first.

### System tray fallback
**What:** Handle `QSystemTrayIcon.isSystemTrayAvailable()` returning False gracefully.
**Why:** If the system tray is unavailable, `closeEvent` hides the window with no way to restore it — the user loses access to the app without killing the process. This is a critical gap on headless environments or Linux without a desktop compositor.
**Pros:** Makes the app safe to use in non-standard environments; eliminates the "window disappeared" bug.
**Cons:** Small amount of additional logic in `closeEvent`; low priority since app is Windows-first.
**Context:** Fix is straightforward: `if not QSystemTrayIcon.isSystemTrayAvailable(): event.accept()` in `closeEvent`, i.e. fall back to normal close behavior. Also consider: if tray unavailable, don't add "hide to tray" behavior at all and show a warning in the log on startup.
**Depends on:** PyQt6 GUI rewrite must be complete first.
