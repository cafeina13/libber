"""FastAPI app: static UI, Spotify auth, playlist inspection, download jobs."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .config import (
    REDIRECT_PATH,
    Settings,
    cookie_option,
    js_runtime,
    load_settings,
    probe_cookies,
    redirect_uri,
    save_credentials,
    save_settings,
)
from .jobs import JobManager
from .library import Library
from .models import Playlist
from .spotify import SpotifyAuth, SpotifyError
from .spotify import fetch as fetch_playlist
from .youtube import YouTubeError
from .youtube import fetch as fetch_youtube

STATIC_DIR = Path(__file__).parent / "static"


class State:
    """Process-wide handles. Single-user localhost app, so plain globals are fine."""

    def __init__(self) -> None:
        self.settings: Settings = load_settings()
        self.port: int = config.SERVER_PORT
        self.auth = SpotifyAuth(self.settings, self.port)
        self.jobs = JobManager(self.settings)
        self.playlists: dict[str, Playlist] = {}
        self._libraries: dict[Path, Library] = {}

    def library(self) -> Library:
        root = self.settings.output_dir
        if root not in self._libraries:
            root.mkdir(parents=True, exist_ok=True)
            self._libraries[root] = Library(root)
        return self._libraries[root]

    def refresh_auth(self) -> None:
        self.auth = SpotifyAuth(self.settings, self.port)


state = State()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    state.jobs.shutdown()


app = FastAPI(title="libber", lifespan=lifespan)


# --------------------------------------------------------------------------
# request models
# --------------------------------------------------------------------------
class CredentialsBody(BaseModel):
    client_id: str
    client_secret: str


class PlaylistBody(BaseModel):
    url: str


class JobBody(BaseModel):
    playlist_id: str
    track_ids: list[str] = Field(default_factory=list)


class RetryBody(BaseModel):
    track_id: str
    video_id: str


class SettingsBody(BaseModel):
    output_dir: str | None = None
    concurrency: int | None = None
    match_threshold: float | None = None
    skip_low_matches: bool | None = None
    enrich_youtube: bool | None = None
    cookies_browser: str | None = None
    cookies_profile: str | None = None
    sleep_between: float | None = None
    audio_quality: str | None = None


# --------------------------------------------------------------------------
# UI + status
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
async def status() -> dict[str, Any]:
    settings = state.settings
    return {
        "has_credentials": settings.has_credentials,
        "logged_in": state.auth.logged_in,
        "user": state.auth.whoami(),
        "redirect_uri": redirect_uri(state.port),
        "settings": {
            "output_dir": str(settings.output_dir),
            "concurrency": settings.concurrency,
            "match_threshold": settings.match_threshold,
            "skip_low_matches": settings.skip_low_matches,
            "enrich_youtube": settings.enrich_youtube,
            "cookies_browser": settings.cookies_browser,
            "cookies_profile": settings.cookies_profile,
            "sleep_between": settings.sleep_between,
            "audio_quality": settings.audio_quality,
        },
        "browsers": detect_browsers(),
        # Without one of these YouTube serves no audio at all, so it's worth
        # stating up front rather than discovering it a hundred tracks in.
        "js_runtime": next(iter(js_runtime()), ""),
    }


def detect_browsers() -> list[dict[str, str]]:
    """Browsers whose cookie stores are actually present on this machine.

    Firefox forks are listed with their profile path because yt-dlp only knows
    the name "firefox" and cannot find Zen, LibreWolf or Waterfox on its own.
    """
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA") or home / "AppData/Roaming")
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData/Local")

    found: list[dict[str, str]] = []
    # Chrome 127+ encrypts its cookie store with App-Bound Encryption, which
    # yt-dlp cannot decrypt on Windows -- so every Chromium browser is a dead
    # end there. They are still listed, marked, rather than hidden: on Linux
    # and macOS they work fine.
    chromium_broken = sys.platform == "win32"
    for name, root in (
        ("chrome", local / "Google/Chrome/User Data"),
        ("edge", local / "Microsoft/Edge/User Data"),
        ("brave", local / "BraveSoftware/Brave-Browser/User Data"),
        ("vivaldi", local / "Vivaldi/User Data"),
        ("chromium", local / "Chromium/User Data"),
        ("opera", appdata / "Opera Software/Opera Stable"),
    ):
        if root.exists():
            label = f"{name} — encrypted, unreadable on Windows" if chromium_broken else name
            found.append({"browser": name, "profile": "", "label": label})

    # Firefox and its forks: locate the profile holding cookies.sqlite.
    for label, root in (
        ("firefox", appdata / "Mozilla/Firefox/Profiles"),
        ("zen", appdata / "zen/Profiles"),
        ("librewolf", appdata / "librewolf/Profiles"),
        ("waterfox", appdata / "Waterfox/Profiles"),
    ):
        if not root.exists():
            continue
        profiles = [p for p in root.iterdir() if (p / "cookies.sqlite").exists()]
        if not profiles:
            continue
        newest = max(profiles, key=lambda p: (p / "cookies.sqlite").stat().st_mtime)
        found.append({
            "browser": "firefox",
            "profile": "" if label == "firefox" else str(newest),
            "label": label,
        })
    return found


@app.post("/api/credentials")
async def set_credentials(body: CredentialsBody) -> dict[str, Any]:
    if not body.client_id.strip() or not body.client_secret.strip():
        raise HTTPException(400, "Both the client ID and secret are required.")
    save_credentials(body.client_id, body.client_secret)
    state.settings.client_id = body.client_id.strip()
    state.settings.client_secret = body.client_secret.strip()
    state.refresh_auth()
    return {"ok": True}


@app.post("/api/settings")
async def update_settings(body: SettingsBody) -> dict[str, Any]:
    settings = state.settings
    if body.output_dir:
        path = Path(body.output_dir).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(400, f"Can't use that folder: {exc}") from exc
        settings.output_dir = path
    if body.concurrency is not None:
        settings.concurrency = max(1, min(8, body.concurrency))
    if body.match_threshold is not None:
        settings.match_threshold = max(0.0, min(100.0, body.match_threshold))
    if body.skip_low_matches is not None:
        settings.skip_low_matches = body.skip_low_matches
    if body.enrich_youtube is not None:
        settings.enrich_youtube = body.enrich_youtube
    if body.cookies_browser is not None:
        settings.cookies_browser = body.cookies_browser.strip()
    if body.cookies_profile is not None:
        settings.cookies_profile = body.cookies_profile.strip()
    if body.sleep_between is not None:
        settings.sleep_between = max(0.0, min(30.0, body.sleep_between))
    if body.audio_quality in ("standard", "high"):
        settings.audio_quality = body.audio_quality
    save_settings(settings)   # survive a restart; losing output_dir is costly
    payload = await status()
    # Report immediately whether the chosen browser can actually be read --
    # otherwise the first sign of trouble is a failed download much later.
    payload["cookie_check"] = await asyncio.to_thread(probe_cookies, settings)
    return payload


@app.post("/api/open-folder")
async def open_folder() -> dict[str, Any]:
    path = state.settings.output_dir
    path.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        raise HTTPException(500, f"Couldn't open the folder: {exc}") from exc
    return {"ok": True}


# --------------------------------------------------------------------------
# Spotify auth
# --------------------------------------------------------------------------
@app.get("/api/login")
async def login() -> dict[str, Any]:
    try:
        return {"url": state.auth.authorize_url()}
    except SpotifyError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/logout")
async def logout() -> dict[str, Any]:
    state.auth.logout()
    return {"ok": True}


@app.get(REDIRECT_PATH, response_class=HTMLResponse)
async def callback(request: Request) -> HTMLResponse:
    error = request.query_params.get("error")
    code = request.query_params.get("code")
    if error:
        body, ok = f"Spotify said: {error}", False
    elif not code:
        body, ok = "No authorization code came back from Spotify.", False
    else:
        try:
            state.auth.complete_login(code)
            body, ok = "Signed in. You can close this tab.", True
        except Exception as exc:
            body, ok = f"Couldn't complete sign-in: {exc}", False

    tone = "#1db954" if ok else "#e5484d"
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8">
<title>libber</title>
<body style="font:16px/1.6 system-ui;background:#0d0f12;color:#e8eaed;
display:grid;place-items:center;height:100vh;margin:0">
<div style="text-align:center">
<div style="font-size:40px;color:{tone}">{'&#10003;' if ok else '&#10005;'}</div>
<p>{body}</p>
<script>if({str(ok).lower()}){{setTimeout(()=>window.close(),1200)}}</script>
</div></body>"""
    )


