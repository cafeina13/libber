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
from libber.jobs import (CANCELLED, DONE, ERROR, EXISTS, REVIEW, Job,
                         _direct_candidate)
from libber.library import Library, folder_for, write_m3u
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


class TestQualityAwareReuse:
    """Reuse ignored the quality setting.

    Switching to High and re-running reused whatever was already on disk, so
    tracks sharing a recording with an earlier ~130 kbps download silently
    stayed at ~130 while their neighbours came down at ~260.
    """

    def _stub(self, monkeypatch, bitrate):
        calls = []

        def fake(cand, dest, on_progress=None, **kw):
            calls.append(cand.video_id)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"audio")
            return Result(path=dest, video_id=cand.video_id, bitrate=bitrate,
                          duration_s=200.0)

        monkeypatch.setattr("libber.jobs.fetch_audio", fake)
        monkeypatch.setattr("libber.jobs.write_tags", lambda *a, **k: None)
        return calls

    def test_standard_reuses_whatever_is_there(self, make_job, playlist, monkeypatch):
        """A 260 kbps file is no reason to re-download at Standard."""
        calls = self._stub(monkeypatch, 260)
        pl = playlist(2)
        monkeypatch.setattr("libber.matcher.search",
                            lambda t: [candidate(video_id="shared")])
        job = make_job(pl, audio_quality="standard")
        job._process(job.tasks[pl.tracks[0].id])
        job._process(job.tasks[pl.tracks[1].id])
        assert len(calls) == 1
        assert "reused" in job.tasks[pl.tracks[1].id].message

    def test_high_refuses_a_lower_bitrate_file(self, make_job, playlist, monkeypatch):
        calls = self._stub(monkeypatch, 130)
        pl = playlist(2)
        monkeypatch.setattr("libber.matcher.search",
                            lambda t: [candidate(video_id="shared")])
        job = make_job(pl, audio_quality="high")
        job._process(job.tasks[pl.tracks[0].id])
        job._process(job.tasks[pl.tracks[1].id])
        assert len(calls) == 2                 # fetched again, not reused
        assert "reused" not in job.tasks[pl.tracks[1].id].message

    def test_the_upgrade_replaces_the_file_rather_than_adding_one(
        self, make_job, playlist, monkeypatch, tmp_path
    ):
        """A second copy would differ only in bitrate, and is exactly the "(2)"
        clutter that has to be cleaned up by hand afterwards."""
        self._stub(monkeypatch, 130)
        pl = playlist(2)
        monkeypatch.setattr("libber.matcher.search",
                            lambda t: [candidate(video_id="shared")])
        job = make_job(pl, audio_quality="high")
        job._process(job.tasks[pl.tracks[0].id])
        self._stub(monkeypatch, 260)           # the better stream this time
        job._process(job.tasks[pl.tracks[1].id])

        files = list(job.folder.glob("*.opus"))
        assert len(files) == 1
        assert not any("(2)" in f.name for f in files)

    def test_both_tracks_follow_the_upgraded_file(
        self, make_job, playlist, monkeypatch, tmp_path
    ):
        """The track that pointed at the old file must not be left resolving to
        the version that was just superseded."""
        self._stub(monkeypatch, 130)
        pl = playlist(2)
        monkeypatch.setattr("libber.matcher.search",
                            lambda t: [candidate(video_id="shared")])
        job = make_job(pl, audio_quality="high")
        job._process(job.tasks[pl.tracks[0].id])
        self._stub(monkeypatch, 260)
        job._process(job.tasks[pl.tracks[1].id])

        first = job.library.entry(pl.tracks[0].id)
        second = job.library.entry(pl.tracks[1].id)
        assert first.file == second.file
        assert first.bitrate == second.bitrate == 260

    def test_high_still_reuses_a_file_already_at_high(self, make_job, playlist,
                                                      monkeypatch):
        calls = self._stub(monkeypatch, 260)
        pl = playlist(2)
        monkeypatch.setattr("libber.matcher.search",
                            lambda t: [candidate(video_id="shared")])
        job = make_job(pl, audio_quality="high")
        job._process(job.tasks[pl.tracks[0].id])
        job._process(job.tasks[pl.tracks[1].id])
        assert len(calls) == 1                 # nothing to upgrade


