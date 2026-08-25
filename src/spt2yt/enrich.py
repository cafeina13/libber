"""Fill in the metadata YouTube doesn't give us.

A YouTube track arrives with a title, a channel and a thumbnail -- no album, no
release date, no ISRC, and 16:9 artwork. Spotify's search endpoint knows all of
it and answers an app token, so we look the recording up there and copy the tags
across.

The danger is attaching confidently wrong metadata: search "Chokehold" and
Spotify will happily return a compilation, a live cut or a different artist
entirely. So candidates are scored the same way the YouTube matcher scores
its own, and anything short of a confident match is left alone. No album beats
the wrong album.
"""

from __future__ import annotations

import threading
from typing import Any

import spotipy
from rapidfuzz import fuzz

from .matcher import normalise, variants_in
from .models import Track

# Deliberately stricter than the download matcher. There, a mediocre match still
# gets you the song; here it silently mislabels your library.
ACCEPT_SCORE = 82.0
MAX_DURATION_DELTA = 7.0

_lock = threading.Lock()


def _score(track: Track, cand: dict[str, Any]) -> float:
    title_score = fuzz.token_set_ratio(normalise(track.title), normalise(cand.get("name", "")))

    cand_artists = [a["name"] for a in (cand.get("artists") or []) if a.get("name")]
    artist_score = max(
        (
            fuzz.token_set_ratio(normalise(a), normalise(b))
            for a in (track.artists or [""])
            for b in (cand_artists or [""])
        ),
        default=0.0,
    )

    delta = abs((cand.get("duration_ms") or 0) / 1000.0 - track.duration_s)
    if track.duration_s and delta > MAX_DURATION_DELTA:
        return 0.0
    duration_score = 100.0 if delta <= 2 else max(0.0, 100.0 * (1 - (delta - 2) / 10.0))

    total = 0.4 * title_score + 0.35 * artist_score + 0.25 * duration_score

    # Don't label a studio track with a live album's details, or vice versa.
    ours = variants_in(track.title)
    theirs = variants_in(cand.get("name", "")) | variants_in(
        (cand.get("album") or {}).get("name", "")
    )
    if ours ^ theirs:
        total -= 30
    return total


def _apply(track: Track, cand: dict[str, Any]) -> None:
    album = cand.get("album") or {}
    images = album.get("images") or []
    best_image = max(images, key=lambda i: (i.get("width") or 0), default=None)

    track.album = album.get("name") or track.album
    album_artists = [a["name"] for a in (album.get("artists") or []) if a.get("name")]
    track.album_artist = (album_artists[0] if album_artists else "") or track.album_artist
    track.release_date = (album.get("release_date") or "")[:10] or track.release_date
    track.isrc = (cand.get("external_ids") or {}).get("isrc") or track.isrc
    track.track_number = cand.get("track_number") or track.track_number
    track.disc_number = cand.get("disc_number") or track.disc_number

    # Square album art beats a 16:9 video thumbnail for a music library.
    if best_image and best_image.get("url"):
        track.cover_url = best_image["url"]
        track.cover_size = (best_image.get("width") or 0, best_image.get("height") or 0)


def from_spotify(track: Track, client: spotipy.Spotify) -> str | None:
    """Look the recording up on Spotify and copy its tags onto `track`.

    Returns a short note on what happened, or None if nothing was applied.
    """
    query = f"{track.artist} {track.title}".strip()
    if not query:
        return None
    try:
        with _lock:  # spotipy's client isn't documented as thread-safe
            results = client.search(q=query, type="track", limit=5)
    except spotipy.SpotifyException:
        return None
    except Exception:
        return None

    items = ((results or {}).get("tracks") or {}).get("items") or []
    if not items:
        return None

    best, best_score = None, 0.0
    for cand in items:
        score = _score(track, cand)
        if score > best_score:
            best, best_score = cand, score

    if not best or best_score < ACCEPT_SCORE:
        return None
    _apply(track, best)
    return best.get("album", {}).get("name") or None


def from_youtube(track: Track) -> str | None:
    """Fallback with no credentials: ask YouTube Music what it knows.

    Flat playlist extraction skips this data for speed, so fetch it per video --
    only for the tracks that still need it.
    """
    if not track.video_id:
        return None
    from yt_dlp import YoutubeDL

    try:
        with YoutubeDL(
            {"quiet": True, "no_warnings": True, "noprogress": True, "skip_download": True}
        ) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={track.video_id}", download=False
            )
    except Exception:
        return None
    if not info:
        return None

    album = info.get("album") or ""
    if album:
        track.album = track.album or album
    if info.get("artist"):
        track.album_artist = track.album_artist or str(info["artist"]).split(",")[0].strip()
    if info.get("release_year"):
        track.release_date = track.release_date or str(info["release_year"])[:4]
    return album or None
