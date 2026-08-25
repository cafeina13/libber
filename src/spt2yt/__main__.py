"""Entry point: `spt2yt` / `python -m spt2yt`."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
import webbrowser


def main() -> int:
    # Windows consoles default to a legacy codepage (cp1254 on a Turkish
    # system), which raises UnicodeEncodeError the moment a non-Latin-1 char
    # reaches stdout -- track titles, or even an arrow in this banner.
    for pipe in (sys.stdout, sys.stderr):
        try:
            pipe.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    parser = argparse.ArgumentParser(
        prog="spt2yt",
        description="Transfer Spotify playlists into offline Opus files via YouTube Music.",
    )
    parser.add_argument("--port", type=int, default=8765, help="local port (default 8765)")
    parser.add_argument("--output", help="download folder (default ~/Music/spt2yt)")
    parser.add_argument("--jobs", type=int, help="parallel downloads (default 3)")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser")
    args = parser.parse_args()

    if shutil.which("ffmpeg") is None:
        print(
            "ffmpeg isn't on PATH. Install it first:\n"
            "  winget install Gyan.FFmpeg.Essentials      (Windows)\n"
            "  brew install ffmpeg                        (macOS)\n"
            "  sudo apt install ffmpeg                    (Debian/Ubuntu)",
            file=sys.stderr,
        )
        return 1

    # Set before importing the app: module-level state reads these at import.
    os.environ["SPT2YT_PORT"] = str(args.port)
    if args.output:
        os.environ["SPT2YT_OUTPUT"] = args.output
    if args.jobs:
        os.environ["SPT2YT_CONCURRENCY"] = str(args.jobs)

    import uvicorn

    from .config import SERVER_HOST, redirect_uri
    from .server import app, state

    url = f"http://{SERVER_HOST}:{args.port}"
    print(f"\n  spt2yt   ->  {url}")
    print(f"  downloads    ->  {state.settings.output_dir}")
    print(f"  redirect URI ->  {redirect_uri(args.port)}\n")

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=SERVER_HOST, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
