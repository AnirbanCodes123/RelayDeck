import subprocess
from urllib.parse import quote
from unittest.mock import MagicMock

import pytest

from scripts import vlc_launcher


def link(url):
    return "relaydeck-vlc://play?url=" + quote(url, safe="")


@pytest.mark.parametrize("url", [
    "rtsp://192.168.29.20:8554/mcl_in_744-745_13",
    "rtsp://user:p%40ss@[2001:db8::1]:8554/feed?token=a%2Bb&x=$(id);test",
])
def test_launches_vlc_explicitly_with_original_url_as_single_argument(monkeypatch, url):
    spawn = MagicMock()
    monkeypatch.setattr(vlc_launcher, "vlc_command", lambda: ["/snap/bin/vlc"])
    monkeypatch.setattr(vlc_launcher.subprocess, "Popen", spawn)
    assert vlc_launcher.main([link(url)]) == 0
    spawn.assert_called_once_with(
        ["/snap/bin/vlc", "--no-one-instance", "--rtsp-tcp", url], start_new_session=True,
    )


@pytest.mark.parametrize("uri", [
    "rtsp://localhost:8554/camera", "relaydeck-vlc://other?url=rtsp://host/camera",
    link("https://example.com/video"), link("file:///etc/passwd"),
    link("--extraintf=lua"), link("rtsp://host:99999/feed"),
    link("rtsp://host/feed\n--extraintf=lua"),
    link("rtsp://host/feed") + "&url=rtsp://other/feed",
])
def test_invalid_links_never_launch_a_process(monkeypatch, uri):
    spawn = MagicMock()
    monkeypatch.setattr(vlc_launcher.subprocess, "Popen", spawn)
    assert vlc_launcher.main([uri]) == 1
    spawn.assert_not_called()


def test_installs_persistent_desktop_handler_without_changing_rtsp_default(monkeypatch, tmp_path):
    monkeypatch.setattr(vlc_launcher.sys, "platform", "linux")
    monkeypatch.setattr(vlc_launcher.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "desktop data"))
    monkeypatch.setattr(vlc_launcher.shutil, "which", lambda name: {
        "vlc": "/usr/bin/vlc", "xdg-mime": "/usr/bin/xdg-mime",
    }.get(name))
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout="relaydeck-vlc.desktop\n"))
    monkeypatch.setattr(vlc_launcher.subprocess, "run", run)
    assert vlc_launcher.main(["--install"]) == 0
    data = tmp_path / "desktop data"
    desktop = (data / "applications/relaydeck-vlc.desktop").read_text()
    assert '"' + str(data / "relaydeck/vlc_launcher.py") + '" %u' in desktop
    assert "MimeType=x-scheme-handler/relaydeck-vlc;" in desktop
    assert (data / "relaydeck/vlc_launcher.py").read_bytes() == vlc_launcher.Path(vlc_launcher.__file__).read_bytes()
    assert run.call_args_list[0].args[0] == [
        "/usr/bin/xdg-mime", "default", "relaydeck-vlc.desktop", "x-scheme-handler/relaydeck-vlc",
    ]
    # Installation is repeatable and the installed copy outlives the checkout.
    assert vlc_launcher.main(["--install"]) == 0


def test_installer_refuses_root_before_writing(monkeypatch, tmp_path):
    monkeypatch.setattr(vlc_launcher.sys, "platform", "linux")
    monkeypatch.setattr(vlc_launcher.os, "geteuid", lambda: 0)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert vlc_launcher.main(["--install"]) == 1
    assert not (tmp_path / "data").exists()
