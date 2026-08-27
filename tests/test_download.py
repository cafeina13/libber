"""Tag writing and the error text users actually read.

Tagging is exercised against a real Opus file generated locally with ffmpeg, so
it covers the actual mutagen round-trip without downloading anything.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
from mutagen.oggopus import OggOpus

from libber.download import _tidy_error, edited_title, write_tags
from libber.youtube import _tidy

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


class TestHandEditedTitles:
    """Two releases of one song carry the same title, so the only way to tell
    them apart in a flat song list is to edit the tag -- "Yalan (Canlı)".
    Re-downloading used to write the Spotify title straight back over it."""

    def test_spots_an_edited_title(self, opus_file, track):
        write_tags(opus_file, track(title="Yalan"), "")
        audio = OggOpus(opus_file)
        audio["title"] = ["Yalan (Canlı)"]
        audio.save()
        assert edited_title(opus_file, "Yalan") == "Yalan (Canlı)"

    def test_untouched_title_is_not_an_edit(self, opus_file, track):
        write_tags(opus_file, track(title="Yalan"), "")
        assert edited_title(opus_file, "Yalan") == ""

    def test_unreadable_file_reports_no_edit(self, tmp_path):
        bogus = tmp_path / "not-audio.opus"
        bogus.write_bytes(b"definitely not an ogg stream")
        assert edited_title(bogus, "Yalan") == ""

    def test_untagged_file_reports_no_edit(self, opus_file):
        assert edited_title(opus_file, "Yalan") == ""

    def test_a_file_owned_by_another_release_is_not_read(self, opus_file, track):
        """Two releases often share one file; its title belongs to whichever
        one fetched it, not to whoever is downloading now."""
        t = track(title="Yalan", url="https://open.spotify.com/track/x")
        write_tags(opus_file, t, "", keep_title="Yalan (Canlı)")
        assert edited_title(opus_file, "Yalan", "some-other-track") == ""
        assert edited_title(opus_file, "Yalan", t.id) == "Yalan (Canlı)"

    def test_a_file_with_no_owner_tag_is_still_read(self, opus_file, track):
        """Files written before ids were embedded still belong to their track."""
        write_tags(opus_file, track(title="Yalan"), "")   # no url -> no spotifyid
        audio = OggOpus(opus_file)
        audio["title"] = ["Yalan (Canlı)"]
        audio.save()
        assert edited_title(opus_file, "Yalan", "any-id") == "Yalan (Canlı)"

    def test_keep_title_survives_a_rewrite(self, opus_file, track):
        """The regression: an upgrade to a better stream overwrites the file,
        then re-tags it from Spotify."""
        write_tags(opus_file, track(title="Yalan"), "", keep_title="Yalan (Canlı)")
        assert OggOpus(opus_file)["title"] == ["Yalan (Canlı)"]

    def test_keep_title_does_not_disturb_other_fields(self, opus_file, track):
        t = track(title="Yalan", album="Canlı", isrc="TRAET1900317",
                  url="https://open.spotify.com/track/x")
        write_tags(opus_file, t, "", keep_title="Yalan (Canlı)")
        audio = OggOpus(opus_file)
        assert audio["album"] == ["Canlı"]
        assert audio["isrc"] == ["TRAET1900317"]
        assert audio["spotifyid"] == [t.id]

    def test_no_keep_title_writes_the_spotify_one(self, opus_file, track):
        write_tags(opus_file, track(title="Yalan"), "", keep_title="")
        assert OggOpus(opus_file)["title"] == ["Yalan"]


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
