# Contributing to PZ Server Manager

Thanks for your interest! This is a small community tool — contributions are welcome.

## Running from source

Requirements: Python 3.11+, Windows 10/11

```bat
pip install -r requirements.txt
python main.py
```

## Running tests

```bat
python -m pytest test_backend.py -v
```

## Building the EXE locally

Requirements: PyInstaller (installed automatically by the release workflow)

```bat
pip install pyinstaller
pyinstaller PZServerManager.spec
```

Output: `dist\PZServerManager.exe`

## Submitting a PR

1. Fork the repo and create a branch
2. Make your change
3. Run tests: `python -m pytest test_backend.py -v`
4. Update `CHANGELOG.md` — add your change under `## Unreleased`
5. Open a PR using the template

## Release process (maintainers only)

1. Edit `CHANGELOG.md` — move items from `Unreleased` to a new `## vX.Y.Z — YYYY-MM-DD` section
2. Commit: `git commit -m "chore: release vX.Y.Z"`
3. Tag: `git tag vX.Y.Z`
4. Push: `git push && git push --tags`

GitHub Actions will automatically:
- Build `dist/PZServerManager.exe` via PyInstaller
- Build `Output/PZServerManager-Setup.exe` via Inno Setup
- Create a GitHub Release with both artifacts attached

## Project structure

```
main.py             Entry point; single-instance socket lock; QApplication setup
backend.py          AppConfig, ServerManager, LogParser, LogTailer, ServerUpdateChecker
gui.py              PyQt6 UI — App (QMainWindow), LogViewer, LogBridge, RconLineEdit
test_backend.py     Unit tests for backend logic
PZServerManager.spec  PyInstaller build config
build.iss           Inno Setup installer script
requirements.txt    Runtime + dev dependencies (PyQt6, keyring, pytest-qt)
TODOS.md            Deferred features
```
