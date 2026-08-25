"""YouTube URL parsing and title cleanup -- all offline."""

from __future__ import annotations

import pytest

from spt2yt.youtube import (
    YouTubeError,
    _clean_title,
    _split_artist_title,
    _to_track,
    parse_source,
)

VID = "dQw4w9WgXcQ"


class TestParseSource:
    @pytest.mark.parametrize(
        "url",
        [
            f"https://www.youtube.com/watch?v={VID}",
            f"https://youtube.com/watch?v={VID}",
            f"https://m.youtube.com/watch?v={VID}",
            f"https://music.youtube.com/watch?v={VID}",
            f"https://youtu.be/{VID}",
            f"https://youtu.be/{VID}?t=42",
            f"https://www.youtube.com/shorts/{VID}",
            f"music.youtube.com/watch?v={VID}",  # no scheme
            VID,                                  # bare id
        ],
    )
    def test_video_forms(self, url):
        assert parse_source(url) == ("yt-video", VID)

    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://www.youtube.com/playlist?list=PLabc123", "PLabc123"),
            ("https://music.youtube.com/playlist?list=OLAK5uy_xyz", "OLAK5uy_xyz"),
            (f"https://www.youtube.com/watch?v={VID}&list=PLabc123", "PLabc123"),
        ],
    )
    def test_playlist_forms(self, url, expected):
        assert parse_source(url) == ("yt-playlist", expected)

    def test_radio_mix_resolves_to_the_video(self):
        """An RD… list is an endless auto-generated mix; someone pasting a song
        link wants the song, not a bottomless queue."""
        assert parse_source(f"https://www.youtube.com/watch?v={VID}&list=RD{VID}") == (
            "yt-video",
            VID,
        )

    @pytest.mark.parametrize(
        "bad", ["", "   ", "hello world", "https://open.spotify.com/playlist/abc"]
    )
    def test_rejects_non_youtube(self, bad):
        with pytest.raises(YouTubeError):
            parse_source(bad)


class TestTitleCleanup:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Artist - Song (Official Video)", "Artist - Song"),
            ("Artist - Song [Official Audio]", "Artist - Song"),
            ("Artist - Song (Official Music Video)", "Artist - Song"),
            ("Artist - Song (Lyrics)", "Artist - Song"),
            ("Artist - Song (HD)", "Artist - Song"),
            ("Artist - Song - Official Video", "Artist - Song"),
            ("Artist - Song", "Artist - Song"),
        ],
    )
    def test_strips_uploader_clutter(self, raw, expected):
        assert _clean_title(raw) == expected

    def test_splits_artist_and_title(self):
        assert _split_artist_title("Radiohead - Karma Police", "") == (
            ["Radiohead"],
            "Karma Police",
        )

    def test_falls_back_to_channel_without_a_dash(self):
        artists, title = _split_artist_title("Just A Song", "Some Channel - Topic")
        assert artists == ["Some Channel"]     # the " - Topic" suffix is dropped
        assert title == "Just A Song"

    def test_splits_multiple_artists(self):
        artists, _ = _split_artist_title("A & B - Song", "")
        assert artists == ["A", "B"]


class TestEntryToTrack:
    def test_builds_a_direct_track(self):
        entry = {"id": VID, "title": "Radiohead - Karma Police (Official Video)",
                 "duration": 264, "uploader": "Radiohead"}
        t = _to_track(entry, 1)
        assert t.video_id == VID           # carries its own recording
        assert t.artists == ["Radiohead"]
        assert t.title == "Karma Police"
        assert t.duration_ms == 264_000
        assert t.cover_url.endswith(f"{VID}/hqdefault.jpg")

    def test_prefers_youtube_music_fields_when_present(self):
        entry = {"id": VID, "title": "whatever the video is called", "duration": 200,
                 "track": "Real Title", "artist": "Real Artist", "album": "Real Album"}
        t = _to_track(entry, 1)
        assert (t.title, t.artists, t.album) == ("Real Title", ["Real Artist"], "Real Album")

    @pytest.mark.parametrize(
        "entry",
        [
            {"id": "tooshort", "title": "x"},
            {"id": VID, "title": "[Deleted video]"},
            {"id": VID, "title": "[Private video]"},
            {"id": VID, "title": "Live now", "live_status": "is_live"},
        ],
    )
    def test_skips_what_cannot_be_downloaded(self, entry):
        assert _to_track(entry) is None
