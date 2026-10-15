from pathlib import Path
from unittest.mock import MagicMock

import pytest

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


@pytest.mark.parametrize(
    ("base_url", "override", "expected_host"),
    [
        ("http://192.168.1.50:9009", "", "192.168.1.50"),
        ("http://10.0.0.8:9009", "", "10.0.0.8"),
        ("http://localhost:9009", "192.168.1.50", "192.168.1.50"),
        ("http://[2001:db8::1]:9009", "", "[2001:db8::1]"),
    ],
)
def test_public_rtsp_urls_use_request_host_or_override(
    monkeypatch, tmp_path: Path, base_url: str, override: str, expected_host: str
) -> None:
    configure_test_runtime(monkeypatch, tmp_path)
    main.manager.public_host = override
    with TestClient(main.app, base_url=base_url) as client:
        video = tmp_path / "camera.mp4"
        video.touch()
        main.manager.register("one", video, "camera.mp4", "mcl1", True, "copy")
        main.store.create("one", "camera.mp4", video, "mcl1", True, "copy")
        expected_url = f"rtsp://{expected_host}:8554/mcl1"
        for path in ("/api/streams", "/api/status"):
            assert client.get(path).json()["streams"][0]["rtsp_url"] == expected_url
        assert client.get("/api/streams/one").json()["rtsp_url"] == expected_url
        assert client.post("/api/streams/one/stop").json()["rtsp_url"] == expected_url
        assert client.post("/api/streams/actions/stop-all").json()["streams"][0]["rtsp_url"] == expected_url
        # Requests from another address must not change the publisher target
        # or leak into a later response for the original address.
        if not override:
            other = client.get("http://10.0.0.9:9009/api/streams").json()
            assert other["streams"][0]["rtsp_url"] == "rtsp://10.0.0.9:8554/mcl1"
            assert client.get("/api/streams/one").json()["rtsp_url"] == expected_url
        assert main.manager.mediamtx_host == "127.0.0.1"


@pytest.mark.parametrize("action", ["pause", "resume", "forward"])
def test_playback_routes_control_shared_stream_and_preserve_public_url(monkeypatch, tmp_path, action):
    configure_test_runtime(monkeypatch, tmp_path)
    main.manager.public_host = ""
    state = main.manager.register("one", tmp_path / "one.mp4", "one.mp4", "camera1", True, "copy")
    control = MagicMock(return_value=state)
    monkeypatch.setattr(main.manager, "playback_action", control)
    with TestClient(main.app, base_url="http://192.168.29.20:9009") as client:
        response = client.post(f"/api/streams/one/playback/{action}")
    assert response.status_code == 200
    assert response.json()["rtsp_url"] == "rtsp://192.168.29.20:8554/camera1"
    control.assert_called_once_with("one", action)


def test_playback_rejects_stopped_missing_and_invalid_controls(monkeypatch, tmp_path):
    configure_test_runtime(monkeypatch, tmp_path)
    main.manager.register("one", tmp_path / "one.mp4", "one.mp4", "camera1", True, "copy")
    with TestClient(main.app) as client:
        assert client.post("/api/streams/one/playback/pause").status_code == 409
        assert client.post("/api/streams/missing/playback/pause").status_code == 404
        assert client.post("/api/streams/one/playback/invalid").status_code == 422
