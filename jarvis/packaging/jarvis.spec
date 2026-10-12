# PyInstaller build definition for the JARVIS Windows desktop application.
#
#   pyinstaller jarvis/packaging/jarvis.spec --noconfirm
#
# The result is a single self-contained executable in dist/JARVIS.exe that starts
# the executive runtime, the scheduler loop and the desktop interface.

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = (
    collect_submodules("jarvis")
    + ["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
       "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on"]
)

datas = collect_data_files("jarvis", subdir="interfaces/desktop/static") + [
    ("jarvis/interfaces/desktop/static", "jarvis/interfaces/desktop/static"),
]

a = Analysis(
    ["jarvis/interfaces/desktop/app.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JARVIS",
    debug=False,
    strip=False,
    upx=False,
    console=False,                 # windowed app; logs go to %APPDATA%/JARVIS/logs
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="JARVIS",
)
