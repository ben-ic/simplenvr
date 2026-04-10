from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager, suppress

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import db, go2rtc_client


def _raise_file_descriptor_limit() -> None:
    """Raise RLIMIT_NOFILE to the hard cap on startup.

    macOS/Linux only. Windows doesn't have this concept — its
    file-handle limit lives in the kernel object pool (~16k by
    default, plenty for our workload) and the `resource` module
    is POSIX-only, so we silently no-op on Windows.

    Why we need it on macOS: the default per-process
    RLIMIT_NOFILE is 256 when launched from GUI/bundled contexts,
    even though the kernel allows tens of thousands. With N
    camera recorders (each holding RTSP sockets, progress pipes,
    motion pipes, preview fan-out sockets), SQLite (DB + WAL +
    SHM), and HTTP/WS clients, 256 is tight enough that normal
    operation can trip `OSError: [Errno 24] Too many open files`.
    We raise to the hard limit at startup before anything opens
    an FD.
    """
    if sys.platform == "win32":
        return
    try:
        import resource  # POSIX-only
    except ImportError:
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ValueError, OSError):
        return
    # Target the hard limit, but cap at 65536 — anything beyond
    # that is implausible for this workload and some platforms
    # reject very large values with EINVAL.
    target = min(hard, 65536) if hard > 0 else 65536
    if target <= soft:
        return
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ValueError, OSError) as exc:
        print(
            f"warning: could not raise RLIMIT_NOFILE from {soft} "
            f"to {target}: {exc}",
            file=sys.stderr,
        )


_raise_file_descriptor_limit()


def _configure_logging() -> None:
    """Configure the root logger so backend INFO logs are visible in
    the dev terminal.

    Without this, Python's default root-logger level is WARNING, which
    silently swallows every `logger.info(...)` call in the backend —
    including the motion detector's "Track promoted" / "Track closed"
    lines, the capability probe's fallback logs, and the discovery
    scanner's status updates. Ben-the-dev needs to see these to debug.

    Production builds stay quiet: uvicorn's own log_level in the
    __main__ block below is still 'warning', so starlette/fastapi
    access logs don't spam the terminal. We only bump the root so our
    own backend.* INFO calls are visible.

    Gated on SIMPLENVR_DEV=1 so shipped installers don't have verbose
    stderr output unless explicitly enabled.
    """
    import logging

    if os.environ.get("SIMPLENVR_DEV") != "1":
        return

    root = logging.getLogger()
    if root.handlers:
        # Already configured (maybe by uvicorn reload or a test harness)
        # — don't double-add handlers, just bump the level.
        for h in root.handlers:
            h.setLevel(logging.INFO)
        root.setLevel(logging.INFO)
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Silence third-party HTTP client chatter. httpx + httpcore log
    # every request at INFO, which drowns the log at ~20+ lines/sec
    # once the HLS proxy is running (4 cameras × 2 segments/sec ×
    # React Strict Mode double-mount). Nothing in our own code cares
    # about these messages — we only care about our own backend.* and
    # uvicorn.* loggers. Raising httpx to WARNING leaves real errors
    # (connection refused, timeouts) visible while killing the spam.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


_configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    conn = await db.init_db()
    app.state.db = conn

    # Hardware capability probe — runs once on first launch, cached on
    # subsequent boots via a fingerprint of the static system signature
    # (OS + arch + RAM + CPU count + selected EP). Writes
    # classification_tier, classification_ep, free_disk_mb, disk_pressure,
    # summarizer_eligible to the settings table so every downstream
    # subsystem reads a centralized verdict instead of re-running its
    # own hardware detection. Any probe failure (ORT missing, bundled
    # model corrupt, calibration regressed below threshold) falls back
    # to tier='disabled' and the classifier subsystem silently refuses
    # to start — the Inbox stays at "Motion at X" forever on that
    # install, which is the safe failure mode.
    from .classification import capability_probe
    try:
        report = await capability_probe.run_and_persist(conn)
        app.state.capability = report
    except Exception as e:
        # We do NOT want a probe crash to take the whole app down.
        # The classifier is additive — without it, recording still
        # works and the Inbox just shows generic "Motion at X" rows.
        import logging
        logging.getLogger(__name__).error(
            "capability probe failed, classifier will stay disabled: %s",
            e, exc_info=True,
        )
        app.state.capability = None

    # Bare-python dev mode (SIMPLENVR_DEV=1 without a Tauri shell) has
    # no one else to start go2rtc — the Tauri Rust side normally does
    # this before spawning Python. ensure_running() spawns the binary
    # ourselves in that case, sets SIMPLENVR_GO2RTC_URL in the current
    # process env, and waits for the admin API. No-op in Tauri mode
    # where the env var is already set. Non-fatal failure — the backend
    # continues with direct camera URLs, which is the documented
    # graceful-fallback behavior.
    from . import dev_go2rtc
    await dev_go2rtc.ensure_running()

    # Block until go2rtc's admin API is reachable, so the discovery
    # scanner can register every camera as part of its own startup
    # without racing the sidecar. In Tauri mode the shell has already
    # waited, so this usually returns true on the first probe. In
    # bare-python dev mode, ensure_running() above already blocked
    # until readiness, so this is a no-op. Kept for defense-in-depth
    # against the case where something else (manual go2rtc launch,
    # tighter Tauri budget) left us in a partial-ready state.
    if go2rtc_client.is_enabled():
        await go2rtc_client.wait_for_ready(timeout_s=15.0)

    # Import here to avoid circular imports at module level
    from .api.ws import EventBus
    from .discovery.scanner import DiscoveryScanner
    from .recording.manager import RecordingManager
    from .motion.manager import MotionManager

    event_bus = EventBus()
    scanner = DiscoveryScanner(conn, event_bus)
    recorder = RecordingManager(conn, event_bus)

    # Classification manager — constructed BEFORE MotionManager so we
    # can thread the submit path into each MotionDetector. Reads the
    # capability probe's verdict from the settings table; if tier is
    # 'disabled' or SIMPLENVR_CLASSIFIER=off, start() is a no-op and
    # manager.enabled stays False. In that case MotionManager still
    # runs, tracks still get persisted, but no labels are ever
    # written — the Inbox stays at "Motion at X" forever on that
    # install, matching the zero-knob safe-failure contract.
    from .classification.manager import ClassificationManager
    tier = (await db.get_setting(conn, "classification_tier")) or "disabled"
    ep = (await db.get_setting(conn, "classification_ep")) or "none"
    classifier = ClassificationManager(conn, event_bus, tier=tier, ep=ep, summarizer=None)
    try:
        await classifier.start()
    except Exception as e:
        import logging as _logging
        _logging.getLogger(__name__).error(
            "classifier manager start failed, staying disabled: %s", e, exc_info=True,
        )

    # --- Summarizer (Moondream VLM, optional) ---
    from .summarizer.manager import SummarizerManager
    summarizer_eligible = (
        (await db.get_setting(conn, "summarizer_eligible")) == "true"
    )
    summarizer = SummarizerManager(conn, event_bus, eligible=summarizer_eligible)
    try:
        await summarizer.start()
    except Exception as e:
        import logging as _logging
        _logging.getLogger(__name__).error(
            "summarizer manager start failed, staying disabled: %s", e, exc_info=True,
        )

    # Wire summarizer into classifier so labeled events get VLM descriptions.
    if summarizer.enabled:
        classifier._summarizer = summarizer

    motion = MotionManager(conn, event_bus, recorder, classifier=classifier)

    app.state.event_bus = event_bus
    app.state.scanner = scanner
    app.state.recorder = recorder
    app.state.classifier = classifier
    app.state.summarizer = summarizer
    app.state.motion = motion

    scan_task = asyncio.create_task(scanner.run_forever())
    recorder_task = asyncio.create_task(recorder.run_forever())
    motion_task = asyncio.create_task(motion.run_forever())

    yield

    # Shutdown — stop recorder first so segments finalize before DB closes
    recorder_task.cancel()
    with suppress(asyncio.CancelledError):
        await recorder_task
    await recorder.shutdown()

    motion_task.cancel()
    with suppress(asyncio.CancelledError):
        await motion_task
    await motion.shutdown()

    # Classifier after motion so any in-flight submits from detector
    # shutdown flushes have landed on the queue before we cancel the
    # worker.
    await classifier.shutdown()
    await summarizer.shutdown()

    scan_task.cancel()
    with suppress(asyncio.CancelledError):
        await scan_task
    await conn.close()


app = FastAPI(title="SimpleNVR", version="0.1.0", lifespan=lifespan)

