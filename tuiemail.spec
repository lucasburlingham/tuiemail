# PyInstaller spec for building a standalone console binary on both Linux and Windows.
# On Linux produced artifact: dist/tuiemail
# On Windows produced artifact: dist/tuiemail.exe

import sys

block_cipher = None

app_name = 'tuiemail'
if sys.platform == 'win32':
    app_name = 'tuiemail'


a = Analysis(
    ['tui_email.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=['curses', 'windows_curses'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=app_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=(sys.platform != 'win32'),
    upx=(sys.platform != 'win32'),
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)