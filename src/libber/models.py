"""Shared data models.

A Track is "something to download" regardless of where its metadata came from,
so both the Spotify and the YouTube sources produce these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Track:
    id: str
    title: str
    artists: list[str]
    album: str = ""
    album_artist: str = ""
    duration_ms: int = 0
    track_number: int = 0
    disc_number: int = 1
    release_date: str = ""
    isrc: str = ""
    cover_url: str = ""
    cover_size: tuple[int, int] = (0, 0)
    url: str = ""
    # Set when the recording is already known -- a direct YouTube source. The
    # matcher is skipped entirely for these; there is nothing to guess at.
    video_id: str = ""

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000.0

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "artists": self.artists,
            "artist": self.artist,
            "album": self.album,
            "duration_ms": self.duration_ms,
            "cover_url": self.cover_url,
            "url": self.url,
            "video_id": self.video_id,
        }


@dataclass
class Playlist:
    id: str
    kind: str  # playlist | album | liked | track | yt-playlist | yt-video
    name: str
    owner: str = ""
    description: str = ""
    image: str = ""
    snapshot_id: str = ""
    tracks: list[Track] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def is_direct(self) -> bool:
        """True when every track already knows its source video."""
        return bool(self.tracks) and all(t.video_id for t in self.tracks)
