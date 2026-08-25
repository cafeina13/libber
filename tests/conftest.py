"""Shared fixtures, plus the opt-in switch for tests that hit live services."""

from __future__ import annotations

import pytest

from spt2yt.config import load_settings
from spt2yt.models import Playlist, Track


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--network",
        action="store_true",
        default=False,
        help="also run tests that call YouTube/Spotify for real",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--network"):
        # -m 'not network' lives in addopts; --network lifts it.
        config.option.markexpr = ""
        return
    skip = pytest.mark.skip(reason="needs --network")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def spotify_client():
    """A Spotify app-token client, or a skip when no credentials are set up."""
    settings = load_settings()
    if not settings.has_credentials:
        pytest.skip("no Spotify credentials configured")
    from spt2yt.spotify import SpotifyAuth

    return SpotifyAuth(settings, 8765).app_client()


@pytest.fixture
def track():
    """A fully-populated track, the shape the Spotify source produces."""

    def build(**overrides):
        base = dict(
            id="0" * 22,
            title="Test Song",
            artists=["Test Artist"],
            album="Test Album",
            album_artist="Test Artist",
            duration_ms=200_000,
            track_number=1,
        )
        base.update(overrides)
        return Track(**base)

    return build


@pytest.fixture
def playlist(track):
    def build(n=3, **overrides):
        tracks = [
            track(id=f"{i:022d}", title=f"Song {i}", track_number=i)
            for i in range(1, n + 1)
        ]
        base = dict(id="pl", kind="playlist", name="Test Playlist", tracks=tracks)
        base.update(overrides)
        return Playlist(**base)

    return build
