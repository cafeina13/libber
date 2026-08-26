"""Local library state: what's already on disk, and the .m3u8 that indexes it.

The state file is what makes re-syncing cheap -- a second run of the same
playlist only touches tracks that are genuinely new. Entries are verified
against the filesystem on read, so deleting a file is enough to make libber
fetch it again.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .download import safe_name
from .spotify import Playlist, Track

STATE_DIRNAME = ".libber"
STATE_FILE = "library.json"
VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Entry:
    file: str
    video_id: str
    title: str
    artist: str
    score: float
    at: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class Library:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.state_path = self.root / STATE_DIRNAME / STATE_FILE
        self._lock = threading.RLock()
        self._data = self._read()

    # -- persistence -----------------------------------------------------
    def _read(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": VERSION, "tracks": {}, "playlists": {}, "review": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"version": VERSION, "tracks": {}, "playlists": {}, "review": {}}
        data.setdefault("tracks", {})
        data.setdefault("playlists", {})
        data.setdefault("review", {})
        return data

    def save(self) -> None:
        with self._lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, indent=1, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self.state_path)

    # -- queries ---------------------------------------------------------
    def entry(self, track_id: str) -> Entry | None:
        """Returns the record only if the file is still where we left it."""
        raw = self._data["tracks"].get(track_id)
        if not raw:
            return None
        if not (self.root / raw["file"]).exists():
            return None
        return Entry(**raw)

    def path_for(self, track_id: str) -> Path | None:
        found = self.entry(track_id)
        return self.root / found.file if found else None

    def known_ids(self) -> set[str]:
        return {tid for tid in self._data["tracks"] if self.entry(tid)}

    def entry_by_video(self, video_id: str) -> tuple[str, Entry] | None:
        """Find an already-downloaded track that used this YouTube video.

        A playlist often lists the same recording twice under different Spotify
        ids -- a single and its album release -- and both match the same video.
        Downloading it twice wastes time and leaves duplicate files.
        """
        if not video_id:
            return None
        with self._lock:
            candidates = [
                (tid, raw) for tid, raw in self._data["tracks"].items()
                if raw.get("video_id") == video_id
            ]
        for track_id, raw in candidates:
            if (self.root / raw["file"]).exists():
                return track_id, Entry(**raw)
        return None

    def owner_of(self, path: Path) -> str | None:
        """Which track id claims this file, if any. Used to avoid overwriting
        a different track that happens to sanitise to the same filename."""
        try:
            rel = str(Path(path).relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return None
        with self._lock:
            for track_id, raw in self._data["tracks"].items():
                if raw.get("file") == rel:
                    return track_id
        return None

    def playlist_state(self, playlist_id: str) -> dict[str, Any] | None:
        return self._data["playlists"].get(playlist_id)

    # -- review queue ----------------------------------------------------
    # Tracks the matcher wouldn't guess at. Kept on disk because the queue is
    # the whole point of a confidence threshold: without this, closing the page
    # loses which tracks need attention and every candidate found for them, and
    # rediscovering that means re-searching the entire playlist.
    def reviews(self) -> dict[str, Any]:
        return self._data.setdefault("review", {})

    def record_review(
        self, track: Track, candidates: list[Any], message: str
    ) -> None:
        with self._lock:
            self.reviews()[track.id] = {
                "title": track.title,
                "artist": track.artist,
                "album": track.album,
                "duration_ms": track.duration_ms,
                "message": message,
                "at": _now(),
                "candidates": [c.to_dict() for c in candidates[:6]],
            }

    def clear_review(self, track_id: str) -> None:
        with self._lock:
            self.reviews().pop(track_id, None)

    # -- mutations -------------------------------------------------------
    def record(
        self, track: Track, path: Path, video_id: str, title: str, artist: str, score: float
    ) -> None:
        with self._lock:
            self._data["tracks"][track.id] = Entry(
                file=str(path.relative_to(self.root)).replace("\\", "/"),
                video_id=video_id,
                title=title,
                artist=artist,
                score=round(score, 1),
                at=_now(),
            ).to_dict()
            # A downloaded track is no longer awaiting a decision, however it
            # got there -- matched, reused, or a link pasted by hand.
            self._data.setdefault("review", {}).pop(track.id, None)

    def forget(self, track_id: str, delete_file: bool = False) -> None:
        with self._lock:
            raw = self._data["tracks"].pop(track_id, None)
            if raw and delete_file:
                (self.root / raw["file"]).unlink(missing_ok=True)

    def record_playlist(self, playlist: Playlist, folder: Path) -> None:
        with self._lock:
            self._data["playlists"][playlist.id] = {
                "name": playlist.name,
                "kind": playlist.kind,
                "snapshot_id": playlist.snapshot_id,
                "folder": str(folder.relative_to(self.root)).replace("\\", "/"),
                "track_ids": [t.id for t in playlist.tracks],
                "at": _now(),
            }

    # -- reporting -------------------------------------------------------
    def sync_report(self, playlist: Playlist) -> dict[str, Any]:
        """What a run would actually do: new vs already-have vs removed."""
        have = self.known_ids()
        current = [t.id for t in playlist.tracks]
        previous = (self.playlist_state(playlist.id) or {}).get("track_ids", [])
        return {
            "total": len(current),
            "new": [tid for tid in current if tid not in have],
            "existing": [tid for tid in current if tid in have],
            "removed": [tid for tid in previous if tid not in current],
        }


def folder_for(root: Path, playlist: Playlist) -> Path:
    if playlist.kind == "track":
        return root / "Singles"
    return root / safe_name(playlist.name, fallback=playlist.id)


def write_m3u(folder: Path, playlist: Playlist, library: Library) -> Path | None:
    """Ordered .m3u8 with paths relative to the playlist folder, so the whole
    directory can be copied to a phone and still open correctly."""
    lines = ["#EXTM3U", f"#PLAYLIST:{playlist.name}"]
    written = 0
    for track in playlist.tracks:
        path = library.path_for(track.id)
        if not path:
            continue
        try:
            rel = path.relative_to(folder)
        except ValueError:
            rel = Path("..") / path.relative_to(library.root)
        lines.append(f"#EXTINF:{round(track.duration_s)},{track.artist} - {track.title}")
        lines.append(str(rel).replace("\\", "/"))
        written += 1

    if not written:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{safe_name(playlist.name, fallback='playlist')}.m3u8"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target
