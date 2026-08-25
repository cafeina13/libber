"""Fetch the matched audio with yt-dlp and write real tags onto the .opus file.

YouTube already serves Opus (itag 251), so the extract-audio step is a stream
copy, not a re-encode -- the bytes we save are the bytes Google served. Tags go
on afterwards as Vorbis comments, with the Spotify cover art embedded so the
files look right in any player.
"""

from __future__ import annotations

import base64
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests
from mutagen.flac import Picture
from mutagen.oggopus import OggOpus
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .matcher import Candidate
from .spotify import Track

ProgressFn = Callable[[float, str], None]

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class DownloadFailed(RuntimeError):
    pass


def safe_name(text: str, fallback: str = "untitled", limit: int = 120) -> str:
    """Windows is the strict one here, so sanitise to its rules everywhere."""
    cleaned = _ILLEGAL.sub("_", text or "").strip().rstrip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)[:limit].rstrip(". ")
    if not cleaned:
        return fallback
    if cleaned.split(".")[0].upper() in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def target_path(root: Path, track: Track, index: int | None = None) -> Path:
    stem = f"{track.artist} - {track.title}"
    if index is not None:
        stem = f"{index:03d} - {stem}"
    return root / f"{safe_name(stem)}.opus"


@dataclass
class Result:
    path: Path
    video_id: str
    bitrate: int
    duration_s: float


def _ydl_opts(tmp: Path, hook) -> dict:
    return {
        # Prefer a native Opus stream so the extract step can copy rather than
        # transcode. The fallbacks only matter for oddball uploads.
        "format": "bestaudio[acodec=opus]/bestaudio/best",
        "outtmpl": {"default": "%(id)s.%(ext)s"},
        "paths": {"home": str(tmp)},
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "opus", "preferredquality": "0"}
        ],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "consoletitle": False,
        "retries": 3,
        "fragment_retries": 5,
        "progress_hooks": [hook],
        "ignoreerrors": False,
    }


def fetch_audio(
    candidate: Candidate, dest: Path, on_progress: ProgressFn | None = None
) -> Result:
    """Download one candidate to `dest`, replacing anything already there."""

    def hook(d: dict) -> None:
        if not on_progress:
            return
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            frac = (done / total) if total else 0.0
            speed = d.get("speed") or 0
            label = f"{speed / 1_000_000:.1f} MB/s" if speed else ""
            on_progress(min(frac, 0.99), label)
        elif d.get("status") == "finished":
            on_progress(0.99, "converting")

    with tempfile.TemporaryDirectory(prefix="spt2yt-") as raw_tmp:
        tmp = Path(raw_tmp)
        try:
            with YoutubeDL(_ydl_opts(tmp, hook)) as ydl:
                info = ydl.extract_info(candidate.url, download=True)
        except DownloadError as exc:
            raise DownloadFailed(_tidy_error(str(exc))) from exc

        produced = next(iter(tmp.glob("*.opus")), None)
        if produced is None:  # ffmpeg chose another container
            produced = next(
                (p for p in tmp.iterdir() if p.is_file() and p.suffix != ".part"), None
            )
        if produced is None:
            raise DownloadFailed("yt-dlp produced no audio file.")

        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        shutil.move(str(produced), str(dest))

    return Result(
        path=dest,
        video_id=candidate.video_id,
        bitrate=int((info.get("abr") or 0)),
        duration_s=float(info.get("duration") or candidate.duration_s or 0),
    )


def _tidy_error(message: str) -> str:
    text = message.replace("ERROR: ", "").strip()
    lowered = text.lower()
    if "private video" in lowered:
        return "That YouTube video is private."
    if "video unavailable" in lowered or "not available" in lowered:
        return "That YouTube video is unavailable (often region-locked)."
    if "sign in to confirm your age" in lowered:
        return "Age-restricted video; needs a signed-in session to fetch."
    if "sign in to confirm" in lowered:
        return "YouTube asked for sign-in verification on this one."
    return text.splitlines()[0][:200] if text else "Download failed."


_COVER_CACHE: dict[str, bytes] = {}


def _cover_bytes(url: str) -> bytes | None:
    if not url:
        return None
    if url in _COVER_CACHE:
        return _COVER_CACHE[url]
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    data = resp.content
    if len(_COVER_CACHE) > 128:
        _COVER_CACHE.clear()
    _COVER_CACHE[url] = data
    return data


def write_tags(path: Path, track: Track, source_url: str = "") -> None:
    """Vorbis comments + embedded artwork, the way Opus-in-Ogg expects."""
    try:
        audio = OggOpus(path)
    except Exception:
        return  # not fatal; the audio is on disk and playable either way

    tags = {
        "title": track.title,
        "artist": track.artists or [""],
        "albumartist": track.album_artist or (track.artists[0] if track.artists else ""),
        "album": track.album,
        "date": track.release_date,
        "tracknumber": str(track.track_number or ""),
        "discnumber": str(track.disc_number or ""),
        "isrc": track.isrc,
    }
    for key, value in tags.items():
        if isinstance(value, list):
            cleaned = [v for v in value if v]
            if cleaned:
                audio[key] = cleaned
        elif value:
            audio[key] = [str(value)]

    if track.url:
        audio["spotifyid"] = [track.id]
        audio["www"] = [track.url]
    if source_url:
        audio["comment"] = [f"Downloaded by spt2yt from {source_url}"]

    data = _cover_bytes(track.cover_url)
    if data:
        pic = Picture()
        pic.data = data
        pic.type = 3  # front cover
        pic.mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
        pic.width, pic.height = track.cover_size or (0, 0)
        pic.depth = 24
        audio["metadata_block_picture"] = [
            base64.b64encode(pic.write()).decode("ascii")
        ]

    try:
        audio.save()
    except Exception:
        pass