# CORS is locked to the Tauri WebView origin plus the Vite dev server.
# The Python sidecar binds to 127.0.0.1 only, but that alone does NOT stop
# a malicious web page the user visits from firing cross-origin requests
# at the loopback API — wildcard CORS would let any page extract camera
# credentials, mutate settings, or trigger SSRF via the manual-camera
# probe. The allowlist below is the authoritative trust boundary.
_allowed_origins = [
    "tauri://localhost",
    "https://tauri.localhost",  # Windows WebView2 uses https://tauri.localhost
    "http://tauri.localhost",   # WebView2 may also use http:// depending on config
]
if os.environ.get("SIMPLENVR_DEV") == "1":
    # Vite dev server origins. The WebView under `cargo tauri dev`
    # loads the frontend from the Vite port (3000, set in
    # frontend/vite.config.ts), which means `window.__TAURI_INTERNALS__`
    # is defined AND the document origin is `http://localhost:3000`.
    # That combination trips backend.ts into the Tauri branch —
    # every API call becomes cross-origin to the backend loopback
    # URL and goes through this CORS allowlist. Both localhost and
    # 127.0.0.1 forms are included because the WebView may load the
    # page by either hostname depending on the Tauri devUrl config.
    _allowed_origins += [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    # Expose range-related headers so cross-origin JS (hls.js in
    # particular) can read them on recording-segment fetches. The
    # CORS "safelisted response headers" set only includes
    # Content-Length, Content-Type and a few others — Content-Range
    # and Accept-Ranges are NOT safelisted, so without this list
    # JS can't tell the server honored its Range request or what
    # total size the file is. hls.js uses Content-Range to validate
    # partial responses when loading fragmented-MP4 segments; if it
    # can't read the header it gives up on the level and surfaces
    # "Found no media in msn 0 of level" to the user. This affects
    # both cargo tauri dev (page origin http://localhost:3000,
    # backend origin http://127.0.0.1:<port>) and bundled Tauri
    # (page origin tauri://localhost, backend origin same as dev).
    # The bare-python dev workflow is unaffected because it runs
    # same-origin via the Vite proxy.
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
    allow_credentials=False,
)

# Register routes
from .api.cameras import router as cameras_router  # noqa: E402
from .api.recordings import router as recordings_router  # noqa: E402
from .api.settings import router as settings_router  # noqa: E402
from .api.streams import router as streams_router, g2r_router  # noqa: E402
from .api.motion import router as motion_router  # noqa: E402
from .api.ws import router as ws_router  # noqa: E402

