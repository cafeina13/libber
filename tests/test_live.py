"""Tests that call YouTube and Spotify for real.

Skipped unless you pass --network. They are slow, they go red when a service
changes its responses, and one of them downloads audio -- none of which belongs
in a default test run. Fixtures are deliberately public and impersonal: no
private playlists, no account ids.

    uv run pytest --network
    uv run pytest --network -m "network and not spotify"
"""

from __future__ import annotations

import pytest

from libber import enrich, matcher, youtube
from libber.download import fetch_audio, write_tags
from libber.jobs import _direct_candidate

pytestmark = pytest.mark.network

# A Creative Commons track, so the download test isn't pulling copyrighted audio.
CC_TITLE = "Monkeys Spinning Monkeys"
CC_ARTIST = "Kevin MacLeod"


class TestYouTubeMusicSearch:
    def test_finds_a_well_known_track(self, track):
        t = track(title="Karma Police", artists=["Radiohead"], album="OK Computer",
                  duration_ms=264_000)
        results = matcher.search(t)
        assert results
        best = results[0]
        assert "radiohead" in ", ".join(best.artists).lower()
        assert abs(best.duration_s - 264) < 10
        assert best.score > 80

    def test_results_are_ranked_best_first(self, track):
        t = track(title="Teardrop", artists=["Massive Attack"], duration_ms=330_000)
        scores = [c.score for c in matcher.search(t)]
        assert scores == sorted(scores, reverse=True)

    def test_nonsense_query_does_not_produce_a_confident_match(self, track):
        t = track(title="zzzq unlikely track name 84721", artists=["No Such Artist 99182"],
                  duration_ms=200_000)
        results = matcher.search(t)
        assert not results or results[0].score < 70


class TestYouTubeFetch:
    def test_single_video(self):
        pl = youtube.fetch("https://www.youtube.com/watch?v=8kf0DpAwK1A")
        assert pl.kind == "yt-video"
        assert len(pl.tracks) == 1
        assert pl.is_direct
        assert pl.tracks[0].video_id == "8kf0DpAwK1A"

    def test_album_playlist_gets_tracks_and_an_album_name(self):
        pl = youtube.fetch(
            "https://music.youtube.com/playlist?list=OLAK5uy_nfeZcBZ_whZMPXn8oF7unN3763k_SxzSo"
        )
        assert pl.kind == "yt-playlist"
        assert len(pl.tracks) > 5
        assert pl.is_direct
        assert not pl.name.lower().startswith("album -")  # prefix stripped
        assert all(t.album for t in pl.tracks)            # filled from the playlist

    def test_missing_playlist_raises_cleanly(self):
        with pytest.raises(youtube.YouTubeError):
            youtube.fetch("https://www.youtube.com/playlist?list=PLdoesnotexist000000")


@pytest.mark.spotify
class TestEnrichment:
    def test_fills_in_album_and_square_art(self, spotify_client, track):
        t = track(title="Karma Police", artists=["Radiohead"], album="", isrc="",
                  release_date="", duration_ms=264_000, cover_size=(480, 360),
                  video_id="x" * 11)
        album = enrich.from_spotify(t, spotify_client)
        assert album == "OK Computer"
        assert t.isrc and t.release_date.startswith("1997")
        assert t.cover_size[0] == t.cover_size[1]  # square

    def test_refuses_a_clip_masquerading_as_the_song(self, spotify_client, track):
        t = track(title="Karma Police", artists=["Radiohead"], album="", duration_ms=45_000,
                  video_id="x" * 11)
        assert enrich.from_spotify(t, spotify_client) is None


class TestDownload:
    def test_downloads_and_tags_without_re_encoding(self, tmp_path, track):
        t = track(title=CC_TITLE, artists=[CC_ARTIST], album="Comedy",
                  album_artist=CC_ARTIST, duration_ms=125_000)
        results = matcher.search(t)
        assert results

        dest = tmp_path / "out.opus"
        result = fetch_audio(results[0], dest, lambda frac, note: None)
        assert result.path.exists()
        assert result.path.stat().st_size > 100_000

        write_tags(result.path, t, results[0].url)
        from mutagen.oggopus import OggOpus

        audio = OggOpus(result.path)
        assert audio["title"] == [CC_TITLE]
        assert audio["artist"] == [CC_ARTIST]

    def test_direct_candidate_skips_matching(self, tmp_path):
        pl = youtube.fetch("https://www.youtube.com/watch?v=8kf0DpAwK1A")
        candidate = _direct_candidate(pl.tracks[0])
        assert candidate.video_id == "8kf0DpAwK1A"
        assert candidate.score == 100.0
