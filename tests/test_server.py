"""HTTP surface: routing, validation, and the messages the UI shows.

No real Spotify or YouTube calls -- the source functions are stubbed, so these
cover what the API does with their results, including the failures. The shared
app state is reset per test so a developer's real credentials can't leak in and
change the outcome.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from spt2yt.config import Settings
from spt2yt.models import Playlist, Track
from spt2yt.server import app, state
from spt2yt.spotify import SpotifyError
from spt2yt.youtube import YouTubeError


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client on isolated state: temp output dir, no real credentials."""
    monkeypatch.setattr(state, "settings", Settings(output_dir=tmp_path))
    monkeypatch.setattr(state, "_libraries", {})
    monkeypatch.setattr(state, "playlists", {})

    class FakeAuth:
        settings = state.settings
        logged_in = False

        def whoami(self):
            return None

        def authorize_url(self):
            raise SpotifyError("Spotify client ID/secret are not configured yet.")

    monkeypatch.setattr(state, "auth", FakeAuth())
    with TestClient(app) as c:
        yield c


def sample_playlist(kind="playlist", n=2, direct=False):
    tracks = [
        Track(
            id=(f"{i}" * 22)[:22],
            title=f"Song {i}",
            artists=["Artist"],
            album="Album",
            duration_ms=200_000,
            video_id=("v" * 11) if direct else "",
        )
        for i in range(1, n + 1)
    ]
    return Playlist(id="pl123", kind=kind, name="Test Playlist", owner="me", tracks=tracks)


