"""Spotify Web API access: auth plus playlist/album/liked-songs reading."""

from __future__ import annotations

import re
from typing import Any, Iterator

import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth

from .config import APP_TOKEN_CACHE, SCOPES, TOKEN_CACHE, Settings, redirect_uri
from .models import Playlist, Track

__all__ = ["Playlist", "SpotifyAuth", "SpotifyError", "Track", "fetch", "parse_source"]


class SpotifyError(RuntimeError):
    pass


_URL_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z]{2}/)?|spotify:)"
    r"(playlist|album|track)[:/]([A-Za-z0-9]{22})"
)


def parse_source(raw: str) -> tuple[str, str]:
    """Turn any Spotify link/URI/bare-id into a (kind, id) pair."""
    text = raw.strip()
    if not text:
        raise SpotifyError("Paste a Spotify playlist link first.")
    if text.lower() in {"liked", "saved", "liked songs"}:
        return "liked", "liked"
    m = _URL_RE.search(text)
    if m:
        return m.group(1), m.group(2)
    if re.fullmatch(r"[A-Za-z0-9]{22}", text):
        return "playlist", text  # bare id, assume playlist
    raise SpotifyError(
        "That doesn't look like a Spotify playlist/album link. Expected something "
        "like https://open.spotify.com/playlist/37i9dQZF1DX..."
    )


class SpotifyAuth:
    """Holds both auth strategies and picks whichever the request can use.

    Client-credentials covers public playlists with no user login. Anything
    private (or Liked Songs) needs the authorization-code flow.
    """

    def __init__(self, settings: Settings, port: int) -> None:
        self.settings = settings
        self.port = port
        self._app_client: spotipy.Spotify | None = None
        self._oauth: SpotifyOAuth | None = None

    # -- authorization code (user) --------------------------------------
    @property
    def oauth(self) -> SpotifyOAuth:
        if not self.settings.has_credentials:
            raise SpotifyError("Spotify client ID/secret are not configured yet.")
        if self._oauth is None:
            TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
            self._oauth = SpotifyOAuth(
                client_id=self.settings.client_id,
                client_secret=self.settings.client_secret,
                redirect_uri=redirect_uri(self.port),
                scope=SCOPES,
                cache_handler=CacheFileHandler(cache_path=str(TOKEN_CACHE)),
                open_browser=False,
                show_dialog=False,
            )
        return self._oauth

    def authorize_url(self) -> str:
        return self.oauth.get_authorize_url()

    def complete_login(self, code: str) -> None:
        self.oauth.get_access_token(code, as_dict=False, check_cache=False)

    def logout(self) -> None:
        TOKEN_CACHE.unlink(missing_ok=True)
        self._oauth = None

    @property
    def logged_in(self) -> bool:
        if not self.settings.has_credentials or not TOKEN_CACHE.exists():
            return False
        try:
            token = self.oauth.cache_handler.get_cached_token()
        except Exception:
            return False
        return bool(token)

    def user_client(self) -> spotipy.Spotify:
        if not self.logged_in:
            raise SpotifyError("LOGIN_REQUIRED")
        return spotipy.Spotify(auth_manager=self.oauth, requests_timeout=20)

    # -- client credentials (app) ---------------------------------------
    def app_client(self) -> spotipy.Spotify:
        if not self.settings.has_credentials:
            raise SpotifyError("Spotify client ID/secret are not configured yet.")
        if self._app_client is None:
            APP_TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
            self._app_client = spotipy.Spotify(
                auth_manager=SpotifyClientCredentials(
                    client_id=self.settings.client_id,
                    client_secret=self.settings.client_secret,
                    cache_handler=CacheFileHandler(cache_path=str(APP_TOKEN_CACHE)),
                ),
                requests_timeout=20,
            )
        return self._app_client

    def client_for(self, kind: str) -> spotipy.Spotify:
        """Pick the weakest credential that can actually serve this request.

        Spotify requires user authentication on GET /playlists/{id}/items --
        even for a fully public playlist. (The playlist *metadata* endpoint
        still answers an app token, which makes the failure look stranger than
        it is: the name loads, the tracks 401.) Albums, single tracks and
        search remain app-token territory.
        """
        if kind in ("playlist", "liked"):
            return self.user_client()
        if self.logged_in:
            return self.user_client()
        return self.app_client()

    def whoami(self) -> dict[str, Any] | None:
        if not self.logged_in:
            return None
        try:
            me = self.user_client().current_user()
        except Exception:
            return None
        return {
            "id": me.get("id"),
            "name": me.get("display_name") or me.get("id"),
            "image": (me.get("images") or [{}])[0].get("url", ""),
        }


def _pick_cover(images: list[dict[str, Any]]) -> tuple[str, tuple[int, int]]:
    """Largest available artwork; Spotify sorts biggest-first but not always."""
    if not images:
        return "", (0, 0)
    best = max(images, key=lambda i: (i.get("width") or 0) * (i.get("height") or 0))
    return best.get("url", ""), (best.get("width") or 0, best.get("height") or 0)


