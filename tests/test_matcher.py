"""Scoring rules that decide whether a download is the right recording.

Each of these pins down a mistake the matcher actually made before it was
fixed, so the comments name the failure rather than restating the assertion.
"""

from __future__ import annotations

import pytest

from libber.matcher import (
    Candidate,
    _duration_score,
    comparable,
    normalise,
    score,
    scripts_in,
    variants_in,
)


def cand(title, artists=("Test Artist",), duration_s=200.0, album="", source="song"):
    return Candidate(
        video_id="x" * 11,
        title=title,
        artists=list(artists),
        album=album,
        duration_s=duration_s,
        source=source,
    )


class TestNormalise:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Bohemian Rhapsody - 2011 Remaster", "bohemian rhapsody"),
            ("Song (Remastered 2009)", "song"),
            ("Song (Deluxe Edition)", "song"),
            ("Someone Like You (feat. Adele)", "someone like you"),
            ("Track ft. Someone", "track"),
            ("Plain Title", "plain title"),
        ],
    )
    def test_strips_noise_that_means_the_same_recording(self, raw, expected):
        assert normalise(raw) == expected

    def test_keeps_words_that_change_the_recording(self):
        # "Live" must survive normalisation or the variant check can't see it.
        assert "live" in normalise("Karma Police (Live at Glastonbury)")


class TestVariantDetection:
    @pytest.mark.parametrize(
        "title, expected",
        [
            ("Song (Live at Wembley)", "live"),
            ("Song (Tiesto Remix)", "remix"),
            ("Song - Karaoke Version", "karaoke"),
            ("Song (Instrumental)", "karaoke"),
            ("Song (Acoustic)", "acoustic"),
            ("Song (sped up)", "edit"),
            ("Song (slowed + reverb)", "edit"),
            ("Song (Extended Mix)", "extended"),
        ],
    )
    def test_flags_different_recordings(self, title, expected):
        assert expected in variants_in(title)

    def test_plain_title_has_no_variants(self):
        assert variants_in("Just A Normal Song") == set()

    def test_substring_does_not_false_positive(self):
        # "live" inside "Delivery" must not read as a live recording.
        assert "live" not in variants_in("Delivery Man")


class TestAlbumEditionsAreNotVariants:
    """An album called "(Extended)" is a deluxe edition, not a set of remixes.

    Halsey's "Easier than Lying" sits on "If I Can't Have Love, I Want Power
    (Extended)". Reading that as a request for an extended mix docked the
    correct recording 10 points and flagged it "missing: extended mix".
    """

    def test_extended_in_an_album_is_ignored(self):
        assert "extended" not in variants_in("Album (Extended)", album=True)

    def test_extended_in_a_title_still_counts(self):
        assert "extended" in variants_in("Song (Extended Mix)")

    @pytest.mark.parametrize(
        "album, expected",
        [
            ("Live at Wembley", "live"),        # really does hold live takes
            ("MTV Unplugged", "live"),
            ("Acoustic Sessions", "acoustic"),
            ("Demo Recordings", "demo"),
            ("Zoom Karaoke - Seventies", "karaoke"),
        ],
    )
    def test_other_words_survive_the_move_to_an_album(self, album, expected):
        assert expected in variants_in(album, album=True)

    def test_the_case_that_shipped(self, track):
        t = track(title="Easier than Lying", artists=["Halsey"],
                  album="If I Can't Have Love, I Want Power (Extended)",
                  duration_ms=206_000)
        result = score(t, cand("Easier than Lying", ["Halsey"], 206,
                               album="If I Can't Have Love, I Want Power"))
        assert result.flags == []
        assert result.score >= 95

    def test_a_real_extended_mix_is_still_flagged(self, track):
        """The guard has to keep working where it was right all along."""
        t = track(title="Song", artists=["Artist"], duration_ms=200_000)
        result = score(t, cand("Song (Extended Mix)", ["Artist"], 200))
        assert result.risky
        assert "extended mix" in result.flags


