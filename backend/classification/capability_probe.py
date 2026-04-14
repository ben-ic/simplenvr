"""Capability probe — hardware telemetry + D-FINE calibration.

Runs once on first launch (and on any hardware signature change) and
writes a small pile of observability settings that downstream code
reads without re-running any detection logic:

    classification_ep              : "cpu" (v1 ships CPU everywhere)
    classification_ram_mb          : int
    classification_cpu_count       : int
    classification_os              : darwin | linux | windows
    classification_arch            : arm64 | x64 | ...
    free_disk_mb                   : int
    disk_pressure                  : OK | LOW | CRITICAL
    classification_calibration_ms  : D-FINE warm median (empty on failure)
    capability_fingerprint         : short hash over static signature
    capability_calibrated_at       : ISO-8601 UTC
    capability_notes               : JSON list of human-readable trail

This is Detection Pipeline v2. YOLOX + tiering + Moondream are all
gone — we ship ONE model (D-FINE-N COCO) on CPU EP for every install.
The probe's remaining jobs are disk-pressure monitoring, hardware
telemetry, and a single D-FINE warm latency number that a future
rewrite will use to decide whether to switch the Snapdragon sidecar
onto QNN INT8.

Escape hatch (Ben-the-dev only):

    SIMPLENVR_CLASSIFIER=off  — skip the calibration benchmark. The
                                actual pipeline kill switch is read
                                by motion/manager.py; this probe just
                                records the decision.

If calibration fails (ORT missing, model missing, session creation
fails, inference fails) the probe records `calibration_ms=None`,
logs a warning, and proceeds. Calibration is telemetry, not a gate —
failure here does NOT disable detection.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import shutil
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .. import db
from ..config import DATA_DIR

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

DiskPressure = Literal["OK", "LOW", "CRITICAL"]

# Disk thresholds — unchanged from v1.
_DISK_OK_MB = 5_000
_DISK_CRITICAL_MB = 1_000


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class CapabilityReport:
    ep: Literal["cpu"]                    # v1 ships CPU everywhere; reserved for future QNN/CoreML routing
    ram_mb: int
    cpu_count: int
    os_name: str                          # "darwin" | "linux" | "windows"
    arch: str                             # "arm64" | "x64" | ...
    free_disk_mb: int
    disk_pressure: DiskPressure
    calibration_ms: float | None          # D-FINE warm median; None if calibration failed
    fingerprint: str                      # short sha256 over (os, arch, ram, cpu, ep, release)
    calibrated_at: str                    # ISO-8601 UTC
    notes: list[str]                      # human-readable trail

    def to_settings_dict(self) -> dict[str, str]:
        """Flatten to the string-keyed shape the settings table expects."""
        return {
            "classification_ep": self.ep,
            "classification_ram_mb": str(self.ram_mb),
            "classification_cpu_count": str(self.cpu_count),
            "classification_os": self.os_name,
            "classification_arch": self.arch,
            "free_disk_mb": str(self.free_disk_mb),
            "disk_pressure": self.disk_pressure,
            "classification_calibration_ms": (
                "" if self.calibration_ms is None else f"{self.calibration_ms:.2f}"
            ),
            "capability_fingerprint": self.fingerprint,
            "capability_calibrated_at": self.calibrated_at,
            "capability_notes": json.dumps(self.notes),
        }


# ---------------------------------------------------------------------------
# Static system info
# ---------------------------------------------------------------------------

def _detect_ram_mb() -> int:
    """Total physical RAM in MB across Windows/macOS/Linux.

    psutil is the preferred path and a declared backend dependency. The
    platform-specific fallbacks exist for dev installs where psutil
    failed to import, and for the theoretical case where psutil is
    present but its memory probe fails. Returns 0 if nothing works —
    non-fatal in v2 (no tier gate), but downstream telemetry will
    show a zero and Ben will know to investigate.
    """
    try:
        import psutil  # type: ignore
        mem = psutil.virtual_memory().total
        if mem > 0:
            return int(mem / (1024 * 1024))
    except Exception:
        pass

    if sys.platform == "darwin":
        try:
            import subprocess
            out = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], text=True, timeout=2
            )
            return int(int(out.strip()) / (1024 * 1024))
        except Exception:
            return 0

    if sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(int(line.split()[1]) / 1024)
        except Exception:
            return 0

    if sys.platform == "win32":
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return int(stat.ullTotalPhys / (1024 * 1024))
        except Exception:
            return 0

    return 0


def _detect_cpu_count() -> int:
    try:
        return os.cpu_count() or 1
    except Exception:
        return 1


def _detect_os() -> tuple[str, str]:
    """Returns (os_name, arch) in a normalized form."""
    system = platform.system().lower()  # darwin | linux | windows
    machine = platform.machine().lower()  # arm64 | x86_64 | aarch64 | amd64
    if machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("x86_64", "amd64"):
        arch = "x64"
    else:
        arch = machine or "unknown"
    return system, arch


def _compute_fingerprint(
    os_name: str,
    arch: str,
    ram_mb: int,
    cpu_count: int,
    ep: str,
) -> str:
    """Opaque hash of the static system signature. If this changes, the
    probe re-runs on next boot (e.g., user upgraded RAM, OS major
    version bumped). `ep` is always "cpu" in v1 but kept in the
    fingerprint so a future QNN-capable build naturally invalidates
    the v1 cache on the first boot after the upgrade.
    """
    payload = f"{os_name}|{arch}|{ram_mb}|{cpu_count}|{ep}|{platform.release()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Disk pressure
# ---------------------------------------------------------------------------

def _detect_disk() -> tuple[int, DiskPressure]:
    """Returns (free_disk_mb, pressure_level) against DATA_DIR."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(str(DATA_DIR))
        free_mb = int(usage.free / (1024 * 1024))
    except Exception as e:
        logger.warning("disk probe failed: %s", e)
        return 0, "CRITICAL"
    if free_mb >= _DISK_OK_MB:
        return free_mb, "OK"
    if free_mb >= _DISK_CRITICAL_MB:
        return free_mb, "LOW"
    return free_mb, "CRITICAL"