def _build_track(item: dict[str, Any], album_fallback: dict[str, Any] | None = None) -> Track | None:
    if not item or item.get("type") == "episode":
        return None
    if not item.get("id"):
        return None  # local file in the playlist, nothing to match against

    album = item.get("album") or album_fallback or {}
    cover_url, cover_size = _pick_cover(album.get("images") or [])
    album_artists = [a["name"] for a in (album.get("artists") or []) if a.get("name")]

    return Track(
        id=item["id"],
        title=item.get("name", ""),
        artists=[a["name"] for a in (item.get("artists") or []) if a.get("name")],
        album=album.get("name", ""),
        album_artist=album_artists[0] if album_artists else "",
        duration_ms=item.get("duration_ms") or 0,
        track_number=item.get("track_number") or 0,
        disc_number=item.get("disc_number") or 1,
        release_date=(album.get("release_date") or "")[:10],
        isrc=((item.get("external_ids") or {}).get("isrc") or ""),
        cover_url=cover_url,
        cover_size=cover_size,
        url=(item.get("external_urls") or {}).get("spotify", ""),
    )


def _entry_payload(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    """Pull the track object out of a playlist/saved-tracks entry.

    Spotify renamed the playlist-item payload from "track" to "item"; saved
    tracks (Liked Songs) still use "track". Accept either. The type check
    matters: the payload itself also carries a boolean-ish "track" key, so a
    bare .get("track") on the wrong object returns something unusable rather
    than nothing.
    """
    if not entry:
        return None
    for key in ("item", "track"):
        payload = entry.get(key)
        if isinstance(payload, dict):
            return payload
    return None


def _paged(sp: spotipy.Spotify, first: dict[str, Any]) -> Iterator[dict[str, Any]]:
    page = first
    while page:
        yield from page.get("items") or []
        page = sp.next(page) if page.get("next") else None


def fetch(auth: SpotifyAuth, raw: str) -> Playlist:
    kind, ident = parse_source(raw)
    sp = auth.client_for(kind)

    try:
        if kind == "playlist":
            return _fetch_playlist(sp, ident)
        if kind == "album":
            return _fetch_album(sp, ident)
        if kind == "liked":
            return _fetch_liked(sp)
        return _fetch_single(sp, ident)
    except spotipy.SpotifyException as exc:
        if exc.http_status == 401:
            raise SpotifyError("LOGIN_REQUIRED") from exc
        if exc.http_status == 403:
            # Confirmed against the live API: reading playlist contents is
            # allowed only for playlists the signed-in user owns. Someone
            # else's playlist gives 403 even when it is fully public.
            raise SpotifyError(
                "Spotify only lets you read playlists you own, and this one "
                "belongs to someone else — public or not. Workaround: open it "
                "in Spotify, select all the tracks, add them to a playlist of "
                "your own, then load that one here."
            ) from exc
        if exc.http_status == 404:
            raise SpotifyError(
                "Spotify returned 404. Editorial and algorithmic playlists "
                "(RapCaviar, Discover Weekly, Daily Mix, Release Radar) are no "
                "longer readable through the API at all. Copy the tracks into "
                "a playlist of your own and load that instead."
            ) from exc
        raise SpotifyError(f"Spotify API error: {exc.msg or exc}") from exc


def _collect(items, builder) -> tuple[list[Track], list[str]]:
    tracks, skipped = [], []
    for raw_item in items:
        track = builder(raw_item)
        if track:
            tracks.append(track)
        else:
            name = (raw_item or {}).get("name") or "unknown item"
            skipped.append(name)
    return tracks, skipped


def _fetch_playlist(sp: spotipy.Spotify, pid: str) -> Playlist:
    meta = sp.playlist(pid, fields="id,name,description,owner.display_name,images,snapshot_id")
    first = sp.playlist_items(pid, limit=100, additional_types=("track",))
    tracks, skipped = _collect(
        (_entry_payload(i) for i in _paged(sp, first)), lambda t: _build_track(t)
    )
    return Playlist(
        id=meta.get("id", pid),
        kind="playlist",
        name=meta.get("name", "Untitled playlist"),
        owner=(meta.get("owner") or {}).get("display_name", ""),
        description=meta.get("description", "") or "",
        image=_pick_cover(meta.get("images") or [])[0],
        snapshot_id=meta.get("snapshot_id", ""),
        tracks=tracks,
        skipped=skipped,
    )


def _fetch_album(sp: spotipy.Spotify, aid: str) -> Playlist:
    album = sp.album(aid)
    first = album.get("tracks") or {}
    tracks, skipped = _collect(
        _paged(sp, first), lambda t: _build_track(t, album_fallback=album)
    )
    return Playlist(
        id=album.get("id", aid),
        kind="album",
        name=album.get("name", "Untitled album"),
        owner=", ".join(a["name"] for a in album.get("artists") or []),
        image=_pick_cover(album.get("images") or [])[0],
        tracks=tracks,
        skipped=skipped,
    )


def _fetch_liked(sp: spotipy.Spotify) -> Playlist:
    first = sp.current_user_saved_tracks(limit=50)
    tracks, skipped = _collect(
        (_entry_payload(i) for i in _paged(sp, first)), lambda t: _build_track(t)
    )
    return Playlist(
        id="liked",
        kind="liked",
        name="Liked Songs",
        owner="you",
        image=tracks[0].cover_url if tracks else "",
        tracks=tracks,
        skipped=skipped,
    )


def _fetch_single(sp: spotipy.Spotify, tid: str) -> Playlist:
    track = _build_track(sp.track(tid))
    if not track:
        raise SpotifyError("That track can't be downloaded (local file or podcast episode).")
    return Playlist(
        id=tid, kind="track", name=track.label, image=track.cover_url, tracks=[track]
    )
