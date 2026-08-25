"""Bugs that actually shipped, pinned so they cannot come back.

Every test here corresponds to a real failure seen while running this against a
live account. The docstrings describe the incident, not the assertion -- if one
of these goes red, read it as "that specific thing broke again".
"""

from __future__ import annotations

import json

import pytest

from spt2yt.download import safe_name, target_path
from spt2yt.library import Library, folder_for
from spt2yt.spotify import _build_track, _entry_payload


class TestPlaylistItemRename:
    """Spotify renamed the playlist-entry payload from "track" to "item".

    The parser asked for "track", got None for every entry, and dropped all 655
    tracks of a playlist into "skipped" -- the app looked like it was working
    and silently downloaded nothing.
    """

    def test_reads_the_new_item_field(self):
        entry = {"added_at": "2026-01-01T00:00:00Z", "item": {"id": "abc", "name": "Song"}}
        assert _entry_payload(entry) == {"id": "abc", "name": "Song"}

    def test_still_reads_the_old_track_field(self):
        # Liked Songs never moved off "track", so both shapes must work.
        entry = {"added_at": "2026-01-01T00:00:00Z", "track": {"id": "abc", "name": "Song"}}
        assert _entry_payload(entry) == {"id": "abc", "name": "Song"}

    def test_prefers_item_when_both_are_present(self):
        entry = {"item": {"id": "new"}, "track": {"id": "old"}}
        assert _entry_payload(entry)["id"] == "new"

    def test_ignores_the_boolean_track_flag_inside_a_payload(self):
        """The trap: the payload itself carries a "track" key that is not a
        track object. A bare .get("track") returns something unusable rather
        than nothing, which is worse than failing outright."""
        entry = {"item": {"id": "abc", "name": "Song", "track": True}}
        assert _entry_payload(entry) == {"id": "abc", "name": "Song", "track": True}

    @pytest.mark.parametrize("entry", [None, {}, {"item": None}, {"track": None},
                                       {"item": "not a dict"}])
    def test_unusable_entries_return_none(self, entry):
        assert _entry_payload(entry) is None


class TestLocalAndUnplayableEntries:
    """Playlists contain local files and podcast episodes, which have no id to
    match against. They must be skipped individually, not crash the fetch."""

    def test_local_file_is_skipped(self):
        assert _build_track({"id": None, "name": "some local file", "is_local": True}) is None

    def test_episode_is_skipped(self):
        assert _build_track({"id": "abc", "type": "episode", "name": "A Podcast"}) is None

    def test_real_track_is_built(self):
        track = _build_track({
            "id": "2TLdDkTi59Ik5ISYhDMsak",
            "type": "track",
            "name": "Berrak",
            "duration_ms": 232_426,
            "track_number": 4,
            "disc_number": 1,
            "artists": [{"name": "Pilli Bebek"}],
            "external_ids": {"isrc": "TRAET1900317"},
            "external_urls": {"spotify": "https://open.spotify.com/track/2TLd"},
            "album": {
                "name": "Uyandırmadan",
                "artists": [{"name": "Pilli Bebek"}],
                "release_date": "2000",
                "images": [
                    {"url": "https://i.scdn.co/64", "width": 64, "height": 64},
                    {"url": "https://i.scdn.co/640", "width": 640, "height": 640},
                ],
            },
        })
        assert track is not None
        assert track.title == "Berrak"
        assert track.isrc == "TRAET1900317"
        assert track.duration_ms == 232_426
        # Largest artwork wins; Spotify does not always sort biggest-first.
        assert track.cover_size == (640, 640)


class TestNonAsciiFilenames:
    """A Turkish console (cp1254) crashed the app on its own startup banner.
    Track titles carry the same characters, so they must survive the filename
    path intact rather than being mangled or stripped."""

    @pytest.mark.parametrize(
        "name",
        ["Üçüncü Şarkı", "Sanatçı Ç", "YAZ NEŞESİ", "Ğğİıÿ", "日本語のタイトル", "Émile"],
    )
    def test_preserved_in_filenames(self, name):
        assert safe_name(name) == name

    def test_preserved_through_target_path(self, track, tmp_path):
        t = track(title="Üçüncü Şarkı", artists=["Sanatçı Ç"])
        assert "Üçüncü Şarkı" in target_path(tmp_path, t).name


class TestFilenameCollisions:
    """Track numbers were removed from filenames on request, which removed the
    uniqueness they were quietly providing: a playlist holding the same song
    twice (a single and its album release) collapsed onto one filename and the
    second download overwrote the first."""

    def test_same_artist_and_title_produce_the_same_base_name(self, track, tmp_path):
        a = track(id="a" * 22, title="GET MINE", artists=["Holy Wars"])
        b = track(id="b" * 22, title="GET MINE", artists=["Holy Wars"])
        assert target_path(tmp_path, a) == target_path(tmp_path, b)

    def test_library_reports_who_owns_a_file(self, tmp_path, track):
        """owner_of is what stops the second track clobbering the first."""
        library = Library(tmp_path)
        t = track(id="a" * 22, title="GET MINE", artists=["Holy Wars"])
        path = target_path(tmp_path, t)
        path.write_bytes(b"audio")
        library.record(t, path, "vid", t.title, t.artist, 100.0)

        assert library.owner_of(path) == t.id
        assert library.owner_of(tmp_path / "Something Else.opus") is None


class TestDuplicateRecordings:
    """Two Spotify ids can match the same YouTube video, which downloaded the
    identical audio twice under two filenames. Deleting one then left a library
    entry pointing at a missing file, so the next sync re-created it."""

    def test_second_track_finds_the_first_ones_file(self, tmp_path, track):
        library = Library(tmp_path)
        first = track(id="a" * 22, title="GET MINE", artists=["Holy Wars"])
        path = target_path(tmp_path, first)
        path.write_bytes(b"audio")
        library.record(first, path, "pvbFiezE85Q", first.title, first.artist, 100.0)

        found = library.entry_by_video("pvbFiezE85Q")
        assert found is not None and found[0] == first.id

    def test_entry_pointing_at_a_deleted_file_is_not_reused(self, tmp_path, track):
        library = Library(tmp_path)
        t = track(id="a" * 22)
        path = target_path(tmp_path, t)
        path.write_bytes(b"audio")
        library.record(t, path, "vid", t.title, t.artist, 100.0)
        path.unlink()

        assert library.entry_by_video("vid") is None
        assert library.entry(t.id) is None      # so it downloads again


class TestStateFileDurability:
    """library.json is what makes a 655-track playlist resumable. A truncated
    or half-written file must not wipe the record of everything downloaded."""

    def test_written_atomically(self, tmp_path, playlist):
        library = Library(tmp_path)
        pl = playlist(1)
        folder = folder_for(tmp_path, pl)
        folder.mkdir(parents=True)
        path = target_path(folder, pl.tracks[0])
        path.write_bytes(b"audio")
        library.record(pl.tracks[0], path, "vid", "t", "a", 100.0)
        library.save()

        assert library.state_path.exists()
        assert not list(library.state_path.parent.glob("*.tmp"))  # no debris
        json.loads(library.state_path.read_text(encoding="utf-8"))  # valid JSON

    def test_unreadable_state_does_not_lose_the_audio(self, tmp_path):
        state = tmp_path / ".spt2yt" / "library.json"
        state.parent.mkdir(parents=True)
        state.write_text('{"tracks": {"a": ', encoding="utf-8")  # truncated
        library = Library(tmp_path)
        assert library.known_ids() == set()   # starts over rather than crashing
        library.save()                        # and can still be written
