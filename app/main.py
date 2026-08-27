from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import aiofiles
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.store import StreamStore
from app.streamer import (
    StreamManager,
    cpu_status,
    nvidia_gpu_status,
    probe_processing_mode,
    resolve_processing_mode,
)
from app.metrics import monitor as host_monitor

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "app" / "static"
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", DATA_DIR / "uploads"))
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", DATA_DIR / "relaydeck.db"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", 20 * 1024**3))
MAX_STREAMS = int(os.getenv("MAX_STREAMS", "80"))
ALLOWED_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".webm",
    ".m4v",
    ".ts",
    ".mpeg",
    ".mpg",
}

manager = StreamManager(
    mediamtx_host=os.getenv("MEDIAMTX_HOST", "127.0.0.1"),
    mediamtx_port=int(os.getenv("MEDIAMTX_RTSP_PORT", "8554")),
    public_host=os.getenv("RTSP_PUBLIC_HOST", "localhost"),
    max_streams=MAX_STREAMS,
)
store = StreamStore(DATABASE_PATH)


@asynccontextmanager
async def lifespan(_: FastAPI):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    store.initialize()
    for record in store.list():
        video_path = Path(record["video_path"])
        if video_path.is_file():
            manager.register(
                record["id"],
                video_path,
                record["original_filename"],
                record["stream_name"],
                record["loop"],
                record["processing_mode"],
            )
    restore_task = asyncio.create_task(_restore_desired_streams())
    host_monitor.start()
    await asyncio.to_thread(nvidia_gpu_status)
    yield
    restore_task.cancel()
    host_monitor.stop()
    manager.shutdown()


app = FastAPI(title="RelayDeck", version="2.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def mediamtx_available() -> bool:
    try:
        with socket.create_connection(
            (manager.mediamtx_host, manager.mediamtx_port), timeout=0.5
        ):
            return True
    except OSError:
        return False


async def _restore_desired_streams() -> None:
    desired_ids = [
        record["id"] for record in store.list() if record["desired_running"]
    ]
    if not desired_ids:
        return
    for _ in range(30):
        if mediamtx_available():
            break
        await asyncio.sleep(1)
    else:
        return
    for offset in range(0, len(desired_ids), 5):
        batch = desired_ids[offset : offset + 5]
        await asyncio.gather(
            *(asyncio.to_thread(manager.start, stream_id) for stream_id in batch),
            return_exceptions=True,
        )
        await asyncio.sleep(0.25)


def services() -> dict:
    return {
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "mediamtx": mediamtx_available(),
        "cpu": cpu_status(),
        "nvidia": nvidia_gpu_status(),
    }


def require_stream(stream_id: str, include_logs: bool = True) -> dict:
    try:
        return manager.get(stream_id, include_logs=include_logs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Stream not found.") from exc


@app.get("/api/status")
async def stream_status() -> dict:
    return {
        "streams": manager.list(),
        "aggregate": manager.aggregate(),
        "services": await asyncio.to_thread(services),
    }


@app.get("/api/streams")
async def list_streams() -> dict:
    return await stream_status()


@app.post("/api/streams")
async def start_stream(
    video: UploadFile = File(...),
    stream_name: str = Form("camera1"),
    loop: bool = Form(True),
    start_immediately: bool = Form(True),
    transcode_engine: str = Form("auto"),
) -> dict:
    stream_name = stream_name.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", stream_name):
        raise HTTPException(
            status_code=422,
            detail="Stream name must use 1–64 letters, numbers, hyphens, or underscores.",
        )
    if transcode_engine not in {"auto", "cpu", "nvidia"}:
        raise HTTPException(
            status_code=422,
            detail="Transcode engine must be auto, cpu, or nvidia.",
        )

    original_name = Path(video.filename or "video").name
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported video type. Use: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )
    if manager.aggregate()["total"] >= MAX_STREAMS:
        raise HTTPException(
            status_code=409,
            detail=f"Stream capacity reached ({MAX_STREAMS}). Delete a stream first.",
        )

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stream_id = uuid.uuid4().hex
    destination = UPLOAD_DIR / f"{stream_id}{extension}"
    total_bytes = 0
    try:
        async with aiofiles.open(destination, "wb") as output:
            while chunk := await video.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="The selected video exceeds the upload size limit.",
                    )
                await output.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await video.close()

    try:
        media_mode = await asyncio.to_thread(probe_processing_mode, destination)
        processing_mode = await asyncio.to_thread(
            resolve_processing_mode, media_mode, transcode_engine
        )
        gpu_fallback = (
            media_mode == "transcode"
            and transcode_engine == "nvidia"
            and processing_mode == "cpu"
        )
        state = manager.register(
            stream_id,
            destination,
            original_name,
            stream_name,
            loop,
            processing_mode,
        )
        store.create(
            stream_id,
            original_name,
            destination,
            stream_name,
            loop,
            processing_mode,
            desired_running=start_immediately,
        )
    except RuntimeError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        try:
            manager.unregister(stream_id)
        except KeyError:
            pass
        destination.unlink(missing_ok=True)
        raise

    if start_immediately and mediamtx_available():
        try:
            state = manager.start(stream_id)
        except RuntimeError as exc:
            state = manager.get(stream_id)
            state["error"] = str(exc)

    state["requested_engine"] = transcode_engine
    state["gpu_fallback"] = gpu_fallback
    return state


@app.get("/api/streams/{stream_id}")
async def stream_detail(stream_id: str) -> dict:
    return require_stream(stream_id)


@app.post("/api/streams/{stream_id}/start")
async def start_existing_stream(stream_id: str) -> dict:
    require_stream(stream_id, include_logs=False)
    if not mediamtx_available():
        raise HTTPException(status_code=503, detail="MediaMTX is not reachable.")
    try:
        state = manager.start(stream_id)
        store.set_desired_running(stream_id, True)
        return state
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/streams/{stream_id}/stop")
async def stop_stream(stream_id: str) -> dict:
    require_stream(stream_id, include_logs=False)
    store.set_desired_running(stream_id, False)
    return await asyncio.to_thread(manager.stop, stream_id)


@app.delete("/api/streams/{stream_id}")
async def delete_stream(stream_id: str) -> dict:
    require_stream(stream_id, include_logs=False)
    stream = await asyncio.to_thread(manager.unregister, stream_id)
    store.delete(stream_id)
    stream.video_path.unlink(missing_ok=True)
    return {"deleted": stream_id}


@app.post("/api/streams/actions/start-all")
async def start_all_streams() -> dict:
    if not mediamtx_available():
        raise HTTPException(status_code=503, detail="MediaMTX is not reachable.")
    store.set_all_desired_running(True)
    await asyncio.to_thread(manager.start_all)
    return await stream_status()


@app.post("/api/streams/actions/stop-all")
async def stop_all_streams() -> dict:
    store.set_all_desired_running(False)
    await asyncio.to_thread(manager.stop_all)
    return await stream_status()


@app.get("/api/health")
async def health() -> dict:
    current_services = services()
    return {
        "ok": current_services["ffmpeg"] and current_services["mediamtx"],
        **current_services,
        "aggregate": manager.aggregate(),
    }


@app.get("/api/metrics")
async def host_metrics() -> dict:
    return host_monitor.snapshot()