app.include_router(cameras_router, prefix="/api")
app.include_router(recordings_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(streams_router, prefix="/api")
app.include_router(motion_router, prefix="/api")
app.include_router(ws_router)
# The /g2r proxy intentionally lives at the root, not under /api,
# because the frontend treats /g2r as a distinct path prefix that
# mirrors the Vite dev proxy rule in frontend/vite.config.ts. See
# backend/api/streams.py for the lineage and the full rationale.
app.include_router(g2r_router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


def _launch_sidecar() -> None:
    """Sidecar launch sequence, shared by `python -m backend.main` and
    the PyInstaller-bundled entry point at `backend/_pyi_entry.py`.

    PyInstaller's onefile bootstrap runs `_pyi_entry.py` as its
    `__main__` script (the spec file declares that, not this one),
    so the `if __name__ == "__main__"` block below is NEVER reached
    in bundled builds. Extracting the launch logic here forces
    both entry points to converge on the same code path and stops
    future additions from drifting — a drift that caused the
    2026-04-10 bundled-mode live-preview regression where
    `SIMPLENVR_BACKEND_PORT` was set in the main.py __main__ block
    but missing from `_pyi_entry.py`, making the backend emit a
    relative `/g2r` path that the WKWebView then mangled into
    `wsi://localhost/...` via a separate video-rtc.js URL-scheme
    assumption bug.
    """
    import json
    import os
    import sys
    import threading
    import uvicorn
    from .port_finder import pick_port_with_socket

    port, sock = pick_port_with_socket()

    # Signal to the Tauri Rust shell that we chose a port. Must be
    # written BEFORE uvicorn.run() blocks. Uses os.write directly on
    # fd 1 to bypass any buffering PyInstaller's onefile bootloader
    # may interpose between Python's sys.stdout and the real pipe
    # the Tauri shell reads from. The Tauri shell parses this single
    # JSON line to discover the bound backend port.
    _ready_signal = json.dumps({"port": port, "ready": True}) + "\n"
    os.write(1, _ready_signal.encode("utf-8"))
    try:
        sys.stdout.flush()
    except Exception:
        pass

    # Stash the chosen port where the FastAPI handlers can read it.
    # The /g2r proxy path's emitted base URL needs to be absolute
    # in Tauri mode (the WebView origin tauri://localhost can't
    # resolve relative /g2r to the backend) so the WS snapshot in
    # backend/api/ws.py reads this env var and builds an absolute
    # http://127.0.0.1:<port>/g2r URL when SIMPLENVR_STDIN_WATCHDOG
    # is set. uvicorn.run below imports the app in the same process
    # so the env var is visible to the running app.
    os.environ["SIMPLENVR_BACKEND_PORT"] = str(port)

    # Stdin watchdog (orphan protection). When the Tauri Rust shell spawns
    # us as a sidecar it sets SIMPLENVR_STDIN_WATCHDOG=1 and connects our
    # stdin to a pipe. If the parent process dies, the pipe closes and
    # sys.stdin.read() returns EOF — we shut down cleanly so we don't
    # end up as an orphan eating CPU and holding RTSP slots on the cameras.
    #
    # CRITICAL: we send SIGTERM to ourselves rather than calling os._exit().
    # uvicorn's signal handler catches SIGTERM, flips should_exit, and runs
    # the FastAPI lifespan shutdown — which calls recorder.shutdown() →
    # stop_recording() per camera → terminate_process_group() per ffmpeg
    # child. os._exit() would bypass all of that and leave 5 orphan ffmpeg
    # processes holding RTSP slots after the Tauri parent died.
    #
    # The env var gate is mandatory because a bare `python -m backend.main
    # < /dev/null` (or any non-interactive non-piped invocation) would
    # otherwise EOF immediately and we'd exit at startup. With the env var
    # unset (the dev workflow), we never touch stdin at all.
    if os.environ.get("SIMPLENVR_STDIN_WATCHDOG") == "1" and sys.platform != "win32":
        # On Windows the tether supervisor uses a Job Object with
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, which is a kernel-level
        # guarantee that the child dies when tether dies.  The stdin
        # watchdog is unnecessary there AND broken: PyInstaller's
        # onefile bootloader on Windows doesn't reliably inherit the
        # stdin pipe through to the inner Python process, so
        # sys.stdin.read() returns EOF immediately and kills the
        # backend before uvicorn can bind.
        import signal as _signal

        def _stdin_watchdog() -> None:
            try:
                sys.stdin.read()  # blocks until parent closes the pipe
            except Exception:
                pass
            try:
                os.kill(os.getpid(), _signal.SIGTERM)
            except Exception:
                # If signal delivery itself fails (very unlikely), fall back
                # to a hard exit so we don't sit forever after parent death.
                os._exit(0)

        threading.Thread(target=_stdin_watchdog, daemon=True).start()

    # Bare-python mode backstop: register atexit handlers that sweep
    # orphan child processes on ANY exit path uvicorn's lifespan
    # shutdown doesn't cover. atexit runs on normal exit, on
    # sys.exit(), and on SIGTERM (which uvicorn translates into
    # sys.exit after its own shutdown). It does NOT run on SIGKILL
    # or hard crash, but that's the only class of exit left after
    # this. atexit runs in LIFO order so dev_go2rtc.shutdown fires
    # before kill_orphan_ffmpegs — that ordering matters because
    # stopping go2rtc cleanly is preferable to the ppid-based sweep.
    #
    # Gated on SIMPLENVR_TETHER_BIN being unset — Tauri mode has the
    # tether binary guaranteeing child death on any parent exit, so
    # it doesn't need (and shouldn't run) the atexit sweeps.
    if not os.environ.get("SIMPLENVR_TETHER_BIN"):
        import atexit
        from .process_cleanup import kill_orphan_ffmpegs, kill_orphan_go2rtc
        from .dev_go2rtc import shutdown as _dev_go2rtc_shutdown
        atexit.register(kill_orphan_ffmpegs)
        atexit.register(kill_orphan_go2rtc)
        atexit.register(_dev_go2rtc_shutdown)

    # Close the pre-bound socket and pass host/port to uvicorn instead.
    # We previously used fd= to avoid a TOCTOU race on the port, but
    # uvicorn >=0.44 hardcodes AF_UNIX when reconstructing from fd,
    # which crashes on Windows (no AF_UNIX support). The TOCTOU window
    # between sock.close() and uvicorn.run() binding is negligible for
    # a desktop app on loopback.
    #
    # timeout_graceful_shutdown=35 gives the lifespan shutdown room to
    # complete the per-camera CameraRecorder.stop() calls, each of
    # which awaits ffmpeg finalization for up to 30 seconds. Uvicorn's
    # default is 5 seconds, which was truncating shutdown and leaving
    # half-stopped recorders behind.
    sock.close()
    uvicorn.run(
        "backend.main:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
        timeout_graceful_shutdown=35,
    )


if __name__ == "__main__":
    _launch_sidecar()
