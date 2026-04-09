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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    conn = await db.init_db()
    app.state.db = conn

    # Block until go2rtc's admin API is reachable, so the discovery
    # scanner can register every camera as part of its own startup
    # without racing the sidecar. The Tauri shell already waits for
    # go2rtc readiness before spawning us in production builds, so
    # this usually returns true on the first probe; the wait is
    # belt-and-suspenders for dev mode (manual `python -m backend.main`
    # after starting go2rtc separately) and for the case where the
    # Tauri shell's own readiness budget was tighter than ours. If
    # go2rtc never answers, we continue with direct camera URLs —
    # SimpleNVR keeps recording in fallback mode.
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
    motion = MotionManager(conn, event_bus, recorder)

    app.state.event_bus = event_bus
    app.state.scanner = scanner
    app.state.recorder = recorder
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
]
if os.environ.get("SIMPLENVR_DEV") == "1":
    _allowed_origins += [
        "http://localhost:1420",
        "http://localhost:5173",
        "http://127.0.0.1:1420",
        "http://127.0.0.1:5173",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)

# Register routes
from .api.cameras import router as cameras_router  # noqa: E402
from .api.recordings import router as recordings_router  # noqa: E402
from .api.settings import router as settings_router  # noqa: E402
from .api.streams import router as streams_router  # noqa: E402
from .api.motion import router as motion_router  # noqa: E402
from .api.ws import router as ws_router  # noqa: E402

app.include_router(cameras_router, prefix="/api")
app.include_router(recordings_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(streams_router, prefix="/api")
app.include_router(motion_router, prefix="/api")
app.include_router(ws_router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import json
    import os
    import sys
    import threading
    import uvicorn
    from .port_finder import pick_port_with_socket

    port, sock = pick_port_with_socket()

    # Signal to the Tauri Rust shell that we chose a port. Must be printed
    # BEFORE uvicorn.run() blocks.
    print(json.dumps({"port": port, "ready": True}), flush=True)
    sys.stdout.flush()

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
    if os.environ.get("SIMPLENVR_STDIN_WATCHDOG") == "1":
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

    # Hand the pre-bound socket to uvicorn via fd= to close the TOCTOU window.
    uvicorn.run(
        "backend.main:app",
        host=None,
        fd=sock.fileno(),
        log_level="warning",
    )