class TestExplicitUpgrade:
    """Re-fetching a track already on disk that sits below the setting.

    Changing quality must not start a bulk re-download on its own, so this is
    opt-in; but without it there is no way to lift an older library at all.
    """

    def _stub(self, monkeypatch, bitrate):
        calls = []

        def fake(cand, dest, on_progress=None, **kw):
            calls.append(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"audio")
            return Result(path=dest, video_id=cand.video_id, bitrate=bitrate,
                          duration_s=200.0)

        monkeypatch.setattr("libber.jobs.fetch_audio", fake)
        monkeypatch.setattr("libber.jobs.write_tags", lambda *a, **k: None)
        monkeypatch.setattr("libber.matcher.search",
                            lambda t: [candidate(video_id="vid", score=99.0)])
        return calls

    def _downloaded_at(self, job, track, kbps):
        job.folder.mkdir(parents=True, exist_ok=True)
        path = target_path(job.folder, track)
        path.write_bytes(b"audio")
        job.library.record(track, path, "vid", "t", "a", 100.0, bitrate=kbps)
        return path

    def test_left_alone_unless_asked(self, make_job, playlist, monkeypatch):
        calls = self._stub(monkeypatch, 260)
        pl = playlist(1)
        job = make_job(pl, audio_quality="high")     # upgrade defaults to False
        self._downloaded_at(job, pl.tracks[0], 130)
        job._process(job.tasks[pl.tracks[0].id])
        assert job.tasks[pl.tracks[0].id].status == EXISTS
        assert calls == []

    def test_refetched_when_asked(self, make_job, playlist, monkeypatch):
        calls = self._stub(monkeypatch, 260)
        pl = playlist(1)
        job = make_job(pl, audio_quality="high")
        job.upgrade = True
        self._downloaded_at(job, pl.tracks[0], 130)
        job._process(job.tasks[pl.tracks[0].id])
        assert job.tasks[pl.tracks[0].id].status == DONE
        assert len(calls) == 1

    def test_the_new_file_replaces_the_old_one(self, make_job, playlist, monkeypatch):
        """Not beside it: a second copy differing only in bitrate is the "(2)"
        clutter this whole area keeps producing."""
        self._stub(monkeypatch, 260)
        pl = playlist(1)
        job = make_job(pl, audio_quality="high")
        job.upgrade = True
        old = self._downloaded_at(job, pl.tracks[0], 130)
        job._process(job.tasks[pl.tracks[0].id])

        files = list(job.folder.glob("*.opus"))
        assert len(files) == 1 and files[0] == old
        assert job.library.entry(pl.tracks[0].id).bitrate == 260

    def test_a_track_already_at_quality_is_untouched(self, make_job, playlist, monkeypatch):
        calls = self._stub(monkeypatch, 260)
        pl = playlist(1)
        job = make_job(pl, audio_quality="high")
        job.upgrade = True
        self._downloaded_at(job, pl.tracks[0], 260)
        job._process(job.tasks[pl.tracks[0].id])
        assert job.tasks[pl.tracks[0].id].status == EXISTS
        assert calls == []

    def test_standard_never_treats_anything_as_below(self, make_job, playlist, monkeypatch):
        calls = self._stub(monkeypatch, 130)
        pl = playlist(1)
        job = make_job(pl, audio_quality="standard")
        job.upgrade = True
        self._downloaded_at(job, pl.tracks[0], 130)
        job._process(job.tasks[pl.tracks[0].id])
        assert job.tasks[pl.tracks[0].id].status == EXISTS
        assert calls == []


