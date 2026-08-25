"""On-disk state, sync reports, dedup and .m3u8 writing."""

from __future__ import annotations

import pytest

from spt2yt.download import safe_name, target_path
from spt2yt.library import Library, folder_for, write_m3u
from spt2yt.models import Playlist


@pytest.fixture
def library(tmp_path):
    return Library(tmp_path)


def put(library, track, folder, video_id="vid", score=95.0):
    """Pretend a track was downloaded."""
    path = target_path(folder, track)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake opus")
    library.record(track, path, video_id, track.title, track.artist, score)
    return path


class TestFilenames:
    @pytest.mark.parametrize(
        "raw", ['a/b', 'a\\b', 'a:b', 'a*b', 'a?b', 'a"b', 'a<b', 'a>b', 'a|b']
    )
    def test_windows_illegal_characters_removed(self, raw):
        assert not set(safe_name(raw)) & set('/\\:*?"<>|')

    @pytest.mark.parametrize("reserved", ["CON", "PRN", "AUX", "NUL", "COM1", "LPT1"])
    def test_reserved_device_names_are_escaped(self, reserved):
        # Windows refuses to create a file named CON.opus, whatever the extension.
        assert safe_name(reserved) != reserved

    def test_non_ascii_survives(self):
        assert safe_name("Üçüncü Şarkı — Sanatçı") == "Üçüncü Şarkı — Sanatçı"

    def test_trailing_dots_and_spaces_trimmed(self):
        assert safe_name("name...  ") == "name"

    def test_empty_falls_back(self):
        assert safe_name("") == "untitled"
        assert safe_name("///") != ""

    def test_long_names_truncated(self):
        assert len(safe_name("x" * 500)) <= 120


class TestSyncReport:
    def test_empty_library_reports_everything_new(self, library, playlist):
        pl = playlist(3)
        report = library.sync_report(pl)
        assert report["total"] == 3
        assert len(report["new"]) == 3
        assert report["existing"] == []

    def test_partial_download(self, library, playlist):
        pl = playlist(3)
        folder = folder_for(library.root, pl)
        put(library, pl.tracks[0], folder)
        report = library.sync_report(pl)
        assert len(report["new"]) == 2
        assert len(report["existing"]) == 1

    def test_state_survives_a_reload(self, library, playlist, tmp_path):
        pl = playlist(2)
        folder = folder_for(library.root, pl)
        put(library, pl.tracks[0], folder)
        library.record_playlist(pl, folder)
        library.save()

        reloaded = Library(tmp_path)
        assert len(reloaded.sync_report(pl)["existing"]) == 1

    def test_deleting_a_file_makes_it_new_again(self, library, playlist):
        """The state file is not trusted on its own -- entries are checked
        against the filesystem, so deleting a file re-downloads it."""
        pl = playlist(2)
        folder = folder_for(library.root, pl)
        path = put(library, pl.tracks[0], folder)
        assert len(library.sync_report(pl)["existing"]) == 1

        path.unlink()
        assert len(library.sync_report(pl)["new"]) == 2
        assert library.entry(pl.tracks[0].id) is None

    def test_detects_tracks_removed_from_the_playlist(self, library, playlist):
        pl = playlist(3)
        folder = folder_for(library.root, pl)
        for t in pl.tracks:
            put(library, t, folder)
        library.record_playlist(pl, folder)
        library.save()

        shrunk = Playlist(id=pl.id, kind="playlist", name=pl.name, tracks=pl.tracks[:2])
        assert len(library.sync_report(shrunk)["removed"]) == 1

    def test_corrupt_state_file_does_not_crash(self, tmp_path):
        state = tmp_path / ".spt2yt" / "library.json"
        state.parent.mkdir(parents=True)
        state.write_text("{ this is not json", encoding="utf-8")
        assert Library(tmp_path).known_ids() == set()


class TestDedup:
    def test_finds_an_existing_download_by_video(self, library, playlist):
        """Two Spotify ids -- a single and its album release -- can match the
        same recording. The second should reuse the file, not fetch it twice."""
        pl = playlist(2)
        folder = folder_for(library.root, pl)
        put(library, pl.tracks[0], folder, video_id="sharedvid")

        found = library.entry_by_video("sharedvid")
        assert found is not None
        assert found[0] == pl.tracks[0].id

    def test_ignores_entries_whose_file_is_gone(self, library, playlist):
        pl = playlist(1)
        folder = folder_for(library.root, pl)
        path = put(library, pl.tracks[0], folder, video_id="sharedvid")
        path.unlink()
        assert library.entry_by_video("sharedvid") is None

    def test_unknown_video_and_empty_id(self, library):
        assert library.entry_by_video("nope") is None
        assert library.entry_by_video("") is None

    def test_owner_of_identifies_the_claiming_track(self, library, playlist):
        pl = playlist(1)
        folder = folder_for(library.root, pl)
        path = put(library, pl.tracks[0], folder)
        assert library.owner_of(path) == pl.tracks[0].id
        assert library.owner_of(folder / "nothing.opus") is None


class TestM3U:
    def test_written_in_playlist_order_with_relative_paths(self, library, playlist):
        pl = playlist(3)
        folder = folder_for(library.root, pl)
        for t in pl.tracks:
            put(library, t, folder)

        m3u = write_m3u(folder, pl, library)
        content = m3u.read_text(encoding="utf-8")
        assert content.startswith("#EXTM3U")

        entries = [l for l in content.splitlines() if l and not l.startswith("#")]
        assert len(entries) == 3
        # Relative and forward-slashed, so the folder can be copied to a phone.
        assert all(not l.startswith(("/", "C:", "\\")) and "\\" not in l for l in entries)
        assert all((folder / l).exists() for l in entries)

    def test_skips_tracks_that_were_not_downloaded(self, library, playlist):
        pl = playlist(3)
        folder = folder_for(library.root, pl)
        put(library, pl.tracks[0], folder)
        entries = [
            l for l in write_m3u(folder, pl, library).read_text(encoding="utf-8").splitlines()
            if l and not l.startswith("#")
        ]
        assert len(entries) == 1

    def test_returns_none_when_nothing_is_downloaded(self, library, playlist):
        pl = playlist(2)
        assert write_m3u(folder_for(library.root, pl), pl, library) is None

    def test_folder_name_is_sanitised(self, library, playlist):
        pl = playlist(1, name="My / Bad: Name?")
        assert not set(folder_for(library.root, pl).name) & set('/\\:*?"<>|')
