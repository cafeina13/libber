"""Job orchestration: what happens to a track between "selected" and "on disk".

The network is stubbed out throughout -- these test the decisions (skip, review,
reuse, retry, rename), not the downloading.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from libber.config import Settings
from libber.download import Result, target_path
from libber.jobs import DONE, ERROR, EXISTS, REVIEW, Job, _direct_candidate
from libber.library import Library
from libber.matcher import Candidate
from libber.models import Playlist, Track


@pytest.fixture
def make_job(tmp_path):
    def build(playlist: Playlist, **settings_kw):
        settings = Settings(output_dir=tmp_path, **settings_kw)
        library = Library(tmp_path)
        job = Job(
            playlist,
            [t.id for t in playlist.tracks],
            settings,
            library,
            asyncio.new_event_loop(),
        )
        return job

    return build


def candidate(video_id="v" * 11, title="Song", artists=("Artist",), duration_s=200.0,
              score=95.0, risky=False, flags=()):
    return Candidate(
        video_id=video_id, title=title, artists=list(artists), album="",
        duration_s=duration_s, source="song", score=score, risky=risky, flags=list(flags),
    )


@pytest.fixture
def stub_download(monkeypatch):
    """Replace the real download with one that just writes a file."""
    calls = []

    def fake(cand, dest, on_progress=None, **kwargs):
        calls.append((cand.video_id, dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake opus audio")
        if on_progress:
            on_progress(1.0, "done")
        return Result(path=dest, video_id=cand.video_id, bitrate=133, duration_s=200.0)

    monkeypatch.setattr("libber.jobs.fetch_audio", fake)
    monkeypatch.setattr("libber.jobs.write_tags", lambda *a, **k: None)
    return calls


class TestAlreadyDownloaded:
    def test_skips_a_track_already_on_disk(self, make_job, playlist, stub_download):
        pl = playlist(1)
        job = make_job(pl)
        task = job.tasks[pl.tracks[0].id]

        job.folder.mkdir(parents=True, exist_ok=True)
        path = target_path(job.folder, pl.tracks[0])
        path.write_bytes(b"already here")
        job.library.record(pl.tracks[0], path, "vid", "t", "a", 100.0)

        job._process(task)
        assert task.status == EXISTS
        assert stub_download == []      # nothing was fetched


class TestMatching:
    def test_downloads_a_confident_match(self, make_job, playlist, stub_download, monkeypatch):
        pl = playlist(1)
        monkeypatch.setattr("libber.matcher.search", lambda t: [candidate(score=95.0)])
        job = make_job(pl)
        task = job.tasks[pl.tracks[0].id]

        job._process(task)
        assert task.status == DONE
        assert len(stub_download) == 1
        assert job.library.entry(pl.tracks[0].id) is not None

    def test_low_score_goes_to_review(self, make_job, playlist, stub_download, monkeypatch):
        pl = playlist(1)
        monkeypatch.setattr("libber.matcher.search", lambda t: [candidate(score=40.0)])
        job = make_job(pl)
        task = job.tasks[pl.tracks[0].id]

        job._process(task)
        assert task.status == REVIEW
        assert stub_download == []

    def test_risky_match_goes_to_review_despite_a_high_score(
        self, make_job, playlist, stub_download, monkeypatch
    ):
        """A live/remix/cover hit is a different recording, so it is held even
        when title and artist score perfectly."""
        pl = playlist(1)
        monkeypatch.setattr(
            "libber.matcher.search",
            lambda t: [candidate(score=99.0, risky=True, flags=["live version"])],
        )
        job = make_job(pl)
        task = job.tasks[pl.tracks[0].id]

        job._process(task)
        assert task.status == REVIEW
        assert "live version" in task.message
        assert stub_download == []

    def test_review_can_be_switched_off(self, make_job, playlist, stub_download, monkeypatch):
        pl = playlist(1)
        monkeypatch.setattr(
            "libber.matcher.search", lambda t: [candidate(score=10.0, risky=True)]
        )
        job = make_job(pl, skip_low_matches=False)
        task = job.tasks[pl.tracks[0].id]

        job._process(task)
        assert task.status == DONE

    def test_no_results_is_an_error(self, make_job, playlist, monkeypatch):
        pl = playlist(1)
        monkeypatch.setattr("libber.matcher.search", lambda t: [])
        job = make_job(pl)
        task = job.tasks[pl.tracks[0].id]

        job._process(task)
        assert task.status == ERROR

    def test_search_failure_is_reported_not_raised(self, make_job, playlist, monkeypatch):
        pl = playlist(1)

        def boom(t):
            raise RuntimeError("network down")

        monkeypatch.setattr("libber.matcher.search", boom)
        job = make_job(pl)
        task = job.tasks[pl.tracks[0].id]

        job._process(task)
        assert task.status == ERROR
        assert "network down" in task.message


class TestFallbackDownload:
    def test_walks_down_the_ranked_list_when_a_video_fails(
        self, make_job, playlist, monkeypatch
    ):
        """Unavailable and region-locked videos are common enough that giving
        up on the first failure would cost real tracks."""
        from libber.download import DownloadFailed

        pl = playlist(1)
        attempts = []

        def flaky(cand, dest, on_progress=None, **kwargs):
            attempts.append(cand.video_id)
            if cand.video_id == "bad":
                raise DownloadFailed("video unavailable")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"audio")
            return Result(path=dest, video_id=cand.video_id, bitrate=133, duration_s=200.0)

        monkeypatch.setattr("libber.jobs.fetch_audio", flaky)
        monkeypatch.setattr("libber.jobs.write_tags", lambda *a, **k: None)
        monkeypatch.setattr(
            "libber.matcher.search",
            lambda t: [candidate(video_id="bad"), candidate(video_id="good")],
        )
        job = make_job(pl)
        task = job.tasks[pl.tracks[0].id]

        job._process(task)
        assert attempts == ["bad", "good"]
        assert task.status == DONE

    def test_all_candidates_failing_is_an_error(self, make_job, playlist, monkeypatch):
        from libber.download import DownloadFailed

        pl = playlist(1)

        def always_fail(cand, dest, on_progress=None, **kwargs):
            raise DownloadFailed("nope")

        monkeypatch.setattr("libber.jobs.fetch_audio", always_fail)
        monkeypatch.setattr("libber.matcher.search", lambda t: [candidate()])
        job = make_job(pl)
        task = job.tasks[pl.tracks[0].id]

        job._process(task)
        assert task.status == ERROR
        assert "nope" in task.message


class TestDuplicateReuse:
    def test_second_track_with_the_same_video_reuses_the_file(
        self, make_job, playlist, stub_download, monkeypatch
    ):
        """A playlist listing a single and its album release matches one video
        twice; downloading it twice wastes time and leaves duplicate files."""
        pl = playlist(2)
        monkeypatch.setattr(
            "libber.matcher.search", lambda t: [candidate(video_id="shared")]
        )
        job = make_job(pl)

        job._process(job.tasks[pl.tracks[0].id])
        job._process(job.tasks[pl.tracks[1].id])

        assert len(stub_download) == 1              # fetched once
        second = job.tasks[pl.tracks[1].id]
        assert second.status == DONE
        assert "reused" in second.message
        # Both ids resolve, to the same file.
        assert job.library.entry(pl.tracks[0].id).file == job.library.entry(
            pl.tracks[1].id
        ).file


class TestFilenameReservation:
    def test_distinct_tracks_never_share_a_filename(self, make_job, playlist, track):
        """Filenames carry no track number, so two entries can collapse onto
        the same name. The second must get a suffix, not overwrite the first."""
        pl = Playlist(
            id="pl", kind="playlist", name="P",
            tracks=[
                track(id="a" * 22, title="GET MINE", artists=["Holy Wars"]),
                track(id="b" * 22, title="GET MINE", artists=["Holy Wars"]),
            ],
        )
        job = make_job(pl)
        first = job._reserve_path(job.tasks["a" * 22])
        second = job._reserve_path(job.tasks["b" * 22])

        assert first != second
        assert second.stem.endswith("(2)")

    def test_reservation_is_stable_for_one_track(self, make_job, playlist):
        pl = playlist(1)
        job = make_job(pl)
        task = job.tasks[pl.tracks[0].id]
        assert job._reserve_path(task) != job._reserve_path(task)  # each claim is unique


class TestDirectYouTubeTracks:
    def test_skips_matching_entirely(self, make_job, track, stub_download, monkeypatch):
        def explode(t):
            raise AssertionError("matcher must not run for a direct source")

        monkeypatch.setattr("libber.matcher.search", explode)
        pl = Playlist(
            id="yt:x", kind="yt-playlist", name="P",
            tracks=[track(id="v" * 11, video_id="v" * 11, album="Known")],
        )
        job = make_job(pl, enrich_youtube=False)
        task = job.tasks["v" * 11]

        job._process(task)
        assert task.status == DONE
        assert stub_download[0][0] == "v" * 11

    def test_direct_candidate_is_a_perfect_score(self, track):
        t = track(title="Song", artists=["Artist"], duration_ms=200_000, video_id="v" * 11)
        cand = _direct_candidate(t)
        assert cand.video_id == "v" * 11
        assert cand.score == 100.0
        assert not cand.risky


class TestFixMatch:
    """Choosing an alternative from the picker.

    A retry always lands after the job has reported done, so it has to do the
    bookkeeping the job's own finish step would have done. It didn't: the
    download happened but was never saved and never reached the playlist file,
    so it silently vanished on restart.
    """

    def _park_then_offer(self, make_job, playlist, monkeypatch, stub_download):
        pl = playlist(1)
        good = candidate(video_id="g" * 11, score=99.0)
        risky = candidate(video_id="r" * 11, score=95.0, risky=True, flags=["live version"])
        monkeypatch.setattr("libber.matcher.search", lambda t: [risky, good])
        job = make_job(pl, match_threshold=90.0)
        task = job.tasks[pl.tracks[0].id]
        job._process(task)
        assert task.status == REVIEW
        return job, task

    def test_downloads_the_chosen_candidate(self, make_job, playlist, monkeypatch,
                                            stub_download):
        job, task = self._park_then_offer(make_job, playlist, monkeypatch, stub_download)
        job._retry_worker(task, next(c for c in task.candidates if c.video_id == "g" * 11))
        assert task.status == DONE
        assert stub_download[0][0] == "g" * 11

    def test_result_survives_a_restart(self, make_job, playlist, monkeypatch,
                                       stub_download, tmp_path):
        job, task = self._park_then_offer(make_job, playlist, monkeypatch, stub_download)
        job._retry_worker(task, task.candidates[1])
        # A fresh Library reads from disk, so this fails unless save() ran.
        assert Library(tmp_path).entry(task.track.id) is not None

    def test_playlist_file_is_rewritten(self, make_job, playlist, monkeypatch,
                                        stub_download):
        job, task = self._park_then_offer(make_job, playlist, monkeypatch, stub_download)
        job._retry_worker(task, task.candidates[1])
        m3u = list(job.library.root.rglob("*.m3u8"))
        assert m3u, "the fixed track never reached the playlist file"
        assert task.path.name in m3u[0].read_text(encoding="utf-8")

    def test_unknown_track_or_candidate_is_refused(self, make_job, playlist, monkeypatch,
                                                   stub_download):
        job, task = self._park_then_offer(make_job, playlist, monkeypatch, stub_download)
        # Both bail out before the pool is touched, so None is safe to pass.
        assert job.retry("nosuchtrack", "g" * 11, None) is False
        assert job.retry(task.track.id, "nosuchvideo", None) is False


class TestManualLink:
    """Pasting a link by hand.

    Search cannot place every recording -- an obscure release, a title the
    catalogue spells differently, a track present on exactly one upload. The
    ranked picker is no help there, so a link is accepted directly. This has to
    work when the matcher returned nothing at all, which is precisely when it
    is needed.
    """

    def _stuck_track(self, make_job, monkeypatch, stub_download):
        pl = Playlist(id="pl", kind="playlist", name="P", tracks=[
            Track(id="a" * 22, title="Obscure", artists=["Rare"], album="Al",
                  duration_ms=200_000)])
        monkeypatch.setattr("libber.matcher.search", lambda t: [])
        job = make_job(pl)
        task = job.tasks["a" * 22]
        job._process(task)
        assert task.status == ERROR and not task.candidates
        return job, task

    def test_works_with_no_candidates_at_all(self, make_job, monkeypatch, stub_download):
        job, task = self._stuck_track(make_job, monkeypatch, stub_download)
        pool = ThreadPoolExecutor(max_workers=1)
        assert job.retry_url(task.track.id, "https://youtu.be/dQw4w9WgXcQ", pool) == ""
        pool.shutdown(wait=True)
        assert task.status == DONE
        assert stub_download[0][0] == "dQw4w9WgXcQ"

    def test_result_is_recorded_and_survives_a_restart(self, make_job, monkeypatch,
                                                       stub_download, tmp_path):
        job, task = self._stuck_track(make_job, monkeypatch, stub_download)
        pool = ThreadPoolExecutor(max_workers=1)
        job.retry_url(task.track.id, "https://youtu.be/dQw4w9WgXcQ", pool)
        pool.shutdown(wait=True)
        entry = Library(tmp_path).entry(task.track.id)
        assert entry is not None and entry.video_id == "dQw4w9WgXcQ"

    def test_marked_as_hand_picked(self, make_job, monkeypatch, stub_download):
        job, task = self._stuck_track(make_job, monkeypatch, stub_download)
        pool = ThreadPoolExecutor(max_workers=1)
        job.retry_url(task.track.id, "https://youtu.be/dQw4w9WgXcQ", pool)
        pool.shutdown(wait=True)
        assert "picked by hand" in task.chosen.flags

    @pytest.mark.parametrize(
        "url, expected",
        [
            ("", "YouTube link"),
            ("not a link", "YouTube link"),
            ("https://open.spotify.com/track/abc", "YouTube link"),
            ("https://www.youtube.com/playlist?list=PLabc123", "single video"),
        ],
    )
    def test_bad_links_are_refused_with_a_reason(self, make_job, monkeypatch,
                                                 stub_download, url, expected):
        job, task = self._stuck_track(make_job, monkeypatch, stub_download)
        problem = job.retry_url(task.track.id, url, None)
        assert expected in problem
        assert stub_download == []

    def test_unknown_track_is_refused(self, make_job, monkeypatch, stub_download):
        job, _ = self._stuck_track(make_job, monkeypatch, stub_download)
        assert "isn't part of this job" in job.retry_url("nope", "https://youtu.be/dQw4w9WgXcQ", None)


class TestCancellation:
    def test_cancelled_job_stops_processing(self, make_job, playlist, stub_download):
        pl = playlist(1)
        job = make_job(pl)
        job.cancelled.set()
        task = job.tasks[pl.tracks[0].id]

        job._process(task)
        assert stub_download == []


class TestSnapshot:
    def test_reports_status_counts_and_ordering(self, make_job, playlist):
        pl = playlist(3)
        job = make_job(pl)
        snap = job.snapshot()
        assert snap["counts"]["pending"] == 3
        assert [t["index"] for t in snap["tasks"]] == [1, 2, 3]
        assert snap["playlist"]["name"] == pl.name
