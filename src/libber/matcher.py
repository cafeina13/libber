"""Match a Spotify track to a YouTube Music recording.

Search alone is not enough. YouTube Music will happily hand back a live cut, a
sped-up edit or a karaoke backing track as the top hit for a perfectly ordinary
query, so every candidate is scored on title, artist and duration, then
penalised for "variant" words that imply a different recording than the one
Spotify has.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz
from ytmusicapi import YTMusic

from .spotify import Track

_local = threading.local()


def client() -> YTMusic:
    """One YTMusic per thread; the underlying requests.Session isn't shared."""
    ytm = getattr(_local, "ytm", None)
    if ytm is None:
        ytm = YTMusic()  # unauthenticated: search works, no login needed
        _local.ytm = ytm
    return ytm


# Parentheticals that describe the *same* recording -- safe to strip.
_HARMLESS = re.compile(
    r"\s*[\(\[][^)\]]*?\b(remaster(ed)?|deluxe|bonus track|mono|stereo|explicit|"
    r"album version|single version|original mix|anniversary|reissue|\d{4})\b[^)\]]*?[\)\]]",
    re.IGNORECASE,
)
_FEAT = re.compile(
    r"\s*[\(\[]?\s*\b(feat\.?|ft\.?|featuring|w/|with)\b\s*[^)\]]*[\)\]]?",
    re.IGNORECASE,
)
_DASH_TAIL = re.compile(
    r"\s+-\s+[^-]*\b(remaster(ed)?|version|mix|edit|mono|stereo|\d{4})\b.*$",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")

# Words that mean "this is a different recording". Matching one of these in a
# candidate when the source track has no such word is the single strongest
# signal that the match is wrong.
VARIANTS: dict[str, tuple[str, ...]] = {
    "live": ("live", "en vivo", "en directo", "concert", "unplugged", "session"),
    "remix": ("remix", "rmx", "bootleg", "flip", "vip mix"),
    "cover": ("cover", "covered by", "tribute", "made famous by", "in the style of"),
    "karaoke": ("karaoke", "backing track", "instrumental", "playback"),
    "acoustic": ("acoustic", "stripped", "piano version"),
    "edit": ("sped up", "speed up", "slowed", "reverb", "nightcore", "daycore", "8d audio"),
    "demo": ("demo", "rehearsal", "outtake"),
    "mashup": ("mashup", "medley"),
    "extended": ("extended", "club mix", "12\" mix"),
}
_LABELS = {
    "live": "live version",
    "remix": "remix",
    "cover": "cover",
    "karaoke": "karaoke/instrumental",
    "acoustic": "acoustic version",
    "edit": "sped up / slowed edit",
    "demo": "demo",
    "mashup": "mashup",
    "extended": "extended mix",
}


_SCRIPTS = {
    "cjk": re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]"),
    "hangul": re.compile(r"[ᄀ-ᇿ가-힯]"),
    "cyrillic": re.compile(r"[Ѐ-ӿ]"),
    "greek": re.compile(r"[Ͱ-Ͽ]"),
    "arabic": re.compile(r"[؀-ۿ]"),
    "latin": re.compile(r"[A-Za-z]"),
}


def scripts_in(text: str) -> frozenset[str]:
    return frozenset(name for name, rx in _SCRIPTS.items() if rx.search(text or ""))


def comparable(left: list[str], right: list[str]) -> bool:
    """Whether two name lists are written in a shared script.

    Spotify romanises artists that YouTube Music leaves in their native script
    (and the reverse), so "Yousei Teikoku" and "妖精帝國" are the same act with
    zero characters in common. Fuzzy matching cannot see that, so rather than
    scoring them as a mismatch we decline to compare them at all.
    """
    a = frozenset().union(*(scripts_in(x) for x in left)) if left else frozenset()
    b = frozenset().union(*(scripts_in(x) for x in right)) if right else frozenset()
    return not a or not b or bool(a & b)


def normalise(text: str) -> str:
    text = _HARMLESS.sub(" ", text or "")
    text = _DASH_TAIL.sub(" ", text)
    text = _FEAT.sub(" ", text)
    text = _PUNCT.sub(" ", text.lower())
    return _SPACE.sub(" ", text).strip()


# Words whose meaning changes between a track title and an album title. An
# album called "(Extended)" is a deluxe edition with bonus tracks, not a record
# of extended mixes -- reading it as one penalised the correct recording of
# Halsey's "Easier than Lying" for not being a remix. The rest survive the move
# intact: a "Live at ..." album really does hold live takes, an "Acoustic"
# album really is acoustic, "Demos" really are demos.
_TITLE_ONLY = {"extended"}


