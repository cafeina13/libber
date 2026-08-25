"""Spotify link parsing and response shaping -- offline."""

from __future__ import annotations

import pytest

from spt2yt.spotify import SpotifyError, _pick_cover, parse_source

PID = "37i9dQZF1DXcBWIGoYBM5M"


class TestParseSource:
    @pytest.mark.parametrize(
        "raw",
        [
            f"https://open.spotify.com/playlist/{PID}",
            f"http://open.spotify.com/playlist/{PID}",
            f"https://open.spotify.com/playlist/{PID}?si=abc123",
            f"spotify:playlist:{PID}",
            f"  https://open.spotify.com/playlist/{PID}  ",
        ],
    )
    def test_playlist_forms(self, raw):
        assert parse_source(raw) == ("playlist", PID)

    def test_localised_links(self):
        # Spotify inserts a locale segment for non-English clients.
        assert parse_source(f"https://open.spotify.com/intl-tr/playlist/{PID}") == (
            "playlist",
            PID,
        )

    def test_album_and_track(self):
        assert parse_source(f"https://open.spotify.com/album/{PID}") == ("album", PID)
        assert parse_source(f"spotify:track:{PID}") == ("track", PID)

    @pytest.mark.parametrize("raw", ["liked", "Liked Songs", "SAVED", " liked "])
    def test_liked_songs_keyword(self, raw):
        assert parse_source(raw) == ("liked", "liked")

    def test_bare_id_assumed_to_be_a_playlist(self):
        assert parse_source(PID) == ("playlist", PID)

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "not a link",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://open.spotify.com/playlist/tooshort",
        ],
    )
    def test_rejects_junk(self, raw):
        with pytest.raises(SpotifyError):
            parse_source(raw)

    def test_error_message_is_actionable(self):
        with pytest.raises(SpotifyError, match="open.spotify.com"):
            parse_source("garbage")


class TestPickCover:
    def test_picks_the_largest(self):
        url, size = _pick_cover([
            {"url": "small", "width": 64, "height": 64},
            {"url": "big", "width": 640, "height": 640},
            {"url": "mid", "width": 300, "height": 300},
        ])
        assert (url, size) == ("big", (640, 640))

    def test_handles_no_images(self):
        assert _pick_cover([]) == ("", (0, 0))

    def test_handles_missing_dimensions(self):
        url, _ = _pick_cover([{"url": "only"}])
        assert url == "only"
