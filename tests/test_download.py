"""Tag writing and the error text users actually read.

Tagging is exercised against a real Opus file generated locally with ffmpeg, so
it covers the actual mutagen round-trip without downloading anything.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
from mutagen.oggopus import OggOpus

from spt2yt.download import _tidy_error, write_tags
from spt2yt.youtube import _tidy

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


@pytest.fixture
def opus_file(tmp_path):
    """One second of silence, encoded as Opus in Ogg -- the shape yt-dlp hands us."""
    path = tmp_path / "sample.opus"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-t", "1", "-c:a", "libopus", str(path)],
        check=True, capture_output=True,
    )
    return path


class TestWriteTags:
    def test_writes_every_field(self, opus_file, track):
        t = track(
            title="Berrak", artists=["Pilli Bebek"], album="Uyandırmadan",
            album_artist="Pilli Bebek", release_date="2000", track_number=4,
            disc_number=1, isrc="TRAET1900317", url="https://open.spotify.com/track/x",
        )
        write_tags(opus_file, t, "https://music.youtube.com/watch?v=x")

        audio = OggOpus(opus_file)
        assert audio["title"] == ["Berrak"]
        assert audio["artist"] == ["Pilli Bebek"]
        assert audio["album"] == ["Uyandırmadan"]
        assert audio["albumartist"] == ["Pilli Bebek"]
        assert audio["date"] == ["2000"]
        assert audio["tracknumber"] == ["4"]
        assert audio["isrc"] == ["TRAET1900317"]
        assert audio["spotifyid"] == [t.id]

    def test_multiple_artists_are_kept_separate(self, opus_file, track):
        """One tag per artist, not a joined string -- players split on them."""
        write_tags(opus_file, track(artists=["BABYMETAL", "F.HERO"]), "")
        assert OggOpus(opus_file)["artist"] == ["BABYMETAL", "F.HERO"]

    def test_non_ascii_survives_the_round_trip(self, opus_file, track):
        t = track(title="Üçüncü Şarkı", artists=["Sanatçı Ç"], album="Şarkılar")
        write_tags(opus_file, t, "")
        audio = OggOpus(opus_file)
        assert audio["title"] == ["Üçüncü Şarkı"]
        assert audio["artist"] == ["Sanatçı Ç"]

    def test_empty_fields_are_omitted_not_blank(self, opus_file, track):
        write_tags(opus_file, track(album="", isrc="", release_date=""), "")
        audio = OggOpus(opus_file)
        assert "album" not in audio
        assert "isrc" not in audio

    def test_missing_cover_url_is_not_fatal(self, opus_file, track):
        write_tags(opus_file, track(cover_url=""), "")
        assert OggOpus(opus_file)["title"]          # tags still written
        assert "metadata_block_picture" not in OggOpus(opus_file)

    def test_unreadable_file_does_not_raise(self, tmp_path, track):
        """The audio is already on disk by this point; a tagging failure must
        not lose the download."""
        bogus = tmp_path / "not-audio.opus"
        bogus.write_bytes(b"definitely not an ogg stream")
        write_tags(bogus, track(), "")      # must not raise


class TestErrorMessages:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("ERROR: Private video. Sign in if you've been granted access", "private"),
            ("ERROR: Video unavailable", "unavailable"),
            ("ERROR: This video is not available in your country", "unavailable"),
            ("ERROR: Sign in to confirm your age", "age"),
        ],
    )
    def test_yt_dlp_errors_become_readable(self, raw, expected):
        assert expected in _tidy_error(raw).lower()

    def test_unknown_errors_are_passed_through_trimmed(self):
        message = _tidy_error("ERROR: something nobody anticipated\nstack trace line")
        assert "something nobody anticipated" in message
        assert "stack trace" not in message      # only the first line
        assert len(message) <= 200

    def test_empty_error_still_says_something(self):
        assert _tidy_error("")

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("ERROR: This playlist is private", "private"),
            ("ERROR: The playlist does not exist", "exist"),
            ("ERROR: Please sign in", "signed-in"),
        ],
    )
    def test_youtube_fetch_errors_become_readable(self, raw, expected):
        assert expected in _tidy(raw).lower()