def variants_in(text: str, *, album: bool = False) -> set[str]:
    low = f" {_PUNCT.sub(' ', (text or '').lower())} "
    low = _SPACE.sub(" ", low)
    found = set()
    for key, words in VARIANTS.items():
        if any(f" {w} " in low for w in words):
            found.add(key)
    return found - _TITLE_ONLY if album else found


@dataclass
class Candidate:
    video_id: str
    title: str
    artists: list[str]
    album: str
    duration_s: float
    source: str  # "song" | "video"
    score: float = 0.0
    flags: list[str] = field(default_factory=list)
    # Set when the candidate looks like a *different recording* rather than
    # merely a weak match. These never auto-download, whatever the score.
    risky: bool = False

    @property
    def url(self) -> str:
        return f"https://music.youtube.com/watch?v={self.video_id}"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Candidate":
        """Rebuild a candidate stored in the review queue on disk."""
        artist = raw.get("artist") or ""
        return cls(
            video_id=raw.get("video_id", ""),
            title=raw.get("title", ""),
            artists=[a.strip() for a in artist.split(",") if a.strip()],
            album=raw.get("album", ""),
            duration_s=float(raw.get("duration_s") or 0),
            source=raw.get("source", "song"),
            score=float(raw.get("score") or 0),
            flags=list(raw.get("flags") or []),
            risky=bool(raw.get("risky")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "artist": ", ".join(self.artists),
            "album": self.album,
            "duration_s": round(self.duration_s),
            "score": round(min(self.score, 100.0), 1),
            "flags": self.flags,
            "risky": self.risky,
            "url": self.url,
            "source": self.source,
        }


# Below this, two same-script titles are different songs, whatever else agrees.
# Genuine matches clear it easily: remaster and "(feat. …)" noise is stripped
# before comparing, so real pairs land at or near 100.
TITLE_FLOOR = 60.0


def _duration_score(delta: float) -> float:
    """Full marks within 2s, nothing past 15s. Duration is the honest signal."""
    delta = abs(delta)
    if delta <= 2:
        return 100.0
    if delta >= 15:
        return 0.0
    return 100.0 * (1 - (delta - 2) / 13.0)


def score(track: Track, cand: Candidate) -> Candidate:
    q_title, c_title = normalise(track.title), normalise(cand.title)
    # token_set_ratio only. partial_ratio was also consulted, which scores the
    # best-matching character window and so reads incidental overlap between
    # two short unrelated titles as similarity -- "Seni Yazdim" against "Her
    # Seye Ragmen" came out at 42 rather than 31. It never helped: a title with
    # extra words is already handled by comparing token sets.
    title_score = float(fuzz.token_set_ratio(q_title, c_title))

    q_artists = [normalise(a) for a in track.artists] or [""]
    c_artists = [normalise(a) for a in cand.artists] or [normalise(cand.title)]
    cross_script = not comparable(track.artists, cand.artists)
    if cross_script:
        # Same name, different writing system. Abstain: award neither credit
        # nor penalty, and let title and duration decide.
        artist_score = 50.0
    else:
        artist_score = max(
            (fuzz.token_set_ratio(qa, ca) for qa in q_artists for ca in c_artists),
            default=0.0,
        )

    delta = cand.duration_s - track.duration_s
    dur_score = _duration_score(delta) if cand.duration_s else 50.0

    total = 0.42 * title_score + 0.33 * artist_score + 0.25 * dur_score

    flags: list[str] = []
    # Variant words the source track doesn't have -> almost certainly wrong take.
    # The album name matters as much as the title here: karaoke and tribute
    # pressings often have a clean track title and give themselves away only
    # in the album ("Zoom Karaoke - Seventies", "The Music of Queen").
    src_variants = variants_in(track.title) | variants_in(track.album, album=True)
    cand_variants = variants_in(cand.title) | variants_in(cand.album, album=True)
    risky = False

    # The title is the only signal that establishes *which song* this is.
    # Artist and duration agree for every other track on the same album, and
    # together they are worth 58 of the 100 points -- so without this a
    # different song by the right artist at the right length scored 74.8 and
    # sailed past the default threshold. No amount of agreement elsewhere makes
    # one title into another.
    if not comparable([track.title], [cand.title]):
        # Romanised against native script: unreadable rather than wrong, so
        # hand it to a human instead of guessing either way.
        flags.append("title in another script — not compared")
        risky = True
    elif title_score < TITLE_FLOOR:
        total -= 30
        flags.append("different title")
        risky = True

    for key in cand_variants - src_variants:
        total -= 26
        flags.append(_LABELS[key])
        risky = True
    # ...and the reverse: we wanted the live cut and got the studio one.
    # The reverse direction is far weaker evidence than the forward one. A
    # candidate announcing "Live" when the source is a studio track is close to
    # proof it is the wrong take; a candidate merely failing to announce
    # "acoustic" proves nothing, because a release that is entirely acoustic
    # titles its tracks plainly. Ruelle's "Monsters" from "Monsters (Acoustic
    # Version)" matched a candidate titled just "Monsters" -- to the same
    # second -- and was docked for it. Duration settles that question better
    # than a label does, so when it agrees exactly, say nothing.
    if abs(delta) > 2:
        for key in src_variants - cand_variants:
            total -= 10
            flags.append(f"missing: {_LABELS[key]}")

    # A different performer is how covers sneak in, and the weighted artist
    # term alone isn't decisive enough -- Pentatonix's "Bohemian Rhapsody"
    # otherwise outranks Queen's. Penalise the mismatch outright.
    if cross_script:
        flags.append("artist name in another script — not compared")
    elif artist_score < 45:
        total -= 25
        flags.append("different artist")
        risky = True
    elif artist_score < 65:
        total -= 12
        flags.append("artist mismatch")

    # _duration_score floors at 0, which makes a 20s mismatch and a 5-minute
    # one look identical. Anything wildly off is a different thing entirely --
    # an excerpt, a full-album upload, a extended mix -- so disqualify it.
    if cand.duration_s and track.duration_s:
        # A remaster drifts by seconds, not by a fifth of the runtime.
        ratio = cand.duration_s / track.duration_s
        if ratio < 0.85:
            total -= 35
            flags.append("much shorter — excerpt or edit")
            risky = True
        elif ratio > 1.25:
            total -= 35
            flags.append("much longer — extended or not this track alone")
            risky = True

    if cand.album and normalise(cand.album) == normalise(track.album):
        total += 5
    if cand.source == "video":
        total -= 8  # user-uploaded videos: worse audio, more likely a re-upload
        flags.append("not an official song entry")
    if cand.duration_s and abs(delta) > 5:
        flags.append(f"{'longer' if delta > 0 else 'shorter'} by {abs(delta):.0f}s")

    # Deliberately not capped at 100. A flawless title/artist/duration match
    # already scores exactly 100, so capping here would swallow the album bonus
    # and leave genuine alternatives tied -- which is the one case the bonus
    # exists to settle. Ranking uses the true value; to_dict() caps for display.
    cand.score = max(0.0, total)
    cand.flags = flags
    cand.risky = risky
    return cand


def _parse(results: list[dict[str, Any]], source: str) -> list[Candidate]:
    out = []
    for r in results:
        vid = r.get("videoId")
        if not vid:
            continue
        artists = [a.get("name", "") for a in (r.get("artists") or []) if a.get("name")]
        album = (r.get("album") or {}).get("name", "") if isinstance(r.get("album"), dict) else ""
        out.append(
            Candidate(
                video_id=vid,
                title=r.get("title", ""),
                artists=artists,
                album=album,
                duration_s=float(r.get("duration_seconds") or 0),
                source=source,
            )
        )
    return out


def search(track: Track, limit: int = 6) -> list[Candidate]:
    """Ranked candidates, best first. Falls back to video results if the song
    catalogue has nothing convincing (common for remixes and niche uploads)."""
    ytm = client()
    query = f"{track.artist} {track.title}".strip()

    candidates: list[Candidate] = []
    try:
        candidates += _parse(ytm.search(query, filter="songs", limit=limit), "song")
    except Exception:
        pass

    ranked = sorted((score(track, c) for c in candidates), key=lambda c: -c.score)

    if not ranked or ranked[0].score < 75:
        try:
            extra = _parse(ytm.search(query, filter="videos", limit=4), "video")
            ranked = sorted(
                ranked + [score(track, c) for c in extra], key=lambda c: -c.score
            )
        except Exception:
            pass

    seen, unique = set(), []
    for c in ranked:
        if c.video_id in seen:
            continue
        seen.add(c.video_id)
        unique.append(c)
    return unique