# ---------------------------------------------------------------------------
# D-FINE calibration benchmark
# ---------------------------------------------------------------------------

def _bundled_model_dir() -> Path:
    """Location of the bundled D-FINE model.

    In dev (`python -m backend.main`), models are under the
    classification subsystem's own directory:
    `backend/classification/models/`.

    In a PyInstaller frozen bundle (the shipping Tauri sidecar),
    `sys._MEIPASS` points at the extracted resource root and the spec
    file copies the model directory to
    `<_MEIPASS>/backend/classification/models/`. Without this guard
    the calibration silently fails in every shipping build because
    `Path(__file__).parent` inside a frozen bundle resolves to a
    temp path that contains the compiled .pyc but not the data files.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "backend" / "classification" / "models"  # type: ignore[attr-defined]
    return Path(__file__).parent / "models"


def _run_calibration_benchmark() -> tuple[float | None, list[str]]:
    """Run D-FINE-N warmup + timed inferences on CPU EP. Returns
    (warm median latency in ms, notes). None latency on any failure.

    D-FINE-N takes two inputs: `images` (1,3,640,640) float32 and
    `orig_target_sizes` (1,2) int64. We use synthetic zeros — the warm
    latency is dominated by graph execution, not content, and avoiding
    an image decode keeps this probe free of any opencv dependency.
    """
    notes: list[str] = []

    try:
        import numpy as np  # type: ignore
        import onnxruntime as ort  # type: ignore
    except ImportError as e:
        notes.append(f"numpy/onnxruntime missing — skipping calibration ({e})")
        logger.warning("numpy/onnxruntime missing — skipping calibration: %s", e)
        return None, notes

    model_path = _bundled_model_dir() / "dfine_n.onnx"
    if not model_path.exists():
        notes.append(f"dfine_n.onnx not bundled at {model_path} — skipping calibration")
        logger.warning("dfine_n.onnx not bundled at %s", model_path)
        return None, notes

    try:
        sess = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
    except Exception as e:
        notes.append(f"D-FINE session creation failed: {e}")
        logger.warning("D-FINE calibration session creation failed: %s", e)
        return None, notes

    try:
        images = np.zeros((1, 3, 640, 640), dtype=np.float32)
        orig_target_sizes = np.array([[640, 640]], dtype=np.int64)
        feeds = {"images": images, "orig_target_sizes": orig_target_sizes}
        # 5 untimed warmup runs to let ORT compile kernels, then 20 timed.
        for _ in range(5):
            sess.run(None, feeds)
        timings: list[float] = []
        for _ in range(20):
            t0 = time.perf_counter()
            sess.run(None, feeds)
            timings.append((time.perf_counter() - t0) * 1000.0)
        median = statistics.median(timings)
        notes.append(f"calibration warm median: {median:.1f} ms")
        return median, notes
    except Exception as e:
        notes.append(f"D-FINE calibration run failed: {e}")
        logger.warning("D-FINE calibration run failed: %s", e)
        return None, notes


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _compute_sync() -> CapabilityReport:
    """Full probe, entirely synchronous. Runs in an executor so the
    20-inference benchmark doesn't block the event loop.
    """
    notes: list[str] = []

    os_name, arch = _detect_os()
    ram_mb = _detect_ram_mb()
    cpu_count = _detect_cpu_count()
    free_mb, pressure = _detect_disk()
    ep: Literal["cpu"] = "cpu"

    if (os.environ.get("SIMPLENVR_CLASSIFIER") or "").lower() == "off":
        notes.append("SIMPLENVR_CLASSIFIER=off — calibration skipped")
        calibration_ms: float | None = None
    else:
        calibration_ms, cal_notes = _run_calibration_benchmark()
        notes.extend(cal_notes)

    fingerprint = _compute_fingerprint(os_name, arch, ram_mb, cpu_count, ep)

    return CapabilityReport(
        ep=ep,
        ram_mb=ram_mb,
        cpu_count=cpu_count,
        os_name=os_name,
        arch=arch,
        free_disk_mb=free_mb,
        disk_pressure=pressure,
        calibration_ms=calibration_ms,
        fingerprint=fingerprint,
        calibrated_at=datetime.now(timezone.utc).isoformat(),
        notes=notes,
    )


async def run_and_persist(conn: "aiosqlite.Connection") -> CapabilityReport:
    """Run the probe off-loop and write the result to the settings table.

    Checks the cached fingerprint first and skips re-calibration if the
    static system signature hasn't changed since the last run — re-
    calibrating on every boot would add ~1s to startup for no reason.
    On cache hit we still refresh `free_disk_mb` + `disk_pressure`
    because those change over time.
    """
    cached_fp = await db.get_setting(conn, "capability_fingerprint")
    cached_calibration = await db.get_setting(conn, "classification_calibration_ms")
    cached_calibrated_at = await db.get_setting(conn, "capability_calibrated_at")
    if cached_fp:
        os_name, arch = _detect_os()
        ram_mb = _detect_ram_mb()
        cpu_count = _detect_cpu_count()
        peek_fp = _compute_fingerprint(os_name, arch, ram_mb, cpu_count, "cpu")
        if peek_fp == cached_fp:
            free_mb, pressure = _detect_disk()
            await db.set_setting(conn, "free_disk_mb", str(free_mb))
            await db.set_setting(conn, "disk_pressure", pressure)
            try:
                cached_ms: float | None = (
                    float(cached_calibration) if cached_calibration else None
                )
            except ValueError:
                cached_ms = None
            print(
                f"[capability_probe] cache hit: ep=cpu fp={cached_fp} "
                f"calibration={f'{cached_ms:.1f}' if cached_ms is not None else 'n/a'}ms "
                f"— skipping re-calibration",
                flush=True,
            )
            logger.info(
                "capability probe: cached fingerprint match (%s) — skipping re-calibration",
                cached_fp,
            )
            return CapabilityReport(
                ep="cpu",
                ram_mb=ram_mb,
                cpu_count=cpu_count,
                os_name=os_name,
                arch=arch,
                free_disk_mb=free_mb,
                disk_pressure=pressure,
                calibration_ms=cached_ms,
                fingerprint=cached_fp,
                calibrated_at=cached_calibrated_at or "",
                notes=["cache hit — skipped calibration"],
            )

    # Cache miss (first boot or hardware changed) — run the full probe.
    loop = asyncio.get_event_loop()
    report = await loop.run_in_executor(None, _compute_sync)

    for key, value in report.to_settings_dict().items():
        await db.set_setting(conn, key, value)

    # Printed (not just logged) so the verification line shows up
    # regardless of uvicorn's log_level — the backend starts uvicorn
    # with log_level='warning' which would otherwise suppress INFO
    # lines. print() goes to stdout, which the Tauri sidecar relays
    # verbatim in dev mode and writes to the sidecar log file in
    # production builds.
    print(
        f"[capability_probe] ep={report.ep} "
        f"ram={report.ram_mb}MB cpu={report.cpu_count} "
        f"os={report.os_name}/{report.arch} "
        f"disk={report.free_disk_mb}MB pressure={report.disk_pressure} "
        f"calibration={f'{report.calibration_ms:.1f}' if report.calibration_ms is not None else 'n/a'}ms",
        flush=True,
    )
    for note in report.notes:
        # Replace Unicode arrows with ASCII to avoid cp1252 encoding
        # errors on Windows consoles.
        safe_note = note.replace("\u2192", "->")
        print(f"[capability_probe]   {safe_note}", flush=True)
    logger.info(
        "capability probe: ep=%s ram=%dMB cpu=%d os=%s/%s disk=%dMB pressure=%s calibration=%sms",
        report.ep, report.ram_mb, report.cpu_count,
        report.os_name, report.arch,
        report.free_disk_mb, report.disk_pressure,
        f"{report.calibration_ms:.1f}" if report.calibration_ms is not None else "n/a",
    )
    return report