class TestTitleFloor:
    """A different song by the right artist used to pass.

    Mary Jane's "Seni Yazdim" matched Mary Jane's "Her Seye Ragmen" at 74.8,
    clearing the default threshold of 70. Artist and duration agree for every
    other track on the same album and are worth 58 points between them, so the
    title carried almost no veto. Only the title says *which song* this is.
    """

    def test_the_case_that_shipped(self, track):
        t = track(title="Seni Yazdım", artists=["Mary Jane"], duration_ms=200_000)
        result = score(t, cand("Her Şeye Rağmen", ["Mary Jane"], 200))
        assert result.risky                     # parked at any threshold
        assert "different title" in result.flags
        assert result.score < 70

    @pytest.mark.parametrize(
        "wanted, got",
        [
            ("Bak", "Olsun"),
            ("Berrak", "Haram Geceler"),
            ("Chokehold", "Granite"),
            ("Idioteque", "The National Anthem"),
        ],
    )
    def test_other_tracks_by_the_same_artist_are_refused(self, track, wanted, got):
        """Same artist, plausible length -- the everyday failure this guards."""
        t = track(title=wanted, artists=["Same Artist"], duration_ms=200_000)
        result = score(t, cand(got, ["Same Artist"], 200))
        assert result.risky
        assert "different title" in result.flags

    def test_risky_regardless_of_score(self, track):
        """The point of the flag: a wrong title is held even when everything
        else is perfect, so the default threshold protects people too."""
        t = track(title="One Song", artists=["Artist"], album="Al", duration_ms=200_000)
        result = score(t, cand("Totally Different", ["Artist"], 200, album="Al"))
        assert result.risky

    @pytest.mark.parametrize(
        "wanted, got",
        [
            ("Chokehold", "Chokehold"),
            ("Teardrop", "Teardrop (feat. Elizabeth Fraser)"),
            ("Bohemian Rhapsody", "Bohemian Rhapsody - 2011 Remaster"),
            ("Kingslayer", "Kingslayer (feat. BABYMETAL)"),
            ("Seni Yazdım", "Seni Yazdım"),
        ],
    )
    def test_real_matches_are_untouched(self, track, wanted, got):
        t = track(title=wanted, artists=["Artist"], duration_ms=200_000)
        result = score(t, cand(got, ["Artist"], 200))
        assert "different title" not in result.flags
        assert not result.risky

    def test_titles_in_different_scripts_are_not_judged(self, track):
        """Romanised against native script is unreadable, not wrong -- so it
        goes to a human rather than being scored as a mismatch."""
        t = track(title="Kuusou Mesorogiwi", artists=["Yousei Teikoku"],
                  duration_ms=243_000)
        result = score(t, cand("空想メソロギヰ", ["妖精帝國"], 240))
        assert "different title" not in result.flags
        assert result.risky                      # held for review instead
        assert any("another script" in f for f in result.flags)


class TestTitleScoring:
    """partial_ratio used to be consulted alongside token_set_ratio. It scores
    the best-matching character window, so two short unrelated titles picked up
    incidental overlap -- and it never helped, because token comparison already
    covers a title with extra words."""

    def test_extra_words_still_match(self, track):
        t = track(title="Teardrop", artists=["Massive Attack"], duration_ms=330_000)
        assert score(t, cand("Teardrop (feat. Elizabeth Fraser)",
                             ["Massive Attack"], 331)).score >= 95

    def test_unrelated_short_titles_score_low(self, track):
        t = track(title="Seni Yazdım", artists=["Mary Jane"], duration_ms=200_000)
        # Was inflated to 40 by the character-window comparison.
        assert score(t, cand("Her Şeye Rağmen", ["Mary Jane"], 200)).score < 50


