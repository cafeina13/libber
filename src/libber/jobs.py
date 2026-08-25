"""Job orchestration: match, download, tag, record -- with live progress events.

Downloads run on a small thread pool (yt-dlp is blocking), while the web layer
consumes an asyncio queue per connected client. Worker threads never touch the
event loop directly; they hand events over with call_soon_threadsafe.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import enrich, matcher
from .config import Settings
from .download import DownloadFailed, fetch_audio, target_path, write_tags
from .library import Library, folder_for, write_m3u
from .matcher import Candidate
from .spotify import Playlist, Track

def _direct_candidate(track: Track) -> Candidate:
    """Wrap an already-identified YouTube video as a perfect-score candidate."""
    return Candidate(
        video_id=track.video_id,
        title=track.title,
        artists=list(track.artists),
        album=track.album,
        duration_s=track.duration_s,
        source="song",
        score=100.0,
    )


PENDING = "pending"
MATCHING = "matching"
REVIEW = "needs_review"
DOWNLOADING = "downloading"
DONE = "done"
EXISTS = "exists"
ERROR = "error"
CANCELLED = "cancelled"


@dataclass
class Task:
    track: Track
    index: int
    status: str = PENDING
    progress: float = 0.0
    message: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    chosen: Candidate | None = None
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.track.id,
            "index": self.index,
            "track": self.track.to_dict(),
            "status": self.status,
            "progress": round(self.progress, 3),
            "message": self.message,
            "match": self.chosen.to_dict() if self.chosen else None,
            "candidates": [c.to_dict() for c in self.candidates[:6]],
            "path": str(self.path) if self.path else None,
        }


class Job:
    def __init__(
        self,
        playlist: Playlist,
        track_ids: Iterable[str],
        settings: Settings,
        library: Library,
        loop: asyncio.AbstractEventLoop,
        spotify: Any = None,
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.playlist = playlist
        self.settings = settings
        self.library = library
        self.loop = loop
        # SpotifyAuth, used only to enrich YouTube-sourced tracks. Optional:
        # the YouTube card works with no Spotify credentials at all.
        self.spotify = spotify
        self.folder = folder_for(library.root, playlist)
        self.status = PENDING
        self.cancelled = threading.Event()
        self._subscribers: list[asyncio.Queue] = []
        self._lock = threading.Lock()
        self._claimed: set[Path] = set()

        wanted = set(track_ids)
        self.tasks: dict[str, Task] = {}
        for position, track in enumerate(playlist.tracks, start=1):
            if track.id in wanted and track.id not in self.tasks:
                self.tasks[track.id] = Task(track=track, index=position)

    # -- events ----------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        message = {"event": event, **payload}
        with self._lock:
            targets = list(self._subscribers)
        for queue in targets:
            self.loop.call_soon_threadsafe(queue.put_nowait, message)

    def _push(self, task: Task) -> None:
        self.emit("task", {"task": task.to_dict()})

    def snapshot(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for task in self.tasks.values():
            counts[task.status] = counts.get(task.status, 0) + 1
        return {
            "id": self.id,
            "status": self.status,
            "folder": str(self.folder),
            "playlist": {"id": self.playlist.id, "name": self.playlist.name},
            "counts": counts,
            "tasks": [t.to_dict() for t in self.ordered()],
        }

    def ordered(self) -> list[Task]:
        return sorted(self.tasks.values(), key=lambda t: t.index)

    # -- execution -------------------------------------------------------
    def start(self, pool: ThreadPoolExecutor) -> None:
        self.status = DOWNLOADING
        threading.Thread(target=self._run, args=(pool,), daemon=True).start()

    def _run(self, pool: ThreadPoolExecutor) -> None:
        try:
            futures = [pool.submit(self._process, t) for t in self.ordered()]
            for future in futures:
                future.result()
        finally:
            self._finish()

    def _finish(self) -> None:
        self.library.record_playlist(self.playlist, self.folder)
        self.library.save()
        m3u = None
        try:
            m3u = write_m3u(self.folder, self.playlist, self.library)
        except OSError as exc:
            self.emit("warning", {"message": f"Couldn't write the .m3u8: {exc}"})

        self.status = CANCELLED if self.cancelled.is_set() else DONE
        summary = {status: 0 for status in (DONE, EXISTS, REVIEW, ERROR, CANCELLED)}
        for task in self.tasks.values():
            summary[task.status] = summary.get(task.status, 0) + 1
        self.emit(
            "job",
            {
                "status": self.status,
                "summary": summary,
                "folder": str(self.folder),
                "m3u": str(m3u) if m3u else None,
            },
        )

    def _process(self, task: Task) -> None:
        if self.cancelled.is_set():
            task.status = CANCELLED
            self._push(task)
            return

        existing = self.library.entry(task.track.id)
        if existing:
            task.status = EXISTS
            task.progress = 1.0
            task.path = self.library.root / existing.file
            task.message = "already downloaded"
            self._push(task)
            return

        # A direct YouTube source already names the recording, so there is
        # nothing to search for and nothing to second-guess -- but it arrives
        # without album, date or ISRC, so fill those in first.
        if task.track.video_id:
            self._enrich(task)
            self._download(task, [_direct_candidate(task.track)])
            return

        task.status = MATCHING
        self._push(task)
        try:
            task.candidates = matcher.search(task.track)
        except Exception as exc:
            task.status = ERROR
            task.message = f"YouTube Music search failed: {exc}"
            self._push(task)
            return

        if not task.candidates:
            task.status = ERROR
            task.message = "No results on YouTube Music."
            self._push(task)
            return

        best = task.candidates[0]
        if self.settings.skip_low_matches:
            # A "risky" top hit is a different *recording* (live, remix, cover,
            # karaoke, wrong length) rather than a merely weak match, so it goes
            # to review no matter how well it scored on title and artist.
            if best.risky:
                task.status = REVIEW
                task.chosen = best
                task.message = f"Best hit looks like a {best.flags[0]} — check it."
                self._push(task)
                return
            if best.score < self.settings.match_threshold:
                task.status = REVIEW
                task.chosen = best
                task.message = "Low-confidence match — pick one yourself."
                self._push(task)
                return

        self._download(task, task.candidates[:3])

    def _enrich(self, task: Task) -> None:
        """Best-effort metadata lookup for a YouTube-sourced track.

        Spotify first: it has album, date, ISRC and square cover art. Falling
        back to YouTube Music's own fields, which need no credentials but carry
        less. Either way a failure is silent -- the download still happens, just
        with thinner tags.
        """
        if not self.settings.enrich_youtube or task.track.album:
            return

        task.status = MATCHING
        task.message = "looking up album details"
        self._push(task)

        found = None
        if self.spotify and self.spotify.settings.has_credentials:
            try:
                found = enrich.from_spotify(task.track, self.spotify.app_client())
            except Exception:
                found = None
        if not found:
            found = enrich.from_youtube(task.track)
        task.message = f"album: {found}" if found else ""

    def _reserve_path(self, task: Task) -> Path:
        """Claim a free filename for this track.

        Filenames carry no track number, so two entries can land on the same
        name -- a playlist holding the same song twice, or the same song from
        two different albums. Downloads run in parallel, so claims are tracked
        in-memory as well as on disk, and a file another track already owns is
        never overwritten.
        """
        base = target_path(self.folder, task.track)
        with self._lock:
            candidate, n = base, 2
            while candidate in self._claimed or (
                candidate.exists()
                and self.library.owner_of(candidate) not in (None, task.track.id)
            ):
                candidate = base.with_name(f"{base.stem} ({n}){base.suffix}")
                n += 1
            self._claimed.add(candidate)
        return candidate

    def _download(self, task: Task, options: list[Candidate]) -> None:
        """Walk down the ranked list; unavailable videos are common enough that
        giving up on the first failure would cost real tracks."""
        last_error = ""
        for candidate in options:
            if self.cancelled.is_set():
                task.status = CANCELLED
                self._push(task)
                return

            # The same recording can appear twice in one playlist under two
            # Spotify ids (a single and its album release). Point the second at
            # the file we already have rather than downloading it again.
            shared = self.library.entry_by_video(candidate.video_id)
            if shared:
                _, found = shared
                path = self.library.root / found.file
                self.library.record(
                    track=task.track,
                    path=path,
                    video_id=candidate.video_id,
                    title=found.title,
                    artist=found.artist,
                    score=candidate.score,
                )
                task.status = DONE
                task.progress = 1.0
                task.path = path
                task.chosen = candidate
                task.message = "same recording as another track — reused"
                self._push(task)
                return

            task.status = DOWNLOADING
            task.chosen = candidate
            task.progress = 0.0
            task.message = ""
            self._push(task)

            def report(fraction: float, note: str, _task: Task = task) -> None:
                _task.progress = fraction
                _task.message = note
                self._push(_task)

            destination = self._reserve_path(task)
            try:
                result = fetch_audio(candidate, destination, report)
            except DownloadFailed as exc:
                last_error = str(exc)
                continue
            except Exception as exc:  # network blips, ffmpeg trouble
                last_error = str(exc)
                continue

            write_tags(result.path, task.track, candidate.url)
            self.library.record(
                track=task.track,
                path=result.path,
                video_id=candidate.video_id,
                title=candidate.title,
                artist=", ".join(candidate.artists),
                score=candidate.score,
            )
            task.status = DONE
            task.progress = 1.0
            task.path = result.path
            task.message = f"{result.bitrate:.0f} kbps opus" if result.bitrate else "opus"
            self._push(task)
            return

        task.status = ERROR
        task.message = last_error or "Every candidate failed to download."
        self._push(task)

    def retry(self, track_id: str, video_id: str, pool: ThreadPoolExecutor) -> bool:
        """Re-run one track against a specific YouTube video the user picked."""
        task = self.tasks.get(track_id)
        if not task:
            return False
        chosen = next((c for c in task.candidates if c.video_id == video_id), None)
        if chosen is None:
            return False
        self.library.forget(track_id, delete_file=True)
        pool.submit(self._download, task, [chosen])
        return True


class JobManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pool = ThreadPoolExecutor(
            max_workers=max(1, settings.concurrency), thread_name_prefix="libber"
        )
        self.jobs: dict[str, Job] = {}

    def create(
        self,
        playlist: Playlist,
        track_ids: Iterable[str],
        library: Library,
        spotify: Any = None,
    ) -> Job:
        job = Job(
            playlist,
            track_ids,
            self.settings,
            library,
            asyncio.get_running_loop(),
            spotify=spotify,
        )
        self.jobs[job.id] = job
        if len(self.jobs) > 20:  # keep memory bounded across a long session
            for stale in list(self.jobs)[:-20]:
                self.jobs.pop(stale, None)
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def shutdown(self) -> None:
        for job in self.jobs.values():
            job.cancelled.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
