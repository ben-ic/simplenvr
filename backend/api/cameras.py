from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

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


@router.delete("/cameras/{camera_id}/auth", response_model=Camera)
async def logout_camera(camera_id: str, request: Request):
    """Clear credentials and revert to needs_auth.

    Stops the recorder, removes the go2rtc stream, wipes username +
    password + RTSP URIs from the DB. The camera stays in the list
    (unlike DELETE /cameras/{id} which removes it entirely) so the
    user can re-authenticate without re-discovering.
    """
    conn = request.app.state.db
    camera = await db.get_camera(conn, camera_id)
    if not camera:
        return JSONResponse(status_code=404, content={"detail": "Camera not found"})

    # Stop recording first
    recorder_mgr = request.app.state.recorder
    try:
        await recorder_mgr.stop_recording(camera_id)
    except Exception:
        pass

    # Remove from go2rtc
    from .. import go2rtc_client
    if go2rtc_client.is_enabled():
        try:
            await go2rtc_client.remove_stream(camera_id)
        except Exception:
            pass
        try:
            await go2rtc_client.remove_stream(f"{camera_id}_sub")
        except Exception:
            pass

    updated = await db.clear_camera_auth(conn, camera_id)

    # Update the scanner's in-memory copy
    scanner = request.app.state.scanner
    if updated and updated.ip in scanner._known_cameras:
        scanner._known_cameras[updated.ip] = updated

    event_bus = request.app.state.event_bus
    await event_bus.emit("camera_updated", {"camera": updated.model_dump(mode="json")})
    return updated


@router.post("/cameras/{camera_id}/retry-main-stream", response_model=Camera)
async def retry_main_stream(camera_id: str, request: Request):
    """Clear the circuit breaker's auto-fallback for this camera.

    After CIRCUIT_BREAKER_THRESHOLD split-brain restarts in 10 minutes,
    the recorder auto-flips the camera to its sub-stream and writes
    fallback_reason. This endpoint clears both fields and restarts the
    recorder so it picks the main stream again. If the underlying bug
    hasn't been fixed, the breaker will trip and fall back to sub
    again on its own — this endpoint is safe to call unconditionally.

    No UI surfaces this in the initial commit; it's API-only until the
    frontend follow-up lands.
    """
    conn = request.app.state.db
    camera = await db.get_camera(conn, camera_id)
    if not camera:
        return JSONResponse(status_code=404, content={"detail": "Camera not found"})

    updated = await db.clear_stream_override(conn, camera_id)
    if updated is None:
        return JSONResponse(status_code=404, content={"detail": "Camera not found"})

    # Bounce the recorder so it rebuilds against the fresh camera row.
    # stop+start is the existing pattern for "pick up new camera fields"
    # (see logout_camera above for the auth equivalent). If the camera
    # isn't currently recording, the stop is a no-op and the start only
    # fires when status=online.
    recorder_mgr = request.app.state.recorder
    try:
        await recorder_mgr.stop_recording(camera_id)
    except Exception:
        pass
    if updated.status == "online" and updated.rtsp_uri:
        try:
            await recorder_mgr.start_recording(updated)
        except Exception:
            pass

    event_bus = request.app.state.event_bus
    await event_bus.emit("camera_updated", {"camera": updated.model_dump(mode="json")})
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


@router.get("/cameras/{camera_id}/snapshot.jpg")
async def get_snapshot(camera_id: str, request: Request):
    """Serve the latest motion-detection JPEG frame for a camera.

    Zero-cost: just returns the frame already cached in the recorder's
    motion broadcaster. No new RTSP connection, no new decode.
    Returns 503 if the recorder isn't running yet, 404 if no frame.
    """
    recorder_mgr = getattr(request.app.state, "recorder", None)
    if recorder_mgr is None:
        return JSONResponse(status_code=503, content={"detail": "starting"})
    recorder = recorder_mgr.recorders.get(camera_id)
    if not recorder or not recorder.is_running:
        return JSONResponse(status_code=404, content={"detail": "not recording"})
    frame = recorder.motion_broadcaster.latest
    if frame is None:
        return JSONResponse(status_code=404, content={"detail": "no frame yet"})
    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache, max-age=2"},
    )


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
    if scanner is None:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"detail": "backend starting up"})
    await scanner.run_scan()
    return {"status": "scan_complete"}


@router.get("/scan/status", response_model=ScanStatus)
async def scan_status(request: Request):
    scanner = request.app.state.scanner
    if scanner is None:
        return ScanStatus(scanning=False, last_scan=None, cameras_found=0)
    return scanner.get_status()
