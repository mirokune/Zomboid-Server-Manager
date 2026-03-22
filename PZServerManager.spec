# -*- mode: python ; coding: utf-8 -*-
# PZServerManager.spec
#
# Build with:  pyinstaller PZServerManager.spec
# Output:      dist/PZServerManager.exe  (single-file, no console window)
#
# collect_all('PyQt6') is required — it bundles the Qt platform plugin (qwindows.dll)
# and all necessary DLLs.  Without it the app crashes on launch with:
#   "This application failed to start because no Qt platform plugin could be initialized."

from PyInstaller.utils.hooks import collect_all

datas_qt, binaries_qt, hiddenimports_qt = collect_all('PyQt6')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries_qt,
    datas=datas_qt,
    hiddenimports=hiddenimports_qt + ['PyQt6.sip', 'keyring.backends.Windows'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'test_backend',
        'test_gui',
        'pytest',
        'pytest_qt',
        '_pytest',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PZServerManager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # upx=True causes AV false positives on PyQt6 apps
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # windowed — no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,          # add icon.ico here when available
)
