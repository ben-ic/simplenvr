from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import db


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    conn = await db.init_db()
    app.state.db = conn

    # Import here to avoid circular imports at module level
    from .api.ws import EventBus
    from .discovery.scanner import DiscoveryScanner
    from .recording.manager import RecordingManager

    event_bus = EventBus()
    scanner = DiscoveryScanner(conn, event_bus)
    recorder = RecordingManager(conn, event_bus)

    app.state.event_bus = event_bus
    app.state.scanner = scanner
    app.state.recorder = recorder

    scan_task = asyncio.create_task(scanner.run_forever())
    recorder_task = asyncio.create_task(recorder.run_forever())

    yield

    # Shutdown — stop recorder first so segments finalize before DB closes
    recorder_task.cancel()
    with suppress(asyncio.CancelledError):
        await recorder_task
    await recorder.shutdown()

    scan_task.cancel()
    with suppress(asyncio.CancelledError):
        await scan_task
    await conn.close()


app = FastAPI(title="SimpleNVR", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routes
from .api.cameras import router as cameras_router  # noqa: E402
from .api.recordings import router as recordings_router  # noqa: E402
from .api.settings import router as settings_router  # noqa: E402
from .api.streams import router as streams_router  # noqa: E402
from .api.ws import router as ws_router  # noqa: E402

app.include_router(cameras_router, prefix="/api")
app.include_router(recordings_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(streams_router, prefix="/api")
app.include_router(ws_router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}
