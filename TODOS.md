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