class TestUpgradingABorrowedTrack:
    """Upgrading a track whose file lives in another playlist's folder.

    A recording is downloaded once and shared, so the file backing a track in
    this playlist may sit under a different one. The upgrade has to follow the
    file rather than the folder being worked on, or it writes a second copy
    into this folder and leaves the original -- still the lesser version --
    referenced by everything else.
    """

    def _setup(self, tmp_path, monkeypatch, bitrate=260):
        shared = Track(id="a" * 22, title="Shared Song", artists=["Artist"],
                       album="Al", duration_ms=200_000)
        first = Playlist(id="A", kind="playlist", name="Playlist A", tracks=[shared])
        second = Playlist(id="B", kind="playlist", name="Playlist B", tracks=[shared])

        library = Library(tmp_path)
        folder_a = folder_for(tmp_path, first)
        folder_a.mkdir(parents=True)
        file_a = folder_a / "Artist - Shared Song.opus"
        file_a.write_bytes(b"old")
        library.record(shared, file_a, "vid1", "t", "a", 100.0, bitrate=130)

        def fake(cand, dest, on_progress=None, **kw):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"new")
            return Result(path=dest, video_id=cand.video_id, bitrate=bitrate,
                          duration_s=200.0)

        monkeypatch.setattr("libber.jobs.fetch_audio", fake)
        monkeypatch.setattr("libber.jobs.write_tags", lambda *a, **k: None)
        monkeypatch.setattr("libber.matcher.search",
                            lambda t: [candidate(video_id="vid1", score=99.0)])

        job = Job(second, [shared.id], Settings(output_dir=tmp_path, audio_quality="high"),
                  library, asyncio.new_event_loop(), upgrade=True)
        return job, shared, file_a

    def test_replaces_the_file_where_it_actually_lives(self, tmp_path, monkeypatch):
        job, shared, file_a = self._setup(tmp_path, monkeypatch)
        job._process(job.tasks[shared.id])

        assert job.tasks[shared.id].status == DONE
        assert file_a.read_bytes() == b"new"          # upgraded in place
        assert list(tmp_path.rglob("*.opus")) == [file_a]   # no copy in folder B

    def test_the_entry_follows_the_upgrade(self, tmp_path, monkeypatch):
        job, shared, file_a = self._setup(tmp_path, monkeypatch)
        job._process(job.tasks[shared.id])
        entry = job.library.entry(shared.id)
        assert entry.bitrate == 260
        assert entry.file == file_a.relative_to(tmp_path).as_posix()

    def test_this_playlist_still_points_at_it(self, tmp_path, monkeypatch):
        """The borrowing playlist keeps a working relative reference."""
        job, shared, file_a = self._setup(tmp_path, monkeypatch)
        job._process(job.tasks[shared.id])
        m3u = write_m3u(job.folder, job.playlist, job.library)
        entries = [l for l in m3u.read_text(encoding="utf-8").splitlines()
                   if l and not l.startswith("#")]
        assert entries == ["../Playlist A/Artist - Shared Song.opus"]
        assert (m3u.parent / entries[0]).exists()

    def test_a_borrowed_file_that_is_gone_downloads_here_instead(self, tmp_path, monkeypatch):
        """If the other playlist's folder was deleted, there is nothing to
        upgrade -- it is simply a new download into this one."""
        job, shared, file_a = self._setup(tmp_path, monkeypatch)
        file_a.unlink()
        job._process(job.tasks[shared.id])
        assert job.tasks[shared.id].status == DONE
        assert (job.folder / "Artist - Shared Song.opus").exists()


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


class TestCircuitBreaker:
    """A run left unattended must not keep hammering a service that has started
    refusing it. Every failing track tried three candidates, so a blocked
    connection meant hundreds of identical requests against something already
    saying no -- deepening the block and burying the cause."""

    def _failing(self, monkeypatch, message):
        from libber.download import DownloadFailed
        attempts = []

        def fake(cand, dest, on_progress=None, **kw):
            attempts.append(cand.video_id)
            raise DownloadFailed(message)

        monkeypatch.setattr("libber.jobs.fetch_audio", fake)
        monkeypatch.setattr("libber.matcher.search",
                            lambda t: [candidate(video_id="a"), candidate(video_id="b"),
                                       candidate(video_id="c")])
        return attempts

    def test_stops_quickly_when_the_connection_is_refused(self, make_job, playlist,
                                                          monkeypatch):
        attempts = self._failing(
            monkeypatch, "YouTube is blocking this connection as automated traffic")
        pl = playlist(30)
        job = make_job(pl)
        for task in job.ordered():
            job._process(task)

        assert job.cancelled.is_set()
        assert "few hours" in job.stopped_early
        done = [t for t in job.tasks.values() if t.status == ERROR]
        assert len(done) <= 3            # tripped, rather than working through 30

    def test_a_refused_connection_does_not_try_every_candidate(self, make_job, playlist,
                                                               monkeypatch):
        """The next candidate fails identically, so trying it triples the load
        for nothing."""
        attempts = self._failing(monkeypatch, "YouTube refused to serve this track's audio")
        pl = playlist(1)
        job = make_job(pl)
        job._process(job.tasks[pl.tracks[0].id])
        assert len(attempts) == 1        # not all three

    def test_ordinary_failures_are_tolerated_longer(self, make_job, playlist, monkeypatch):
        """A handful of unavailable videos is normal and must not abort a run."""
        self._failing(monkeypatch, "That YouTube video is unavailable")
        pl = playlist(4)
        job = make_job(pl)
        for task in job.ordered():
            job._process(task)
        assert not job.cancelled.is_set()

    def test_a_success_resets_the_count(self, make_job, playlist, monkeypatch,
                                        stub_download):
        """Failures have to be consecutive: scattered ones across a long run are
        not a blocked connection."""
        pl = playlist(3)
        job = make_job(pl)
        job._note_failure("unavailable")
        job._note_failure("unavailable")
        job._note_success()
        job._note_failure("unavailable")
        assert not job.cancelled.is_set()

    def test_remaining_tracks_are_marked_cancelled(self, make_job, playlist, monkeypatch):
        self._failing(monkeypatch, "YouTube is blocking this connection")
        pl = playlist(10)
        job = make_job(pl)
        for task in job.ordered():
            job._process(task)
        assert any(t.status == CANCELLED for t in job.tasks.values())


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