class TestCrossScriptArtists:
    """Spotify romanises artists that YouTube Music leaves in their native
    script. "Yousei Teikoku" and "妖精帝國" share no characters, so fuzzy
    matching read the correct official release as a different artist and buried
    it at 39.6, below a fan video.
    """

    @pytest.mark.parametrize(
        "name, expected",
        [
            ("Yousei Teikoku", "latin"),
            ("妖精帝國", "cjk"),
            ("初音ミク", "cjk"),
            ("방탄소년단", "hangul"),
            ("Пикник", "cyrillic"),
            ("Sanatçı Ç", "latin"),   # Latin with diacritics is still Latin
        ],
    )
    def test_script_detection(self, name, expected):
        assert expected in scripts_in(name)

    def test_names_in_different_scripts_are_not_comparable(self):
        assert comparable(["Yousei Teikoku"], ["妖精帝國"]) is False
        assert comparable(["BTS"], ["방탄소년단"]) is False

    def test_names_in_the_same_script_are_comparable(self):
        assert comparable(["Queen"], ["Pentatonix"]) is True
        assert comparable(["妖精帝國"], ["初音ミク"]) is True

    def test_missing_names_do_not_block_comparison(self):
        assert comparable([], ["Anyone"]) is True
        assert comparable(["Anyone"], []) is True

    def test_mixed_script_name_stays_comparable(self):
        # "BABYMETAL (ベビーメタル)" carries both, so there is common ground.
        assert comparable(["BABYMETAL"], ["BABYMETAL (ベビーメタル)"]) is True

    def test_official_release_is_not_punished_for_its_script(self, track):
        t = track(title="空想メソロギヰ", artists=["Yousei Teikoku"],
                  album="PAX VESANIA", duration_ms=243_000)
        result = score(t, cand("空想メソロギヰ", ["妖精帝國"], 240, album="PAX VESANIA"))
        assert result.score > 75          # was 39.6 before
        assert not result.risky
        assert "different artist" not in result.flags
        assert any("another script" in f for f in result.flags)

    def test_abstaining_still_relies_on_title_and_duration(self, track):
        """Declining to compare artists must not wave through a different song
        that happens to be in another script."""
        t = track(title="空想メソロギヰ", artists=["Yousei Teikoku"], duration_ms=243_000)
        wrong_song = score(t, cand("全然違う歌", ["妖精帝國"], 243))
        assert wrong_song.score < 60

    def test_same_script_covers_are_still_caught(self, track):
        """The abstain path must not weaken the cover check it sits beside."""
        t = track(title="Bohemian Rhapsody", artists=["Queen"], duration_ms=354_000)
        result = score(t, cand("Bohemian Rhapsody", ["Pentatonix"], 356))
        assert result.risky
        assert "different artist" in result.flags


class TestDurationScore:
    def test_exact_and_near_exact_score_full(self):
        assert _duration_score(0) == 100.0
        assert _duration_score(2) == 100.0

    def test_decays_then_floors(self):
        assert 0 < _duration_score(8) < 100
        assert _duration_score(15) == 0.0
        assert _duration_score(500) == 0.0  # floors rather than going negative

    def test_symmetric(self):
        assert _duration_score(7) == _duration_score(-7)


