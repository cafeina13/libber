"""Metadata enrichment for YouTube-sourced tracks.

Enrichment is optional, so the bar is asymmetric: missing an album is a
shrug, attaching the wrong one silently mislabels the library. These tests
lean on the refusal side.
"""

from __future__ import annotations

import pytest

from libber.enrich import ACCEPT_SCORE, _apply, _score


def sp_track(name, artists=("Test Artist",), duration_ms=200_000, album="Test Album",
             release="2020-01-01", isrc="TEST00000001", track_number=1, art=640):
    """A Spotify search result, trimmed to the fields enrichment reads."""
    return {
        "name": name,
        "artists": [{"name": a} for a in artists],
        "duration_ms": duration_ms,
        "track_number": track_number,
        "disc_number": 1,
        "external_ids": {"isrc": isrc},
        "album": {
            "name": album,
            "artists": [{"name": artists[0]}],
            "release_date": release,
            "images": [
                {"url": f"https://i.scdn.co/{art}", "width": art, "height": art},
                {"url": "https://i.scdn.co/64", "width": 64, "height": 64},
            ],
        },
    }


@pytest.fixture
def yt_track(track):
    """A track as the YouTube source produces it: no album, 16:9 thumbnail."""
    return lambda **kw: track(
        album="", album_artist="", isrc="", release_date="",
        cover_url="https://i.ytimg.com/vi/x/hqdefault.jpg", cover_size=(480, 360),
        video_id="x" * 11, **kw
    )


class TestScoring:
    def test_exact_match_accepted(self, yt_track):
        t = yt_track(title="Chokehold", artists=["Sleep Token"], duration_ms=305_000)
        assert _score(t, sp_track("Chokehold", ["Sleep Token"], 305_000)) >= ACCEPT_SCORE

    def test_small_duration_drift_still_accepted(self, yt_track):
        # Remasters and platform differences shift a track by a second or two.
        t = yt_track(title="Chokehold", artists=["Sleep Token"], duration_ms=305_000)
        assert _score(t, sp_track("Chokehold", ["Sleep Token"], 307_000)) >= ACCEPT_SCORE

    def test_wrong_duration_is_rejected_outright(self, yt_track):
        """A 60s clip and the real song share a title; only duration tells
        them apart, so a big gap scores zero rather than merely low."""
        t = yt_track(title="Chokehold", artists=["Sleep Token"], duration_ms=62_000)
        assert _score(t, sp_track("Chokehold", ["Sleep Token"], 305_000)) == 0.0

    def test_different_artist_rejected(self, yt_track):
        t = yt_track(title="Bohemian Rhapsody", artists=["Queen"], duration_ms=354_000)
        assert _score(t, sp_track("Bohemian Rhapsody", ["Pentatonix"], 354_000)) < ACCEPT_SCORE

    def test_different_title_rejected(self, yt_track):
        t = yt_track(title="Chokehold", artists=["Sleep Token"], duration_ms=305_000)
        assert _score(t, sp_track("Granite", ["Sleep Token"], 305_000)) < ACCEPT_SCORE

    def test_live_track_does_not_borrow_studio_details(self, yt_track):
        t = yt_track(title="Karma Police (Live at Glastonbury)", artists=["Radiohead"],
                     duration_ms=264_000)
        assert _score(t, sp_track("Karma Police", ["Radiohead"], 264_000)) < ACCEPT_SCORE

    def test_studio_track_does_not_take_a_live_album(self, yt_track):
        t = yt_track(title="Karma Police", artists=["Radiohead"], duration_ms=264_000)
        assert _score(
            t, sp_track("Karma Police (Live)", ["Radiohead"], 264_000, album="Live in Dublin")
        ) < ACCEPT_SCORE

    def test_missing_duration_does_not_hard_fail(self, yt_track):
        # Some flat playlist entries carry no duration at all.
        t = yt_track(title="Chokehold", artists=["Sleep Token"], duration_ms=0)
        assert _score(t, sp_track("Chokehold", ["Sleep Token"], 305_000)) > 0


class TestApply:
    def test_copies_metadata_and_upgrades_artwork(self, yt_track):
        t = yt_track(title="Chokehold", artists=["Sleep Token"], duration_ms=305_000)
        _apply(t, sp_track("Chokehold", ["Sleep Token"], 305_000,
                           album="Take Me Back To Eden", release="2023-05-19",
                           isrc="GBUM72200345", track_number=1))
        assert t.album == "Take Me Back To Eden"
        assert t.release_date == "2023-05-19"
        assert t.isrc == "GBUM72200345"
        assert t.album_artist == "Sleep Token"
        # Square album art replaces the 16:9 video thumbnail.
        assert t.cover_size == (640, 640)
        assert "ytimg" not in t.cover_url

    def test_does_not_clobber_existing_values(self, track):
        t = track(album="Already Known", video_id="x" * 11)
        _apply(t, sp_track("Test Song", album="Something Else"))
        assert t.album == "Something Else"  # album is explicitly refreshed
        assert t.title == "Test Song"       # title is never rewritten

    def test_survives_a_result_with_no_artwork(self, yt_track):
        t = yt_track(title="Song", artists=["Artist"])
        payload = sp_track("Song", ["Artist"])
        payload["album"]["images"] = []
        _apply(t, payload)
        assert t.album == "Test Album"
        assert t.cover_url  # the YouTube thumbnail is kept rather than blanked