# --------------------------------------------------------------------------
# playlists + jobs
# --------------------------------------------------------------------------
@app.post("/api/playlist")
async def load_playlist(body: PlaylistBody) -> dict[str, Any]:
    def work() -> Playlist:
        return fetch_playlist(state.auth, body.url)

    try:
        playlist = await asyncio.to_thread(work)
    except SpotifyError as exc:
        if str(exc) == "LOGIN_REQUIRED":
            return JSONResponse(
                {
                    "error": "login_required",
                    "message": "Spotify needs you signed in to read playlist "
                    "contents — this applies to public playlists too, not just "
                    "your private ones.",
                },
                status_code=401,
            )
        raise HTTPException(400, str(exc)) from exc

    return _playlist_response(playlist)


def _playlist_response(playlist: Playlist) -> dict[str, Any]:
    """Shared shape for both sources, so the UI renders them identically."""
    state.playlists[playlist.id] = playlist
    library = state.library()
    return {
        "playlist": {
            "id": playlist.id,
            "kind": playlist.kind,
            "name": playlist.name,
            "owner": playlist.owner,
            "image": playlist.image,
            "description": playlist.description,
            "skipped": playlist.skipped,
            "direct": playlist.is_direct,
        },
        "tracks": [
            {**t.to_dict(), "downloaded": bool(library.entry(t.id))}
            for t in playlist.tracks
        ],
        "sync": library.sync_report(playlist),
    }