class TestEditedTitlesSurviveRedownload:
    """Two releases of one song share a title, so the only way to separate them
    in a flat song list is to edit the tag. Re-downloading wrote the Spotify
    title straight back over it."""

    def _capture(self, monkeypatch):
        seen: dict = {}
        monkeypatch.setattr("libber.jobs.write_tags",
                            lambda *a, **k: seen.update(k))
        return seen

    def test_stored_edit_reaches_the_tagger(
        self, make_job, playlist, stub_download, monkeypatch
    ):
        """The file was deleted, so there is nothing on disk to read the edit
        back from -- the library has to remember it."""
        seen = self._capture(monkeypatch)
        monkeypatch.setattr("libber.matcher.search", lambda t: [candidate(score=95.0)])
        pl = playlist(1)
        track = pl.tracks[0]
        job = make_job(pl)
        job.folder.mkdir(parents=True, exist_ok=True)
        path = target_path(job.folder, track)
        path.write_bytes(b"old audio")
        job.library.record(track, path, "vid", "t", "a", 100.0,
                           custom_title="Yalan (Canlı)")
        path.unlink()               # file gone, edit not

        job._process(job.tasks[track.id])
        assert job.tasks[track.id].status == DONE
        assert seen.get("keep_title") == "Yalan (Canlı)"
        assert job.library.custom_title_for(track.id) == "Yalan (Canlı)"

    def test_an_untouched_track_keeps_the_spotify_title(
        self, make_job, playlist, stub_download, monkeypatch
    ):
        seen = self._capture(monkeypatch)
        monkeypatch.setattr("libber.matcher.search", lambda t: [candidate(score=95.0)])
        pl = playlist(1)
        job = make_job(pl)
        job._process(job.tasks[pl.tracks[0].id])
        assert seen.get("keep_title") == ""
        assert job.library.custom_title_for(pl.tracks[0].id) == ""

    def test_the_file_wins_over_a_stale_stored_edit(
        self, make_job, playlist, stub_download, monkeypatch
    ):
        """An edit made after the playlist was loaded is newer than anything
        recorded, so the file has to be read rather than trusted blindly."""
        seen = self._capture(monkeypatch)
        monkeypatch.setattr("libber.matcher.search", lambda t: [candidate(score=95.0)])
        monkeypatch.setattr("libber.jobs.edited_title",
                            lambda *a, **k: "Yalan (Live 2011)")
        pl = playlist(1)
        track = pl.tracks[0]
        job = make_job(pl, audio_quality="high")   # a floor the file misses
        job.upgrade = True
        job.folder.mkdir(parents=True, exist_ok=True)
        path = target_path(job.folder, track)
        path.write_bytes(b"old audio")
        job.library.record(track, path, "vid", "t", "a", 100.0, bitrate=130,
                           custom_title="Yalan (Canlı)")

        job._process(job.tasks[track.id])
        assert seen.get("keep_title") == "Yalan (Live 2011)"
        assert job.library.custom_title_for(track.id) == "Yalan (Live 2011)"

    def test_a_title_put_back_by_hand_is_not_resurrected(
        self, make_job, playlist, stub_download, monkeypatch
    ):
        seen = self._capture(monkeypatch)
        monkeypatch.setattr("libber.matcher.search", lambda t: [candidate(score=95.0)])
        monkeypatch.setattr("libber.jobs.edited_title", lambda *a, **k: "")
        pl = playlist(1)
        track = pl.tracks[0]
        job = make_job(pl, audio_quality="high")
        job.upgrade = True
        job.folder.mkdir(parents=True, exist_ok=True)
        path = target_path(job.folder, track)
        path.write_bytes(b"old audio")
        job.library.record(track, path, "vid", "t", "a", 100.0, bitrate=130,
                           custom_title="Yalan (Canlı)")

        job._process(job.tasks[track.id])
        assert seen.get("keep_title") == ""
        assert job.library.custom_title_for(track.id) == ""
