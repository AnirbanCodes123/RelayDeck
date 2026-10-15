"""Install or run the Ubuntu desktop handler for RelayDeck's VLC buttons."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit


SCHEME = "relaydeck-vlc"
DESKTOP_ID = "relaydeck-vlc.desktop"


def stream_url(uri: str) -> str:
    outer = urlsplit(uri)
    if outer.scheme != SCHEME or outer.netloc != "play" or outer.path not in {"", "/"} or outer.fragment:
        raise ValueError("Invalid RelayDeck VLC link.")
    query = parse_qs(outer.query, strict_parsing=True)
    if set(query) != {"url"} or len(query["url"]) != 1:
        raise ValueError("The VLC link must contain exactly one stream URL.")
    url = query["url"][0]
    parsed = urlsplit(url)
    if parsed.scheme != "rtsp" or not parsed.hostname or parsed.fragment:
        raise ValueError("Only RTSP stream URLs can be opened.")
    if any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ValueError("The stream URL contains invalid whitespace or control characters.")
    # Accessing port validates malformed or out-of-range port numbers too.
    _ = parsed.port
    return url


def vlc_command() -> list[str]:
    executable = shutil.which("vlc")
    if executable:
        return [executable]
    if Path("/snap/bin/vlc").is_file():
        return ["/snap/bin/vlc"]
    flatpak = shutil.which("flatpak")
    if flatpak and subprocess.run(
        [flatpak, "info", "org.videolan.VLC"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0:
        return [flatpak, "run", "org.videolan.VLC"]
    raise RuntimeError("VLC is not installed. Install VLC on the desktop running your browser first.")


def desktop_argument(value: str) -> str:
    # Desktop Exec uses its own quoting rules, not shell quoting. First quote
    # the argument, then escape backslashes for the desktop-entry string layer.
    escaped = "".join("\\" + char if char in '\\"`$' else char for char in value)
    return '"' + escaped.replace("\\", "\\\\").replace("%", "%%") + '"'


def install() -> None:
    if sys.platform != "linux":
        raise RuntimeError("This installer is for Ubuntu/Linux desktops.")
    if os.geteuid() == 0:
        raise RuntimeError("Run without sudo, as the desktop user who runs the browser.")
    vlc_command()
    xdg_mime = shutil.which("xdg-mime")
    if not xdg_mime:
        raise RuntimeError("xdg-mime is missing. Install the xdg-utils package first.")
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    launcher = data_home / "relaydeck" / "vlc_launcher.py"
    applications = data_home / "applications"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    applications.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve()
    if source != launcher.resolve():
        shutil.copyfile(source, launcher)
    python = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable
    desktop = applications / DESKTOP_ID
    desktop.write_text(
        "[Desktop Entry]\nType=Application\nName=RelayDeck — Open in VLC\n"
        "Comment=Play RelayDeck RTSP streams in a VLC window\n"
        f"Exec={desktop_argument(python)} {desktop_argument(str(launcher))} %u\n"
        "Icon=vlc\nTerminal=false\nNoDisplay=true\n"
        f"MimeType=x-scheme-handler/{SCHEME};\n",
        encoding="utf-8",
    )
    update_database = shutil.which("update-desktop-database")
    if update_database:
        subprocess.run([update_database, str(applications)], check=True)
    subprocess.run([xdg_mime, "default", DESKTOP_ID, f"x-scheme-handler/{SCHEME}"], check=True)
    result = subprocess.run(
        [xdg_mime, "query", "default", f"x-scheme-handler/{SCHEME}"],
        capture_output=True, text=True, check=True,
    )
    if result.stdout.strip() != DESKTOP_ID:
        raise RuntimeError("Desktop registration was not confirmed. Run this installer in your desktop session.")
    print("Installed RelayDeck's VLC handler. The VLC buttons now select VLC explicitly.")
    print("If the browser asks permission, allow RelayDeck to open VLC and remember the choice if offered.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Register the handler for your desktop user")
    parser.add_argument("uri", nargs="?", help="relaydeck-vlc:// link supplied by the browser")
    args = parser.parse_args(argv)
    if args.install and args.uri:
        parser.error("Use --install or a stream link, not both.")
    if not args.install and not args.uri:
        parser.error("Provide --install or a stream link.")
    try:
        if args.install:
            install()
        else:
            url = stream_url(args.uri)
            # No shell: credentials, query strings, and metacharacters remain
            # one URL argument. Force a new VLC window and RTSP-over-TCP.
            subprocess.Popen(vlc_command() + ["--no-one-instance", "--rtsp-tcp", url],
                             start_new_session=True)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"RelayDeck VLC: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
