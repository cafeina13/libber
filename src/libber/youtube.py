"""Read YouTube / YouTube Music playlists and videos as downloadable tracks.

Nothing here needs matching: the recording is already identified, so tracks
produced by this module carry a video_id and the job runner skips the matcher
entirely. Metadata is whatever YouTube exposes, cleaned up -- which is thinner
than Spotify's, so titles get the usual "(Official Video)" clutter stripped and
"Artist - Title" split apart when the uploader formatted it that way.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .config import js_runtime
from .models import Playlist, Track


class YouTubeError(RuntimeError):
    pass


_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_LIST_ID = re.compile(r"^[A-Za-z0-9_-]{2,}$")

# Junk uploaders bolt onto titles. Stripped for the filename and tags only --
# it never affects which video is downloaded.
_NOISE = re.compile(
    r"\s*[\(\[]\s*(?:"
    r"official\s*(?:music\s*)?(?:video|audio|visualizer|lyric[s]?\s*video)?|"
    r"music\s*video|lyric[s]?(?:\s*video)?|audio|visuali[sz]er|"
    r"hd|hq|4k|full\s*hd|remaster(?:ed)?(?:\s*\d{4})?|"
    r"free\s*download|out\s*now|explicit"
    r")\s*[\)\]]",
    re.IGNORECASE,
)
_TRAILING_NOISE = re.compile(
    r"\s*[-–|]\s*(?:official\s*(?:music\s*)?video|official\s*audio|lyric[s]?\s*video|"
    r"music\s*video|visuali[sz]er)\s*$",
    re.IGNORECASE,
)
_TOPIC = re.compile(r"\s*-\s*Topic\s*$", re.IGNORECASE)
_SPLIT = re.compile(r"\s+[-–—]\s+")


def parse_source(raw: str) -> tuple[str, str]:
    """Return (kind, id) for a YouTube URL. kind is 'yt-playlist' or 'yt-video'."""
    text = (raw or "").strip()
    if not text:
        raise YouTubeError("Paste a YouTube link first.")

    parsed = urlparse(text if "//" in text else f"https://{text}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host not in {
        "youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "youtube-nocookie.com"
    }:
        if _VIDEO_ID.match(text):
            return "yt-video", text
        raise YouTubeError(
            "That doesn't look like a YouTube link. Expected something like "
            "https://www.youtube.com/playlist?list=… or https://youtu.be/…"
        )

    query = parse_qs(parsed.query or "")
    list_id = (query.get("list") or [""])[0]
    video_id = (query.get("v") or [""])[0]
    if host == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/")[0]
    if not video_id and parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
        video_id = parsed.path.split("/")[2] if len(parsed.path.split("/")) > 2 else ""

    # A "RD…" list is an auto-generated radio mix -- effectively endless, and
    # almost never what someone pasting a song link wants.
    if list_id and not list_id.startswith("RD") and _LIST_ID.match(list_id):
        return "yt-playlist", list_id
    if video_id and _VIDEO_ID.match(video_id):
        return "yt-video", video_id
    if list_id:
        return "yt-playlist", list_id
    raise YouTubeError("Couldn't find a video or playlist id in that link.")


def _clean_title(text: str) -> str:
    cleaned = _NOISE.sub(" ", text or "")
    cleaned = _TRAILING_NOISE.sub("", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip(" -–—|")


def _split_artist_title(title: str, uploader: str) -> tuple[list[str], str]:
    """Prefer 'Artist - Title' when the uploader used it, else fall back to
    the channel name (minus YouTube's ' - Topic' suffix)."""
    parts = _SPLIT.split(title, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        artists = [a.strip() for a in re.split(r"\s*(?:,|&|feat\.|ft\.)\s*", parts[0]) if a.strip()]
        return artists or [parts[0].strip()], parts[1].strip()
    channel = _TOPIC.sub("", uploader or "").strip()
    return ([channel] if channel else []), title.strip()


def _thumbnail(video_id: str, entry: dict[str, Any] | None = None) -> str:
    # hqdefault always exists; maxres often 404s, and a missing cover is worse
    # than a smaller one.
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def _to_track(entry: dict[str, Any], index: int = 0) -> Track | None:
    video_id = entry.get("id") or entry.get("url") or ""
    if not _VIDEO_ID.match(str(video_id)):
        return None
    if entry.get("live_status") in {"is_live", "is_upcoming"}:
        return None

    raw_title = entry.get("title") or ""
    if raw_title.lower() in {"[deleted video]", "[private video]", "[unavailable video]"}:
        return None

    uploader = entry.get("uploader") or entry.get("channel") or entry.get("artist") or ""

    # YouTube Music entries carry proper track/artist fields; use them when present.
    if entry.get("track"):
        title = entry["track"]
        raw_artist = entry.get("artist") or uploader
        artists = [a.strip() for a in re.split(r"\s*[,;&]\s*", raw_artist) if a.strip()]
    else:
        artists, title = _split_artist_title(_clean_title(raw_title), uploader)
        title = _clean_title(title)

    duration = entry.get("duration") or 0
    return Track(
        id=video_id,
        title=title or raw_title or video_id,
        artists=artists or ["Unknown artist"],
        album=entry.get("album") or "",
        album_artist=(entry.get("artist") or (artists[0] if artists else "")),
        duration_ms=int(float(duration) * 1000) if duration else 0,
        track_number=index,
        release_date=str(entry.get("release_year") or "")[:4],
        cover_url=_thumbnail(video_id, entry),
        cover_size=(480, 360),
        url=f"https://www.youtube.com/watch?v={video_id}",
        video_id=video_id,
    )


def _extract(url: str, flat: bool, cookies: tuple | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "ignoreerrors": True,
    }
    runtime = js_runtime()
    if runtime:
        opts["js_runtimes"] = runtime
    if cookies:
        opts["cookiesfrombrowser"] = cookies
    if flat:
        opts["extract_flat"] = "in_playlist"
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise YouTubeError(_tidy(str(exc))) from exc
    if not info:
        raise YouTubeError("YouTube returned nothing for that link.")
    return info


def _tidy(message: str) -> str:
    text = message.replace("ERROR: ", "").strip()
    low = text.lower()
    if "private" in low:
        return "That playlist or video is private."
    if "does not exist" in low or "not found" in low or "unavailable" in low:
        return "That playlist or video doesn't exist (or was removed)."
    if "sign in" in low:
        return "YouTube wants a signed-in session for that one."
    return text.splitlines()[0][:200] if text else "Couldn't read that link."


def fetch(raw: str, cookies: tuple | None = None) -> Playlist:
    kind, ident = parse_source(raw)

    if kind == "yt-video":
        info = _extract(f"https://www.youtube.com/watch?v={ident}", flat=False,
                        cookies=cookies)
        track = _to_track(info, 1)
        if not track:
            raise YouTubeError("That video can't be downloaded (live, private or removed).")
        return Playlist(
            id=f"yt:{ident}",
            kind="yt-video",
            name=track.label,
            owner=info.get("uploader") or "",
            image=track.cover_url,
            tracks=[track],
        )

    info = _extract(f"https://www.youtube.com/playlist?list={ident}", flat=True,
                    cookies=cookies)
    entries = [e for e in (info.get("entries") or []) if e]
    tracks, skipped = [], []
    for i, entry in enumerate(entries, start=1):
        track = _to_track(entry, i)
        if track:
            tracks.append(track)
        else:
            skipped.append(entry.get("title") or "unavailable video")

    if not tracks:
        raise YouTubeError(
            "No downloadable videos in that playlist — everything in it is "
            "private, deleted or a live stream."
        )

    # Flat extraction keeps big playlists fast but returns no album field. An
    # "OLAK5uy_" list is a YouTube Music album release, so its own title is the
    # album name -- worth filling in, since otherwise these tag as album-less.
    if ident.startswith("OLAK5uy_"):
        album_name = re.sub(r"^(Album|Single|EP)\s+-\s+", "", info.get("title") or "", flags=re.I)
        for track in tracks:
            track.album = track.album or album_name
            track.album_artist = track.album_artist or track.artists[0]

    # yt-dlp labels YouTube Music album playlists "Album - <name>"; that prefix
    # would end up as the folder name.
    name = re.sub(r"^(Album|Single|EP)\s+-\s+", "", info.get("title") or "", flags=re.I)

    return Playlist(
        id=f"yt:{ident}",
        kind="yt-playlist",
        name=name or "YouTube playlist",
        owner=info.get("uploader") or info.get("channel") or "",
        description=(info.get("description") or "")[:300],
        image=tracks[0].cover_url,
        tracks=tracks,
        skipped=skipped,
    )