class TestStaticAndStatus:
    def test_serves_the_page(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "spt2yt" in r.text

    def test_serves_assets(self, client):
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/style.css").status_code == 200

    def test_status_shape(self, client):
        body = client.get("/api/status").json()
        assert body["has_credentials"] is False
        assert body["logged_in"] is False
        assert body["redirect_uri"].startswith("http://127.0.0.1:")
        assert body["redirect_uri"].endswith("/callback")
        for key in ("output_dir", "concurrency", "match_threshold",
                    "skip_low_matches", "enrich_youtube"):
            assert key in body["settings"]


class TestCredentials:
    @pytest.mark.parametrize(
        "payload",
        [{"client_id": "", "client_secret": ""},
         {"client_id": "abc", "client_secret": "  "},
         {"client_id": "   ", "client_secret": "abc"}],
    )
    def test_rejects_blank_values(self, client, payload):
        assert client.post("/api/credentials", json=payload).status_code == 400

    def test_missing_fields_are_a_validation_error(self, client):
        assert client.post("/api/credentials", json={}).status_code == 422


class TestSettings:
    def test_updates_and_echoes_back(self, client, tmp_path):
        target = tmp_path / "music"
        body = client.post("/api/settings", json={
            "output_dir": str(target), "concurrency": 5,
            "match_threshold": 85, "skip_low_matches": False, "enrich_youtube": False,
        }).json()
        assert body["settings"]["concurrency"] == 5
        assert body["settings"]["match_threshold"] == 85
        assert body["settings"]["skip_low_matches"] is False
        assert body["settings"]["enrich_youtube"] is False
        assert target.exists()      # the folder is created

    @pytest.mark.parametrize("value, expected", [(0, 1), (99, 8)])
    def test_concurrency_is_clamped(self, client, value, expected):
        body = client.post("/api/settings", json={"concurrency": value}).json()
        assert body["settings"]["concurrency"] == expected

    @pytest.mark.parametrize("value, expected", [(-10, 0), (500, 100)])
    def test_threshold_is_clamped(self, client, value, expected):
        body = client.post("/api/settings", json={"match_threshold": value}).json()
        assert body["settings"]["match_threshold"] == expected

    def test_partial_update_leaves_the_rest_alone(self, client):
        before = client.get("/api/status").json()["settings"]
        after = client.post("/api/settings", json={"concurrency": 4}).json()["settings"]
        assert after["match_threshold"] == before["match_threshold"]


class TestLoadingPlaylists:
    def test_returns_tracks_and_a_sync_report(self, client, monkeypatch):
        monkeypatch.setattr("spt2yt.server.fetch_playlist",
                            lambda auth, url: sample_playlist())
        body = client.post("/api/playlist", json={"url": "spotify:playlist:x"}).json()
        assert body["playlist"]["name"] == "Test Playlist"
        assert body["playlist"]["direct"] is False
        assert len(body["tracks"]) == 2
        assert body["sync"]["total"] == 2
        assert len(body["sync"]["new"]) == 2
        assert all("downloaded" in t for t in body["tracks"])

    def test_login_required_is_a_401_with_guidance(self, client, monkeypatch):
        def needs_login(auth, url):
            raise SpotifyError("LOGIN_REQUIRED")

        monkeypatch.setattr("spt2yt.server.fetch_playlist", needs_login)
        r = client.post("/api/playlist", json={"url": "spotify:playlist:x"})
        assert r.status_code == 401
        assert r.json()["error"] == "login_required"
        # The message must not blame privacy -- public playlists need it too.
        assert "public" in r.json()["message"].lower()

    def test_other_spotify_errors_are_400_with_the_reason(self, client, monkeypatch):
        def refuse(auth, url):
            raise SpotifyError("Spotify only lets you read playlists you own")

        monkeypatch.setattr("spt2yt.server.fetch_playlist", refuse)
        r = client.post("/api/playlist", json={"url": "spotify:playlist:x"})
        assert r.status_code == 400
        assert "you own" in r.json()["detail"]


class TestLoadingYouTube:
    def test_marks_the_playlist_as_direct(self, client, monkeypatch):
        monkeypatch.setattr("spt2yt.server.fetch_youtube",
                            lambda url: sample_playlist(kind="yt-playlist", direct=True))
        body = client.post("/api/youtube", json={"url": "https://youtu.be/x"}).json()
        assert body["playlist"]["kind"] == "yt-playlist"
        assert body["playlist"]["direct"] is True

    def test_bad_link_is_a_400(self, client, monkeypatch):
        def bad(url):
            raise YouTubeError("That doesn't look like a YouTube link.")

        monkeypatch.setattr("spt2yt.server.fetch_youtube", bad)
        r = client.post("/api/youtube", json={"url": "nope"})
        assert r.status_code == 400
        assert "YouTube" in r.json()["detail"]

    def test_needs_no_credentials(self, client, monkeypatch):
        """The YouTube card must work with no Spotify setup at all."""
        monkeypatch.setattr("spt2yt.server.fetch_youtube",
                            lambda url: sample_playlist(kind="yt-video", n=1, direct=True))
        assert client.get("/api/status").json()["has_credentials"] is False
        assert client.post("/api/youtube", json={"url": "https://youtu.be/x"}).status_code == 200


class TestJobs:
    def test_unknown_playlist_is_a_404(self, client):
        r = client.post("/api/jobs", json={"playlist_id": "nope", "track_ids": ["a"]})
        assert r.status_code == 404
        assert "Load the playlist again" in r.json()["detail"]

    def test_unknown_job_is_a_404(self, client):
        assert client.get("/api/jobs/deadbeef").status_code == 404
        assert client.post("/api/jobs/deadbeef/cancel").status_code == 404
        assert client.post(
            "/api/jobs/deadbeef/retry", json={"track_id": "a", "video_id": "b"}
        ).status_code == 404

    def test_creates_a_job_from_a_loaded_playlist(self, client, monkeypatch):
        monkeypatch.setattr("spt2yt.server.fetch_playlist",
                            lambda auth, url: sample_playlist())
        monkeypatch.setattr("spt2yt.jobs.Job.start", lambda self, pool: None)
        loaded = client.post("/api/playlist", json={"url": "spotify:playlist:x"}).json()

        r = client.post("/api/jobs", json={"playlist_id": loaded["playlist"]["id"],
                                           "track_ids": [loaded["tracks"][0]["id"]]})
        assert r.status_code == 200
        body = r.json()
        assert body["job_id"]
        assert len(body["snapshot"]["tasks"]) == 1

        assert client.get(f"/api/jobs/{body['job_id']}").status_code == 200
        assert client.post(f"/api/jobs/{body['job_id']}/cancel").json() == {"ok": True}

    def test_empty_selection_downloads_the_whole_playlist(self, client, monkeypatch):
        monkeypatch.setattr("spt2yt.server.fetch_playlist",
                            lambda auth, url: sample_playlist(n=3))
        monkeypatch.setattr("spt2yt.jobs.Job.start", lambda self, pool: None)
        loaded = client.post("/api/playlist", json={"url": "spotify:playlist:x"}).json()

        body = client.post("/api/jobs", json={"playlist_id": loaded["playlist"]["id"],
                                              "track_ids": []}).json()
        assert len(body["snapshot"]["tasks"]) == 3


class TestAuthRoutes:
    def test_login_without_credentials_explains_itself(self, client):
        r = client.get("/api/login")
        assert r.status_code == 400
        assert "client ID" in r.json()["detail"]

    def test_callback_reports_a_denial(self, client):
        r = client.get("/callback", params={"error": "access_denied"})
        assert r.status_code == 200
        assert "access_denied" in r.text

    def test_callback_without_a_code_does_not_crash(self, client):
        r = client.get("/callback")
        assert r.status_code == 200
        assert "authorization code" in r.text.lower()
