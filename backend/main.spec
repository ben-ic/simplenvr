# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the SimpleNVR backend.

CRITICAL: zeep + onvif use runtime file discovery for WSDL/XSD files,
which PyInstaller does not detect via static analysis. Without the
explicit collect_data_files() calls below, ONVIF discovery silently
fails in the bundled binary at runtime. See plan §20 (risk #1).
"""

import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# --- Data files (WSDL/XSD) ---------------------------------------------------
# onvif-zeep-async ships its WSDL bundle under the package name `onvif`.
zeep_datas = collect_data_files("zeep")
onvif_datas = collect_data_files("onvif")

datas = [*zeep_datas, *onvif_datas]

# --- Hidden imports ----------------------------------------------------------
hiddenimports = [
    "zeep",
    "zeep.wsdl",
    "zeep.transports",
    "zeep.cache",
    "zeep.helpers",
    "lxml.etree",
    "lxml._elementpath",
    *collect_submodules("onvif"),
    *collect_submodules("zeep"),
    # backend package (string-imported by uvicorn as "backend.main:app")
    "backend",
    "backend.main",
    "backend.api.streams",
    "backend.api.cameras",
    "backend.api.recordings",
    "backend.api.motion",
    "backend.api.settings",
    "backend.api.ws",
    "backend.discovery.scanner",
    "backend.discovery.ws_discovery",
    "backend.discovery.onvif_client",
    "backend.discovery.rtsp_probe",
    "backend.motion.detector",
    "backend.motion.manager",
    "backend.recording.camera_recorder",
    "backend.recording.codec",
    "backend.recording.manager",
    "backend.ffmpeg_path",
    "backend.port_finder",
    "backend.config",
    "backend.db",
    "backend.models",
    # uvicorn string-import resolution
    "uvicorn",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
]

# uvloop is Unix-only; on Windows uvicorn.loops.auto handles fallback.
if sys.platform != "win32":
    hiddenimports.append("uvicorn.loops.uvloop")

a = Analysis(
    ["_pyi_entry.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# Onefile mode: produces a single executable for simplicity and to match
# Tauri externalBin's expectation of a single triple-suffixed file. The
# ~2s extraction startup is acceptable; Phase 9's health-check loop must
# tolerate it.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="simplenvr-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
