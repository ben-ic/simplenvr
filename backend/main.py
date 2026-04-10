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
    # ── Phase 1: critical path (must finish before /health responds) ──
    # Only DB init lives here. Everything else starts in the background
    # so the Tauri health check passes immediately and the frontend can
    # connect. Subsystems that aren't ready yet simply no-op (classifier
    # returns no labels, summarizer returns no descriptions, scanner
    # waits for go2rtc internally).
    conn = await db.init_db()
    app.state.db = conn

    from .api.ws import EventBus
    event_bus = EventBus()
    app.state.event_bus = event_bus

    # Pre-set subsystem slots to None so API endpoints can check
    # readiness with getattr() instead of crashing on AttributeError.
    app.state.capability = None
    app.state.scanner = None
    app.state.recorder = None
    app.state.classifier = None
    app.state.motion = None
    app.state.audio = None

    # ── Phase 2: background startup (non-blocking) ──
    # Everything after this point runs as a background task. The FastAPI
    # server is already accepting requests, so the Tauri health check
    # passes and the frontend loads while cameras, classifier, and
    # summarizer spin up in the background.
    async def _background_startup():
        _log = __import__("logging").getLogger(__name__)

        try:
            # Hardware capability probe (cached on warm boot, ~1s cold).
            from .classification import capability_probe
            try:
                report = await capability_probe.run_and_persist(conn)
                app.state.capability = report
            except Exception as e:
                _log.error(
                    "capability probe failed, classifier will stay disabled: %s",
                    e, exc_info=True,
                )
                app.state.capability = None

            # go2rtc: dev mode may need to spawn it; Tauri mode already did.
            from . import dev_go2rtc
            await dev_go2rtc.ensure_running()
            if go2rtc_client.is_enabled():
                await go2rtc_client.wait_for_ready(timeout_s=15.0)

            from .discovery.scanner import DiscoveryScanner
            from .recording.manager import RecordingManager
            from .motion.manager import MotionManager

            scanner = DiscoveryScanner(conn, event_bus)
            recorder = RecordingManager(conn, event_bus)

            # Classifier — reads cached probe verdict, loads ONNX model.
            from .classification.manager import ClassificationManager
            tier = (await db.get_setting(conn, "classification_tier")) or "disabled"
            ep = (await db.get_setting(conn, "classification_ep")) or "none"
            classifier = ClassificationManager(conn, event_bus, tier=tier, ep=ep)
            try:
                await classifier.start()
            except Exception as e:
                _log.error(
                    "classifier manager start failed, staying disabled: %s", e, exc_info=True,
                )

            motion = MotionManager(conn, event_bus, recorder, classifier=classifier)

            # Audio classifier (YAMNet). Lightweight CPU inference, no tiering.
            from .audio.manager import AudioManager
            audio = AudioManager(conn, event_bus, recorder)
            try:
                await audio.start()
            except Exception as e:
                _log.error(
                    "audio manager start failed, staying disabled: %s", e, exc_info=True,
                )

            # Stash references for shutdown and API access.
            app.state.scanner = scanner
            app.state.recorder = recorder
            app.state.classifier = classifier
            app.state.motion = motion
            app.state.audio = audio

            app.state._scan_task = asyncio.create_task(scanner.run_forever())
            app.state._recorder_task = asyncio.create_task(recorder.run_forever())
            app.state._motion_task = asyncio.create_task(motion.run_forever())
            app.state._audio_task = asyncio.create_task(audio.run_forever())

            _log.info("background startup complete")
        except Exception as e:
            _log.error("background startup FAILED: %s", e, exc_info=True)

    startup_task = asyncio.create_task(_background_startup())
    app.state._startup_task = startup_task

    yield

    # Shutdown — wait for background startup to finish first (if still
    # running), then tear down in reverse order.
    startup_task = getattr(app.state, "_startup_task", None)
    if startup_task and not startup_task.done():
        startup_task.cancel()
        with suppress(asyncio.CancelledError):
            await startup_task

    # Subsystems may not exist if startup was cancelled early.
    recorder_task = getattr(app.state, "_recorder_task", None)
    recorder = getattr(app.state, "recorder", None)
    motion_task = getattr(app.state, "_motion_task", None)
    motion = getattr(app.state, "motion", None)
    audio_task = getattr(app.state, "_audio_task", None)
    audio = getattr(app.state, "audio", None)
    classifier = getattr(app.state, "classifier", None)
    scan_task = getattr(app.state, "_scan_task", None)

    if recorder_task:
        recorder_task.cancel()
        with suppress(asyncio.CancelledError):
            await recorder_task
    if recorder:
        await recorder.shutdown()

    if motion_task:
        motion_task.cancel()
        with suppress(asyncio.CancelledError):
            await motion_task
    if motion:
        await motion.shutdown()

    if audio_task:
        audio_task.cancel()
        with suppress(asyncio.CancelledError):
            await audio_task
    if audio:
        await audio.shutdown()

    if classifier:
        await classifier.shutdown()

    if scan_task:
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
    # In PyInstaller onefile builds the module may already be loaded
    # under a different sys.modules key (e.g. via _pyi_entry →
    # backend.main). Ensure uvicorn's import of "backend.main" resolves
    # to THIS module — the one where `app` already has CORS middleware
    # attached — rather than creating a second bare instance.
    import sys as _sys
    _sys.modules.setdefault("backend.main", _sys.modules[__name__])
    uvicorn.run(
        "backend.main:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
        timeout_graceful_shutdown=35,
    )


if __name__ == "__main__":
    _launch_sidecar()
