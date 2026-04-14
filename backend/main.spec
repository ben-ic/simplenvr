# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the SimpleNVR backend.

CRITICAL: zeep + onvif use runtime file discovery for WSDL/XSD files,
which PyInstaller does not detect via static analysis. Without the
explicit collect_data_files() calls below, ONVIF discovery silently
fails in the bundled binary at runtime. See plan §20 (risk #1).
"""

import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# --- Data files (WSDL/XSD) ---------------------------------------------------
# onvif-zeep-async ships its WSDL bundle under the package name `onvif`.
zeep_datas = collect_data_files("zeep")
onvif_datas = collect_data_files("onvif")

# --- Classification models ---------------------------------------------------
# D-FINE-N (detection, Apache-2.0, 15.3 MB) + YAMNet (audio, Apache-2.0,
# 15 MB) ONNX weights ship committed in-tree at
# backend/classification/models/. PyInstaller copies them into the
# bundle so the sidecar is fully self-contained: no post-install
# downloads, no first-run network dependency.
#
# Fail loudly if the weights aren't present at build time — shipping
# without the detection model would produce a sidecar whose motion
# pipeline silently never fires events. `scripts/fetch_dfine.{sh,ps1}`
# verifies the SHA256 of the committed file and can be run before
# PyInstaller; the actual model bytes live in the git tree.
_classifier_models_dir = os.path.join(
    os.path.dirname(os.path.abspath(SPEC)),  # type: ignore[name-defined]
    "classification",
    "models",
)
_required_models = ["dfine_n.onnx", "yamnet.onnx", "yamnet_classes.txt", "NOTICE.txt"]
classifier_datas = []
for _m in _required_models:
    _full = os.path.join(_classifier_models_dir, _m)
    if not os.path.exists(_full):
        raise SystemExit(
            f"main.spec: required classifier model not found: {_full}\n"
            f"Run scripts/fetch_dfine.sh (and scripts/fetch_yamnet.sh) "
            f"to verify the in-tree weights, then retry."
        )
    # Destination inside the bundle matches the runtime import path used
    # by backend.classification.capability_probe._bundled_model_dir().
    classifier_datas.append((_full, "backend/classification/models"))

datas = [*zeep_datas, *onvif_datas, *classifier_datas]

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
    "backend.discovery.auth_backoff",
    "backend.discovery.mac_lookup",
    "backend.motion.detector",
    "backend.motion.manager",
    "backend.motion.tracker",
    "backend.motion.confidence",
    "backend.motion.heatmap",
    "backend.detect_frames",
    "backend.detect_frames.ffmpeg_source",
    "backend.detect_frames.shm_ring",
    "backend.classification",
    "backend.classification.capability_probe",
    "backend.classification.labelmap",
    "backend.classification.dfine",
    "backend.recording.camera_recorder",
    "backend.recording.codec",
    "backend.recording.manager",
    "backend.recording.frame_broadcaster",
    "backend.recording.janitor",
    "backend.recording.storage",
    "backend.ffmpeg_path",
    "backend.port_finder",
    "backend.process_cleanup",
    "backend.go2rtc_client",
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
    excludes=[
        "torch", "torchvision",
        "tensorflow", "tensorflow_hub", "tf_keras", "keras",
        "h5py", "transformers", "tokenizers",
        "grpc", "grpcio", "tensorboard",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# Onedir mode: produces a directory with the executable + all deps laid
# out on disk. No temp-directory extraction on launch, so cold start
# drops from ~2-10s (onefile) to near-instant. The directory is bundled
# into the Tauri app via the `resources` config instead of `externalBin`.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="simplenvr-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="simplenvr-backend",
)