class TestScoring:
    def test_perfect_match_scores_top(self, track):
        t = track(title="Chokehold", artists=["Sleep Token"], duration_ms=305_000)
        result = score(t, cand("Chokehold", ["Sleep Token"], 305))
        assert result.score >= 95
        assert not result.risky
        assert result.flags == []

    def test_cover_by_another_artist_loses_to_the_original(self, track):
        """The Pentatonix bug: a note-perfect cover title outranked Queen."""
        t = track(title="Bohemian Rhapsody", artists=["Queen"], duration_ms=354_000)
        original = score(t, cand("Bohemian Rhapsody", ["Queen"], 354))
        cover = score(t, cand("Bohemian Rhapsody", ["Pentatonix"], 356))
        assert cover.score < original.score
        assert cover.risky
        assert "different artist" in cover.flags

    def test_excerpt_is_disqualified(self, track):
        """The "Operatic Section" bug: a 64s excerpt won because the duration
        score floors at zero, making 290s off look like 15s off."""
        t = track(title="Bohemian Rhapsody", artists=["Queen"], duration_ms=354_000)
        excerpt = score(t, cand("Bohemian Rhapsody (Operatic Section)", ["Queen"], 64))
        full = score(t, cand("Bohemian Rhapsody", ["Queen"], 354))
        assert excerpt.score < full.score
        assert excerpt.risky

    def test_full_album_upload_is_disqualified(self, track):
        t = track(title="Chokehold", artists=["Sleep Token"], duration_ms=305_000)
        result = score(t, cand("Take Me Back To Eden (Full Album)", ["Sleep Token"], 3600))
        assert result.risky

    def test_live_version_flagged_when_source_is_studio(self, track):
        t = track(title="Karma Police", artists=["Radiohead"], duration_ms=264_000)
        result = score(t, cand("Karma Police (Live at Glastonbury)", ["Radiohead"], 268))
        assert result.risky
        assert "live version" in result.flags

    def test_live_source_is_not_penalised_for_being_live(self, track):
        # Asking for a live track and getting one is a match, not a variant.
        t = track(title="Karma Police (Live at Glastonbury)", artists=["Radiohead"],
                  duration_ms=264_000)
        result = score(t, cand("Karma Police (Live at Glastonbury)", ["Radiohead"], 264))
        assert "live version" not in result.flags

    def test_karaoke_hides_in_the_album_name(self, track):
        """Karaoke pressings often have a clean track title and give themselves
        away only in the album, so the album is scanned too."""
        t = track(title="Bohemian Rhapsody", artists=["Queen"], duration_ms=354_000)
        result = score(
            t, cand("Bohemian Rhapsody", ["Zoom Karaoke"], 358,
                    album="Zoom Karaoke - Seventies Hits")
        )
        assert result.risky
        assert any("karaoke" in f for f in result.flags)

    def test_matching_album_breaks_a_tie_between_perfect_matches(self, track):
        """The case the bonus exists for. A popular song returns several
        candidates that are all flawless on title, artist and duration -- the
        album is the only thing left to separate the named release from a
        remaster or a compilation. Scores are therefore not capped at 100,
        or these would tie and the order would come down to YouTube's."""
        t = track(title="Song", artists=["Artist"], album="The Album", duration_ms=200_000)
        with_album = score(t, cand("Song", ["Artist"], 200, album="The Album"))
        without = score(t, cand("Song", ["Artist"], 200, album="Compilation Hits"))
        assert without.score == 100.0
        assert with_album.score > without.score

    def test_display_score_is_capped(self, track):
        # Uncapped internally for ranking, capped in the payload so the UI
        # never shows "105".
        t = track(title="Song", artists=["Artist"], album="The Album", duration_ms=200_000)
        best = score(t, cand("Song", ["Artist"], 200, album="The Album"))
        assert best.score > 100.0
        assert best.to_dict()["score"] == 100.0

    def test_video_results_rank_below_song_results(self, track):
        t = track(title="Song", artists=["Artist"], duration_ms=200_000)
        as_song = score(t, cand("Song", ["Artist"], 200, source="song"))
        as_video = score(t, cand("Song", ["Artist"], 200, source="video"))
        assert as_video.score < as_song.score

    def test_stacked_penalties_never_go_negative(self, track):
        """Penalties stack and can far exceed the base score; a negative result
        would sort below nothing and read absurdly in the UI."""
        t = track(title="Song", artists=["Artist"], duration_ms=200_000)
        awful = score(
            t,
            cand("Completely Different (Live) (Remix) (Karaoke)", ["Nobody"], 9000,
                 album="Karaoke Live Remixes", source="video"),
        )
        assert awful.score == 0.0
        assert awful.to_dict()["score"] == 0.0

    def test_to_dict_exposes_what_the_ui_needs(self, track):
        t = track(title="Song", artists=["Artist"], duration_ms=200_000)
        payload = score(t, cand("Song (Live)", ["Artist"], 200)).to_dict()
        assert {"video_id", "title", "artist", "score", "flags", "risky", "url"} <= payload.keys()
        assert payload["url"].startswith("https://music.youtube.com/watch?v=")
