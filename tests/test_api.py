from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main
from app.store import StreamStore
from app.streamer import StreamManager


def configure_test_runtime(monkeypatch, tmp_path: Path, capacity: int = 2) -> None:
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(main, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(main, "MAX_STREAMS", capacity)
    monkeypatch.setattr(main, "store", StreamStore(tmp_path / "relaydeck.db"))
    monkeypatch.setattr(main, "manager", StreamManager(max_streams=capacity))


def test_list_endpoint_returns_aggregate(monkeypatch, tmp_path: Path) -> None:
    configure_test_runtime(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        response = client.get("/api/streams")
    assert response.status_code == 200
    assert response.json()["aggregate"]["total"] == 0
    assert response.json()["aggregate"]["capacity"] == 2


def test_upload_rejects_invalid_endpoint(monkeypatch, tmp_path: Path) -> None:
    configure_test_runtime(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        response = client.post(
            "/api/streams",
            data={"stream_name": "bad endpoint!"},
            files={"video": ("camera.mp4", b"not-read", "video/mp4")},
        )
    assert response.status_code == 422


def test_upload_rejects_when_registry_is_full(monkeypatch, tmp_path: Path) -> None:
    configure_test_runtime(monkeypatch, tmp_path, capacity=1)
    video = tmp_path / "existing.mp4"
    video.touch()
    main.manager.register("one", video, "existing.mp4", "camera1", True, "copy")
    with TestClient(main.app) as client:
        response = client.post(
            "/api/streams",
            data={"stream_name": "camera2"},
            files={"video": ("camera2.mp4", b"not-read", "video/mp4")},
        )
    assert response.status_code == 409
    assert "capacity" in response.json()["detail"].lower()
