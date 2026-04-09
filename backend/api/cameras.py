from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import db
from ..models import (
    Camera,
    CameraAuthRequest,
    CameraNameRequest,
    ManualCameraRequest,
    ScanStatus,
)

router = APIRouter(tags=["cameras"])


@router.get("/cameras", response_model=list[Camera])
async def list_cameras(request: Request):
    cameras = await db.get_all_cameras(request.app.state.db)
    return cameras


@router.get("/cameras/{camera_id}", response_model=Camera)
async def get_camera(camera_id: str, request: Request):
    camera = await db.get_camera(request.app.state.db, camera_id)
    if not camera:
        return JSONResponse(status_code=404, content={"detail": "Camera not found"})
    return camera


@router.post("/cameras/{camera_id}/auth", response_model=Camera)
async def submit_auth(camera_id: str, body: CameraAuthRequest, request: Request):
    conn = request.app.state.db
    scanner = request.app.state.scanner

    camera = await db.get_camera(conn, camera_id)
    if not camera:
        return JSONResponse(status_code=404, content={"detail": "Camera not found"})

    updated = await scanner.authenticate_camera(
        camera, body.username, body.password, body.apply_to_manufacturer
    )
    return updated


@router.post("/cameras/{camera_id}/name", response_model=Camera)
async def set_name(camera_id: str, body: CameraNameRequest, request: Request):
    camera = await db.update_camera_name(
        request.app.state.db, camera_id, body.name
    )
    if not camera:
        return JSONResponse(status_code=404, content={"detail": "Camera not found"})

    event_bus = request.app.state.event_bus
    await event_bus.emit("camera_updated", {"camera": camera.model_dump(mode="json")})
    return camera


@router.delete("/cameras/{camera_id}")
async def delete_camera(camera_id: str, request: Request):
    """Permanently remove a camera.

    Stops any live recorder, unregisters from go2rtc, drops the row
    from the scanner's in-memory known-camera set so the next scan
    doesn't re-discover it under the same identity, then deletes
    the DB row and all dependent motion_events / tracked_events /
    recordings rows.

    This is a user-facing affordance: a camera that is permanently
    offline, moved to a different network, or mis-detected should
    be removable from the UI instead of ghost-haunting the Cameras
    list as an un-authable "Needs Login" row forever.
    """
    conn = request.app.state.db
    camera = await db.get_camera(conn, camera_id)
    if not camera:
        return JSONResponse(
            status_code=404, content={"detail": "Camera not found"}
        )

    # Stop the recorder first so ffmpeg releases the RTSP slot and
    # the camera's dedicated broadcasters sentinel their subscribers.
    # Safe to call even if no recorder is running — stop_recording
    # is a no-op in that case.
    recorder_mgr = request.app.state.recorder
    try:
        await recorder_mgr.stop_recording(camera_id)
    except Exception:
        pass

    # Drop from go2rtc if registered. Non-fatal on failure — the DB
    # row still needs to be deleted either way.
    from .. import go2rtc_client
    if go2rtc_client.is_enabled():
        try:
            await go2rtc_client.remove_stream(camera_id)
        except Exception:
            pass

    # Drop from the scanner's in-memory known-camera set so the next
    # scan pass doesn't immediately re-discover the camera and
    # re-insert it into the DB with a new UUID. The scanner's
    # `existing = self._known_cameras.get(ep.ip)` check is the
    # deduper for already-known devices; removing the entry makes
    # the deleted camera look "new" again on next discovery, which
    # is the correct behavior if it's still on the network (the
    # user explicitly removed it, so re-adding it means re-entering
    # credentials fresh).
    scanner = request.app.state.scanner
    scanner._known_cameras.pop(camera.ip, None)

    # Finally, delete the DB row and its dependents.
    await db.delete_camera(conn, camera_id)

    # Broadcast the deletion so the frontend can remove the row
    # from its cameras Map without a refetch.
    event_bus = request.app.state.event_bus
    await event_bus.emit("camera_deleted", {"camera_id": camera_id})

    return {"ok": True, "camera_id": camera_id}


@router.post("/cameras/manual", response_model=Camera)
async def add_camera_manually(body: ManualCameraRequest, request: Request):
    """
    Escape-hatch camera add when auto-discovery didn't find the camera.

    The user provides IP + port + credentials (and optionally a brand
    hint and RTSP path); we probe known RTSP path patterns until one
    yields a valid stream, then create a Camera row with
    identification_source="manual". Returns 400 with a human-readable
    error message on failure so the frontend can surface it inline.
    """
    scanner = request.app.state.scanner
    try:
        camera = await scanner.manually_add_camera(
            ip=body.ip,
            port=body.port,
            username=body.username,
            password=body.password,
            path=body.path,
            brand=body.brand,
            name=body.name,
        )
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    return camera


@router.post("/scan")
async def trigger_scan(request: Request):
    scanner = request.app.state.scanner
    await scanner.run_scan()
    return {"status": "scan_complete"}


@router.get("/scan/status", response_model=ScanStatus)
async def scan_status(request: Request):
    scanner = request.app.state.scanner
    return scanner.get_status()
