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
