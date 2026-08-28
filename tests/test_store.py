from pathlib import Path

from app.store import StreamStore


def test_store_persists_and_updates_streams(tmp_path: Path) -> None:
    database = tmp_path / "relaydeck.db"
    upload = tmp_path / "camera.mp4"
    upload.touch()
    store = StreamStore(database)
    store.initialize()

    created = store.create(
        "stream-id",
        "camera.mp4",
        upload,
        "camera1",
        True,
        "copy",
    )
    assert created["stream_name"] == "camera1"
    assert created["desired_running"] is False

    reopened = StreamStore(database)
    reopened.initialize()
    assert reopened.get("stream-id")["video_path"] == str(upload)

    reopened.set_desired_running("stream-id", True)
    assert reopened.get("stream-id")["desired_running"] is True

    deleted = reopened.delete("stream-id")
    assert deleted["id"] == "stream-id"
    assert reopened.list() == []


def test_store_delete_all_removes_every_stream(tmp_path: Path) -> None:
    database = tmp_path / "relaydeck.db"
    store = StreamStore(database)
    store.initialize()
    first = tmp_path / "one.mp4"
    second = tmp_path / "two.mp4"
    first.touch()
    second.touch()
    store.create("one", "one.mp4", first, "camera1", True, "copy")
    store.create("two", "two.mp4", second, "camera2", True, "nvidia")
    removed = store.delete_all()
    assert len(removed) == 2
    assert store.list() == []