@app.post("/api/youtube")
async def load_youtube(body: PlaylistBody) -> dict[str, Any]:
    """YouTube needs no auth at all -- the recording is already identified, so
    there is no matching step and nothing to sign in to."""
    try:
        playlist = await asyncio.to_thread(
            fetch_youtube, body.url, cookie_option(state.settings)
        )
    except YouTubeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _playlist_response(playlist)


@app.post("/api/jobs")
async def create_job(body: JobBody) -> dict[str, Any]:
    playlist = state.playlists.get(body.playlist_id)
    if not playlist:
        raise HTTPException(404, "Load the playlist again — the server restarted.")
    track_ids = body.track_ids or [t.id for t in playlist.tracks]
    if not track_ids:
        raise HTTPException(400, "No tracks selected.")

    job = state.jobs.create(playlist, track_ids, state.library(), spotify=state.auth)
    job.start(state.jobs.pool)
    return {"job_id": job.id, "snapshot": job.snapshot()}


@app.get("/api/jobs/{job_id}")
async def job_snapshot(job_id: str) -> dict[str, Any]:
    job = state.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job.")
    return job.snapshot()


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, Any]:
    job = state.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job.")
    job.cancelled.set()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
async def retry_track(job_id: str, body: RetryBody) -> dict[str, Any]:
    job = state.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job.")
    if not job.retry(body.track_id, body.video_id, state.jobs.pool):
        raise HTTPException(400, "That track or candidate isn't in this job.")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    job = state.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job.")
    queue = job.subscribe()

    async def stream():
        try:
            yield _sse({"event": "snapshot", "snapshot": job.snapshot()})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(message)
                # Deliberately no break on the "job" event. Fixing a match
                # happens after the job reports done, and closing here left
                # those retries streaming to nobody.
        finally:
            job.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
