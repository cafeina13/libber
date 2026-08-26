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
    bitrate: int = 0        # kbps; 0 when unknown, filled in on first read

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

    def entry_by_video(
        self, video_id: str, min_bitrate: int = 0
    ) -> tuple[str, Entry] | None:
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
            if not (self.root / raw["file"]).exists():
                continue
            found = Entry(**raw)
            # Reusing a file fetched at a lower quality would silently ignore
            # the setting -- the request was for the better stream.
            if min_bitrate and self.bitrate_of(found) < min_bitrate:
                continue
            return track_id, found
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

    # -- self-healing ----------------------------------------------------
    def reconcile(self) -> dict[str, int]:
        """Re-point entries at files that were renamed or moved.

        Entries record a path, so renaming a file by hand silently detaches it:
        the track reads as missing and downloads again while the renamed file
        sits there unreferenced. Every file libber writes carries its Spotify id
        in the tags, which is authoritative -- the pairing is recovered from the
        audio rather than guessed from the name.

        Only folders that actually contain a missing entry are scanned, so the
        usual case (nothing missing) costs one directory listing.
        """
        from mutagen.oggopus import OggOpus

        with self._lock:
            missing = {tid: e for tid, e in self._data["tracks"].items()
                       if not (self.root / e["file"]).exists()}
        if not missing:
            return {"repaired": 0, "still_missing": 0}

        folders = {(self.root / e["file"]).parent for e in missing.values()}
        by_id: dict[str, str] = {}
        for folder in folders:
            if not folder.is_dir():
                continue
            for path in folder.glob("*.opus"):
                try:
                    tagged = (OggOpus(path).get("spotifyid") or [""])[0]
                except Exception:
                    continue
                # A file claimed by another entry is still the right answer for
                # whatever its tags say it is; two ids sharing one file is
                # normal when a recording is reused.
                if tagged:
                    by_id.setdefault(tagged, path.relative_to(self.root).as_posix())

        repaired = 0
        with self._lock:
            # A file that survives can be pointed at by more than one id: two
            # Spotify ids for one recording share a file, and only one of them
            # is in the tags. Fall back to the video, which both agree on.
            by_video = {e["video_id"]: e["file"]
                        for e in self._data["tracks"].values()
                        if e.get("video_id") and (self.root / e["file"]).exists()}
            for track_id, entry in list(missing.items()):
                found = by_id.get(track_id) or by_video.get(entry.get("video_id", ""))
                if found:
                    self._data["tracks"][track_id]["file"] = found
                    missing.pop(track_id)
                    repaired += 1
        if repaired:
            self.save()
        return {"repaired": repaired, "still_missing": len(missing)}

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
    def bitrate_of(self, entry: Entry) -> int:
        """The file's bitrate in kbps, read from the audio if not yet recorded.

        Entries written before bitrate was tracked carry 0, and probing is
        cheap enough to do lazily -- mutagen reads the Opus header, no
        subprocess -- so the answer is cached back into the entry.
        """
        if entry.bitrate:
            return entry.bitrate
        from mutagen.oggopus import OggOpus

        try:
            kbps = int(OggOpus(self.root / entry.file).info.bitrate / 1000)
        except Exception:
            return 0
        with self._lock:
            for raw in self._data["tracks"].values():
                if raw.get("file") == entry.file:
                    raw["bitrate"] = kbps
        return kbps

    def repoint(self, old_file: str, new_path: Path, bitrate: int = 0) -> int:
        """Move every entry that referenced one file onto another.

        Used when a shared recording is re-fetched at a higher quality: each
        track pointing at the superseded file should follow it, or they would
        keep resolving to the lesser version.
        """
        new_file = str(new_path.relative_to(self.root)).replace("\\", "/")
        moved = 0
        with self._lock:
            for raw in self._data["tracks"].values():
                if raw.get("file") == old_file:
                    raw["file"] = new_file
                    raw["bitrate"] = int(bitrate or 0)
                    moved += 1
        return moved

    def record(
        self, track: Track, path: Path, video_id: str, title: str, artist: str,
        score: float, bitrate: int = 0
    ) -> None:
        with self._lock:
            self._data["tracks"][track.id] = Entry(
                file=str(path.relative_to(self.root)).replace("\\", "/"),
                video_id=video_id,
                title=title,
                artist=artist,
                score=round(score, 1),
                at=_now(),
                bitrate=int(bitrate or 0),
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
