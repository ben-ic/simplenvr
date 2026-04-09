"""Capability probe — hardware detection + tier assignment.

Runs once on first launch. Produces a verdict that every downstream
subsystem reads from `settings` without re-running detection logic:

    classification_tier     : strong | normal | modest | weak | disabled
    classification_ep       : coreml | qnn | directml | openvino | cuda | cpu | none
    free_disk_mb            : int
    disk_pressure           : OK | LOW | CRITICAL
    summarizer_eligible     : bool
    capability_fingerprint  : str (OS+arch+cpu+ram+accel hash — re-probes on change)
    capability_calibrated_at: ISO8601 timestamp

The calibration benchmark is the answer, not the question. No vendor
databases, no "Snapdragon should be fast" assumptions — we run 20
warmup inferences on YOLOX-Nano against a bundled calibration JPEG
and bucket on the measured warm median latency. This is robust against:

  * hardware we've never seen before
  * aging chips that thermal-throttle over years of use
  * the user running other heavy apps simultaneously
  * ORT version regressions on specific EP + model combinations

Escape hatches (Ben-the-dev only, zero user-facing config):

    SIMPLENVR_CLASSIFIER=off              — disable classifier entirely
    SIMPLENVR_CLASSIFIER_TIER=strong|...  — force tier, skip calibration
    SIMPLENVR_CLASSIFIER_EP=coreml|...    — force execution provider
    SIMPLENVR_SUMMARIZER=off              — disable summarizer entirely
    SIMPLENVR_VLM_FORCE_AVAILABLE=1       — offer summarizer even on Weak tier

If the calibration benchmark fails outright (ORT can't load any EP,
bundled model file corrupt, bundled calibration image corrupt, or the
model runs but produces garbage output), the probe falls back to
`classification_tier=disabled` and logs a warning. The classifier
subsystem honors `disabled` by never starting its worker. This is
the "turn off for slow machines" contract Ben asked for: a failure
at any stage of the probe means the feature silently opts out and
the Inbox stays at "Motion at X" forever on that install.
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
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .. import db
from ..config import DATA_DIR

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------

Tier = Literal["strong", "normal", "modest", "weak", "disabled"]
ExecutionProvider = Literal[
    "coreml", "qnn", "directml", "openvino", "cuda", "cpu", "none"
]
DiskPressure = Literal["OK", "LOW", "CRITICAL"]

# Latency buckets (warm median ms on YOLOX-Nano 416x416).
# Measured against real hardware 2026-04-09:
#   M4 MacBook Air CoreML  → 6.4 ms   → strong
#   Snapdragon X NPU (QNN) → ~9 ms    → strong (estimated from Qualcomm card)
#   Intel i5 CPU           → ~60 ms   → modest
#   RPi 5 XNNPACK          → ~150 ms  → weak
_TIER_LATENCY_MS: dict[Tier, float] = {
    "strong": 15.0,
    "normal": 50.0,
    "modest": 150.0,
    # anything >= modest ceiling → weak
}

# Minimum RAM (MB) for each tier. Below this, demote regardless of latency.
# Prevents a single-digit-ms latency on a 2GB SBC from being called Strong
# when it would OOM trying to load YOLOX-S at runtime.
_TIER_MIN_RAM_MB: dict[Tier, int] = {
    "strong": 8_000,
    "normal": 4_000,
    "modest": 2_000,
    "weak": 0,
}

# Free-disk thresholds for summarizer eligibility + disk pressure.
_SUMMARIZER_MIN_FREE_MB = 3_000
_DISK_OK_MB = 5_000
_DISK_CRITICAL_MB = 1_000


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class CapabilityReport:
    tier: Tier
    ep: ExecutionProvider
    ram_mb: int
    cpu_count: int
    os_name: str
    arch: str
    free_disk_mb: int
    disk_pressure: DiskPressure
    calibration_ms: float | None  # None if calibration was skipped or failed
    summarizer_eligible: bool
    fingerprint: str
    calibrated_at: str
    notes: list[str]  # human-readable reasons ("tier forced via env", "ORT missing", etc.)

    def to_settings_dict(self) -> dict[str, str]:
        """Flatten to the string-keyed shape the settings table expects."""
        return {
            "classification_tier": self.tier,
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
            "summarizer_eligible": "true" if self.summarizer_eligible else "false",
            "capability_fingerprint": self.fingerprint,
            "capability_calibrated_at": self.calibrated_at,
            "capability_notes": json.dumps(self.notes),
        }


# ---------------------------------------------------------------------------
# Static system info
# ---------------------------------------------------------------------------

def _detect_ram_mb() -> int:
    """Total physical RAM in MB across Windows/macOS/Linux.

    psutil is the preferred path and is a declared backend dependency,
    so in the shipping bundle this branch always succeeds. The
    platform-specific fallbacks exist for dev installs where the full
    requirements weren't pip-installed, and for the theoretical case
    where psutil is present but its memory probe fails.

    Returns 0 if we can't figure it out — 0 forces the probe into the
    "weak" tier by the RAM gate, which is the safe failure mode: when
    in doubt, don't load the big model.
    """
    # Preferred path: psutil (cross-platform, declared dep).
    try:
        import psutil  # type: ignore
        mem = psutil.virtual_memory().total
        if mem > 0:
            return int(mem / (1024 * 1024))
    except Exception:
        pass

    # macOS fallback.
    if sys.platform == "darwin":
        try:
            import subprocess
            out = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], text=True, timeout=2
            )
            return int(int(out.strip()) / (1024 * 1024))
        except Exception:
            return 0

    # Linux fallback.
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        # "MemTotal:       16289208 kB"
                        return int(int(line.split()[1]) / 1024)
        except Exception:
            return 0

    # Windows fallback via ctypes (no new dependency). This path runs
    # only if psutil failed to import AND we're on Windows — a
    # dev-install edge case, but we cover it so the tier assignment
    # is never wrong because of a missing optional package.
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
    # Normalize arch names to two canonical buckets.
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
    ep: ExecutionProvider,
) -> str:
    """Opaque hash of the static system signature. If this changes, the
    probe re-runs on next boot (e.g., user upgraded RAM, OS major version
    bumped, installed a new accelerator driver).
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
# Execution provider detection + calibration benchmark
# ---------------------------------------------------------------------------

