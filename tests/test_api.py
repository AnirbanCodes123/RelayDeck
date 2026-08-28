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


def test_metrics_endpoint_returns_host_snapshot(monkeypatch, tmp_path: Path) -> None:
    configure_test_runtime(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        response = client.get("/api/metrics")
    assert response.status_code == 200
    payload = response.json()
    assert "cpu" in payload
    assert "memory" in payload
    assert "gpu" in payload
    assert "percent" in payload["cpu"]
    assert "edge_agent" in payload
    assert payload["edge_agent"]["port"] == 9000
    assert "cpu" in payload["edge_agent"]
    assert "memory" in payload["edge_agent"]
    assert "gpu" in payload["edge_agent"]
    assert "percent" in payload["edge_agent"]["cpu"]


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


def test_clear_all_removes_streams_and_uploads(monkeypatch, tmp_path: Path) -> None:
    configure_test_runtime(monkeypatch, tmp_path)
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    first = upload_dir / "one.mp4"
    second = upload_dir / "two.mp4"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    main.store.initialize()
    main.store.create("one", "one.mp4", first, "camera1", True, "copy")
    main.store.create("two", "two.mp4", second, "camera2", True, "nvidia")
    with TestClient(main.app) as client:
        listed = client.get("/api/streams")
        assert listed.json()["aggregate"]["total"] == 2
        response = client.post("/api/streams/actions/clear-all")
    assert response.status_code == 200
    payload = response.json()
    assert payload["deleted"] == 2
    assert payload["aggregate"]["total"] == 0
    assert not first.exists()
    assert not second.exists()
    assert main.store.list() == []
    assert main.manager.list() == []