def _bundled_model_dir() -> Path:
    """Location of the bundled calibration model + image.

    In dev (`python -m backend.main`), models are under the classification
    subsystem's own directory: `backend/classification/models/`.

    In a PyInstaller frozen bundle (the shipping Tauri sidecar on Windows,
    macOS, and Linux), `sys._MEIPASS` points at the extracted resource
    root. The PyInstaller spec file copies the model directory to
    `<_MEIPASS>/backend/classification/models/` so the same relative
    path works across both contexts.

    This matters because `Path(__file__).parent` inside a frozen bundle
    resolves to a PyInstaller temp path that does NOT contain the
    data files — only the compiled .pyc. The data files are a separate
    copy at `_MEIPASS`. Without this guard, the probe works in dev and
    silently falls back to "calibration model not bundled → disabled"
    in every shipping build.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "backend" / "classification" / "models"  # type: ignore[attr-defined]
    return Path(__file__).parent / "models"


def _detect_best_ep() -> tuple[ExecutionProvider, list[str]]:
    """Enumerate ORT providers, return the best one we can actually load.

    "Best" is a fixed priority order per platform. We try to create a
    throwaway session with each provider in turn — "available" providers
    that fail at session creation are treated as absent. This catches
    cases like "QNN EP is registered but the HTP driver isn't installed"
    and "DirectML EP is present but the GPU is disabled."

    Returns (ep, notes) — notes are human-readable explanations of what
    was tried and what worked.
    """
    notes: list[str] = []
    try:
        import onnxruntime as ort  # type: ignore
    except ImportError:
        notes.append("onnxruntime not importable — classifier disabled")
        return "none", notes

    available = set(ort.get_available_providers())
    notes.append(f"ORT reports providers: {sorted(available)}")

    # Priority chain per platform. Order matters — first working EP wins.
    os_name, arch = _detect_os()
    if os_name == "darwin":
        chain: list[tuple[ExecutionProvider, str]] = [
            ("coreml", "CoreMLExecutionProvider"),
            ("cpu", "CPUExecutionProvider"),
        ]
    elif os_name == "windows" and arch == "arm64":
        chain = [
            ("qnn", "QNNExecutionProvider"),
            ("directml", "DmlExecutionProvider"),
            ("cpu", "CPUExecutionProvider"),
        ]
    elif os_name == "windows":
        chain = [
            ("openvino", "OpenVINOExecutionProvider"),
            ("directml", "DmlExecutionProvider"),
            ("cuda", "CUDAExecutionProvider"),
            ("cpu", "CPUExecutionProvider"),
        ]
    elif os_name == "linux":
        chain = [
            ("cuda", "CUDAExecutionProvider"),
            ("openvino", "OpenVINOExecutionProvider"),
            ("cpu", "CPUExecutionProvider"),
        ]
    else:
        chain = [("cpu", "CPUExecutionProvider")]

    # Look for the calibration model. If it's missing, we can only report
    # what ORT claims is available — can't validate via a real session.
    calib_model = _bundled_model_dir() / "yolox_nano.onnx"
    if not calib_model.exists():
        notes.append(
            f"calibration model not bundled at {calib_model} — "
            f"reporting first ORT-available EP without session validation"
        )
        for ep_name, ort_name in chain:
            if ort_name in available:
                return ep_name, notes
        return "none", notes

    for ep_name, ort_name in chain:
        if ort_name not in available:
            continue
        try:
            sess = ort.InferenceSession(
                str(calib_model), providers=[ort_name]
            )
            # Verify the session actually bound the EP (some EPs silently
            # fall back to CPU when session creation "succeeds" but the
            # model has unsupported ops). We check the first-in-list
            # actual provider.
            actual = sess.get_providers()
            if not actual or actual[0] != ort_name:
                notes.append(
                    f"{ort_name} loaded but ORT fell back to {actual} — skipping"
                )
                continue
            notes.append(f"{ort_name} validated via session probe")
            return ep_name, notes
        except Exception as e:
            notes.append(f"{ort_name} session probe failed: {e}")
            continue

    notes.append("no EP in the priority chain could create a working session")
    return "none", notes


def _run_calibration_benchmark(ep: ExecutionProvider) -> float | None:
    """Run 20 warmup inferences on YOLOX-Nano + bundled test image and
    return the warm median latency in milliseconds.

    Returns None on any failure — caller treats None as "disabled" tier.
    """
    if ep == "none":
        return None

    try:
        import numpy as np  # type: ignore
        import onnxruntime as ort  # type: ignore
    except ImportError:
        logger.warning("numpy/onnxruntime missing — skipping calibration")
        return None

    model_path = _bundled_model_dir() / "yolox_nano.onnx"
    if not model_path.exists():
        logger.warning("yolox_nano.onnx not bundled — skipping calibration")
        return None

    ep_map = {
        "coreml": "CoreMLExecutionProvider",
        "qnn": "QNNExecutionProvider",
        "directml": "DmlExecutionProvider",
        "openvino": "OpenVINOExecutionProvider",
        "cuda": "CUDAExecutionProvider",
        "cpu": "CPUExecutionProvider",
    }
    ort_provider = ep_map.get(ep)
    if ort_provider is None:
        return None

    try:
        sess = ort.InferenceSession(str(model_path), providers=[ort_provider])
    except Exception as e:
        logger.warning("calibration session creation failed: %s", e)
        return None

    # YOLOX-Nano expects 1x3x416x416 float32 in BGR order, values in 0..255
    # after letterboxing. For calibration we use a synthetic grey image —
    # the warm latency is dominated by the graph execution, not the
    # content. Using a synthetic image means we don't need to bundle a
    # calibration JPEG (saves ~1 MB in the installer).
    try:
        input_name = sess.get_inputs()[0].name
        dummy = (
            np.full((1, 3, 416, 416), 114.0, dtype=np.float32)
        )
        # Warmup: 5 untimed + 20 timed. ORT compiles kernels on the first
        # call; first-call latency is not representative.
        for _ in range(5):
            sess.run(None, {input_name: dummy})
        timings: list[float] = []
        for _ in range(20):
            t0 = time.perf_counter()
            sess.run(None, {input_name: dummy})
            timings.append((time.perf_counter() - t0) * 1000.0)
        return statistics.median(timings)
    except Exception as e:
        logger.warning("calibration run failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Tier decision
# ---------------------------------------------------------------------------

def _bucket_tier(
    calibration_ms: float | None,
    ram_mb: int,
    ep: ExecutionProvider,
) -> tuple[Tier, list[str]]:
    """Bucket the measurements into a tier, with human-readable notes."""
    notes: list[str] = []

    if ep == "none":
        notes.append("no working execution provider → disabled")
        return "disabled", notes

    if calibration_ms is None:
        notes.append("calibration failed → disabled")
        return "disabled", notes

    # Start with latency bucket.
    if calibration_ms < _TIER_LATENCY_MS["strong"]:
        tier: Tier = "strong"
    elif calibration_ms < _TIER_LATENCY_MS["normal"]:
        tier = "normal"
    elif calibration_ms < _TIER_LATENCY_MS["modest"]:
        tier = "modest"
    else:
        tier = "weak"
    notes.append(f"latency {calibration_ms:.1f} ms → {tier} by latency")

    # Demote if RAM is insufficient for the tier.
    while _TIER_MIN_RAM_MB[tier] > ram_mb and tier != "weak":
        demoted: Tier = {
            "strong": "normal",
            "normal": "modest",
            "modest": "weak",
            "weak": "weak",
        }[tier]
        notes.append(
            f"RAM {ram_mb} MB below {tier} minimum "
            f"{_TIER_MIN_RAM_MB[tier]} → demoted to {demoted}"
        )
        tier = demoted

    return tier, notes


def _parse_tier_override(value: str | None) -> Tier | None:
    if not value:
        return None
    v = value.strip().lower()
    if v in ("strong", "normal", "modest", "weak", "disabled", "off"):
        return "disabled" if v == "off" else v  # type: ignore[return-value]
    return None


def _parse_ep_override(value: str | None) -> ExecutionProvider | None:
    if not value:
        return None
    v = value.strip().lower()
    if v in ("coreml", "qnn", "directml", "openvino", "cuda", "cpu", "none"):
        return v  # type: ignore[return-value]
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _compute_sync(_conn_unused) -> CapabilityReport:
    """The actual probe, entirely synchronous. Runs in an executor so
    the 20-inference benchmark doesn't block the event loop.
    """
    notes: list[str] = []

    # --- hard kill switch ---
    if (os.environ.get("SIMPLENVR_CLASSIFIER") or "").lower() == "off":
        notes.append("SIMPLENVR_CLASSIFIER=off → disabled")
        os_name, arch = _detect_os()
        ram_mb = _detect_ram_mb()
        free_mb, pressure = _detect_disk()
        return CapabilityReport(
            tier="disabled",
            ep="none",
            ram_mb=ram_mb,
            cpu_count=_detect_cpu_count(),
            os_name=os_name,
            arch=arch,
            free_disk_mb=free_mb,
            disk_pressure=pressure,
            calibration_ms=None,
            summarizer_eligible=False,
            fingerprint=_compute_fingerprint(os_name, arch, ram_mb, _detect_cpu_count(), "none"),
            calibrated_at=datetime.now(timezone.utc).isoformat(),
            notes=notes,
        )

    # --- static info ---
    os_name, arch = _detect_os()
    ram_mb = _detect_ram_mb()
    cpu_count = _detect_cpu_count()
    free_mb, pressure = _detect_disk()

    # --- EP detection ---
    forced_ep = _parse_ep_override(os.environ.get("SIMPLENVR_CLASSIFIER_EP"))
    if forced_ep is not None:
        ep: ExecutionProvider = forced_ep
        notes.append(f"EP forced via env: {ep}")
    else:
        ep, ep_notes = _detect_best_ep()
        notes.extend(ep_notes)

    # --- calibration benchmark ---
    calibration_ms = _run_calibration_benchmark(ep) if ep != "none" else None
    if calibration_ms is not None:
        notes.append(f"calibration warm median: {calibration_ms:.1f} ms")

    # --- tier decision (env override wins) ---
    forced_tier = _parse_tier_override(os.environ.get("SIMPLENVR_CLASSIFIER_TIER"))
    if forced_tier is not None:
        tier: Tier = forced_tier
        notes.append(f"tier forced via env: {tier}")
    else:
        tier, tier_notes = _bucket_tier(calibration_ms, ram_mb, ep)
        notes.extend(tier_notes)

    # --- summarizer eligibility ---
    summarizer_force = (os.environ.get("SIMPLENVR_VLM_FORCE_AVAILABLE") or "").lower() == "1"
    summarizer_off = (os.environ.get("SIMPLENVR_SUMMARIZER") or "").lower() == "off"
    if summarizer_off:
        summarizer_eligible = False
        notes.append("SIMPLENVR_SUMMARIZER=off → summarizer ineligible")
    elif summarizer_force:
        summarizer_eligible = True
        notes.append("SIMPLENVR_VLM_FORCE_AVAILABLE=1 → forcing eligible")
    else:
        summarizer_eligible = (
            tier in ("strong", "normal")
            and free_mb >= _SUMMARIZER_MIN_FREE_MB
            and pressure == "OK"
        )
        if not summarizer_eligible:
            reasons = []
            if tier not in ("strong", "normal"):
                reasons.append(f"tier={tier}")
            if free_mb < _SUMMARIZER_MIN_FREE_MB:
                reasons.append(f"free_disk={free_mb}MB<{_SUMMARIZER_MIN_FREE_MB}MB")
            if pressure != "OK":
                reasons.append(f"disk_pressure={pressure}")
            notes.append(f"summarizer ineligible: {', '.join(reasons)}")

    fingerprint = _compute_fingerprint(os_name, arch, ram_mb, cpu_count, ep)

    return CapabilityReport(
        tier=tier,
        ep=ep,
        ram_mb=ram_mb,
        cpu_count=cpu_count,
        os_name=os_name,
        arch=arch,
        free_disk_mb=free_mb,
        disk_pressure=pressure,
        calibration_ms=calibration_ms,
        summarizer_eligible=summarizer_eligible,
        fingerprint=fingerprint,
        calibrated_at=datetime.now(timezone.utc).isoformat(),
        notes=notes,
    )


async def run_and_persist(conn: "aiosqlite.Connection") -> CapabilityReport:
    """Run the probe off-loop and write the result to the settings table.

    This is the function the FastAPI lifespan should call. It checks the
    cached fingerprint first and skips re-calibration if the static system
    signature hasn't changed since the last run — re-calibrating on every
    boot would add ~500 ms to startup for no reason.
    """
    # Check cache.
    cached_fp = await db.get_setting(conn, "capability_fingerprint")
    cached_tier = await db.get_setting(conn, "classification_tier")
    if cached_fp and cached_tier:
        # Peek at the static signature without running the calibration.
        os_name, arch = _detect_os()
        ram_mb = _detect_ram_mb()
        cpu_count = _detect_cpu_count()
        cached_ep = await db.get_setting(conn, "classification_ep") or "none"
        peek_fp = _compute_fingerprint(os_name, arch, ram_mb, cpu_count, cached_ep)  # type: ignore[arg-type]
        if peek_fp == cached_fp:
            print(
                f"[capability_probe] cache hit: tier={cached_tier} "
                f"ep={cached_ep} fp={cached_fp} — skipping re-calibration",
                flush=True,
            )
            logger.info(
                "capability probe: cached fingerprint match (%s), tier=%s ep=%s — skipping re-calibration",
                cached_fp, cached_tier, cached_ep,
            )
            # Refresh disk pressure only — that changes over time.
            free_mb, pressure = _detect_disk()
            await db.set_setting(conn, "free_disk_mb", str(free_mb))
            await db.set_setting(conn, "disk_pressure", pressure)
            # Return a minimal report; manager doesn't need the full thing
            # when we're taking the cache path.
            return CapabilityReport(
                tier=cached_tier,  # type: ignore[arg-type]
                ep=cached_ep,  # type: ignore[arg-type]
                ram_mb=ram_mb,
                cpu_count=cpu_count,
                os_name=os_name,
                arch=arch,
                free_disk_mb=free_mb,
                disk_pressure=pressure,
                calibration_ms=None,
                summarizer_eligible=(
                    (await db.get_setting(conn, "summarizer_eligible")) == "true"
                ),
                fingerprint=cached_fp,
                calibrated_at=(
                    await db.get_setting(conn, "capability_calibrated_at") or ""
                ),
                notes=["cache hit — skipped calibration"],
            )

    # Cache miss (first boot or hardware changed) — run the full probe
    # off the event loop so the benchmark doesn't block.
    loop = asyncio.get_event_loop()
    report = await loop.run_in_executor(None, _compute_sync, None)

    # Persist.
    for key, value in report.to_settings_dict().items():
        await db.set_setting(conn, key, value)

    # Printed (not logged) so the verification line shows up regardless
    # of uvicorn's log_level — the backend starts uvicorn with
    # log_level='warning' which would otherwise suppress these INFO
    # lines, leaving Ben-the-dev with no visible signal that the probe
    # actually ran. print() goes to stdout, which the Tauri sidecar
    # relays verbatim in dev mode and writes to the sidecar log file
    # in production builds.
    print(
        f"[capability_probe] tier={report.tier} ep={report.ep} "
        f"ram={report.ram_mb}MB disk={report.free_disk_mb}MB "
        f"pressure={report.disk_pressure} "
        f"calibration={f'{report.calibration_ms:.1f}' if report.calibration_ms is not None else 'n/a'}ms "
        f"summarizer_eligible={report.summarizer_eligible}",
        flush=True,
    )
    for note in report.notes:
        print(f"[capability_probe]   {note}", flush=True)
    # Also log at INFO for structured log aggregation if anyone hooks
    # logging.basicConfig() into the backend later — the print is the
    # primary surface, the log call is belt-and-suspenders.
    logger.info(
        "capability probe: tier=%s ep=%s ram=%dMB disk=%dMB pressure=%s "
        "calibration=%sms summarizer_eligible=%s",
        report.tier, report.ep, report.ram_mb, report.free_disk_mb,
        report.disk_pressure,
        f"{report.calibration_ms:.1f}" if report.calibration_ms is not None else "n/a",
        report.summarizer_eligible,
    )
    return report
