"""Settings persistence.

The download folder is the setting that matters: if a restart forgets it, the
app points at an empty default library and re-downloads everything into the
wrong place. That happened, hence these.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from libber import config
from libber.config import (
    Settings,
    cookie_option,
    load_settings,
    probe_cookies,
    save_settings,
)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Redirect the app home and clear the env so nothing leaks in."""
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "credentials.env")
    monkeypatch.setattr(config, "DEFAULT_OUTPUT", tmp_path / "default-music")
    for var in ("LIBBER_OUTPUT", "LIBBER_CONCURRENCY",
                "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


class TestDefaults:
    def test_without_a_saved_file(self, isolated):
        s = load_settings()
        assert s.output_dir == isolated / "default-music"
        assert s.concurrency == 3
        assert s.match_threshold == 70.0
        assert s.skip_low_matches is True
        assert s.enrich_youtube is True


class TestRoundTrip:
    def test_settings_survive_a_reload(self, isolated):
        """The regression: settings only lived in memory, so restarting
        silently reverted the download folder."""
        save_settings(Settings(
            output_dir=Path("D:/Music/The Archive/libber"),
            concurrency=5,
            match_threshold=90.0,
            skip_low_matches=False,
            enrich_youtube=False,
        ))
        s = load_settings()
        assert s.output_dir == Path("D:/Music/The Archive/libber")
        assert s.concurrency == 5
        assert s.match_threshold == 90.0
        assert s.skip_low_matches is False
        assert s.enrich_youtube is False

    def test_written_atomically(self, isolated):
        save_settings(Settings(output_dir=isolated / "music"))
        assert (isolated / "settings.json").exists()
        assert not list(isolated.glob("*.tmp"))

    def test_paths_with_spaces_survive(self, isolated):
        """Windows library paths routinely contain spaces."""
        target = Path("C:/Users/Someone/Music/The Archive/libber")
        save_settings(Settings(output_dir=target))
        assert load_settings().output_dir == target

    def test_types_are_preserved(self, isolated):
        save_settings(Settings(concurrency=7, match_threshold=85.5))
        s = load_settings()
        assert isinstance(s.concurrency, int)
        assert isinstance(s.match_threshold, float)


class TestCookieOption:
    """YouTube blocks anonymous requests, so cookies are effectively required.
    Firefox forks need an explicit profile path -- yt-dlp only knows the name
    "firefox" and cannot find Zen, LibreWolf or Waterfox by itself."""

    def test_none_when_unset(self):
        assert cookie_option(Settings()) is None

    def test_browser_only(self):
        assert cookie_option(Settings(cookies_browser="edge")) == ("edge",)

    def test_browser_with_profile(self):
        zen = r"C:\Users\x\AppData\Roaming\zen\Profiles\abc.Default (alpha)"
        assert cookie_option(
            Settings(cookies_browser="firefox", cookies_profile=zen)
        ) == ("firefox", zen)

    def test_profile_without_browser_is_ignored(self):
        assert cookie_option(Settings(cookies_profile="/some/path")) is None

    def test_survives_a_reload(self, isolated):
        zen = r"C:\Users\x\AppData\Roaming\zen\Profiles\abc.Default (alpha)"
        save_settings(Settings(cookies_browser="firefox", cookies_profile=zen,
                               sleep_between=2.5))
        s = load_settings()
        assert cookie_option(s) == ("firefox", zen)
        assert s.sleep_between == 2.5


class TestProbeCookies:
    """Which browsers work is not guessable -- Chromium on Windows encrypts its
    cookie store in a way yt-dlp can't read, Firefox forks are fine, and a fork
    needs an explicit profile. So the browser is read and the result reported,
    rather than a platform/browser matrix being maintained by hand.
    """

    def _fake_jar(self, monkeypatch, cookies=None, error=None):
        def fake(*spec):
            if error:
                raise RuntimeError(error)
            return cookies or []

        monkeypatch.setattr("yt_dlp.cookies.extract_cookies_from_browser", fake)

    def _cookie(self, name, domain=".youtube.com"):
        return SimpleNamespace(name=name, domain=domain)

    def test_unconfigured_is_reported_not_probed(self):
        result = probe_cookies(Settings())
        assert result["configured"] is False
        assert result["ok"] is False

    def test_success_counts_youtube_cookies(self, monkeypatch):
        self._fake_jar(monkeypatch, [self._cookie("SID"), self._cookie("PREF"),
                                     self._cookie("other", ".example.com")])
        result = probe_cookies(Settings(cookies_browser="firefox"))
        assert result["ok"] is True
        assert "2 YouTube cookies" in result["message"]

    def test_signed_in_session_is_called_out(self, monkeypatch):
        self._fake_jar(monkeypatch, [self._cookie("LOGIN_INFO"), self._cookie("SID")])
        result = probe_cookies(Settings(cookies_browser="firefox"))
        assert result["signed_in"] is True
        assert "signed-in" in result["message"]

    def test_signed_out_is_noted_as_safer(self, monkeypatch):
        self._fake_jar(monkeypatch, [self._cookie("PREF")])
        result = probe_cookies(Settings(cookies_browser="firefox"))
        assert result["ok"] is True
        assert result["signed_in"] is False
        assert "safer" in result["message"]

    def test_no_youtube_cookies_is_not_success(self, monkeypatch):
        self._fake_jar(monkeypatch, [self._cookie("x", ".example.com")])
        result = probe_cookies(Settings(cookies_browser="firefox"))
        assert result["ok"] is False
        assert "visit youtube.com" in result["message"]

    def test_chromium_encryption_gets_a_plain_explanation(self, monkeypatch):
        self._fake_jar(monkeypatch, error="Failed to decrypt with DPAPI. See issue 10927")
        result = probe_cookies(Settings(cookies_browser="edge"))
        assert result["ok"] is False
        assert "App-Bound Encryption" in result["message"]
        assert "Firefox" in result["message"]        # says what does work

    def test_missing_profile_is_explained(self, monkeypatch):
        self._fake_jar(monkeypatch, error="could not find firefox cookies database")
        result = probe_cookies(Settings(cookies_browser="firefox",
                                        cookies_profile="/nope"))
        assert result["ok"] is False
        assert "profile" in result["message"]

    def test_unknown_failure_is_passed_through(self, monkeypatch):
        self._fake_jar(monkeypatch, error="something else entirely went wrong")
        result = probe_cookies(Settings(cookies_browser="firefox"))
        assert result["ok"] is False
        assert "something else entirely" in result["message"]


class TestResilience:
    def test_corrupt_file_falls_back_to_defaults(self, isolated):
        (isolated / "settings.json").write_text("{ not json", encoding="utf-8")
        assert load_settings().concurrency == 3      # no crash

    def test_non_object_json_is_ignored(self, isolated):
        (isolated / "settings.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert load_settings().concurrency == 3

    def test_partial_file_fills_the_rest_from_defaults(self, isolated):
        (isolated / "settings.json").write_text(
            json.dumps({"concurrency": 6}), encoding="utf-8"
        )
        s = load_settings()
        assert s.concurrency == 6
        assert s.output_dir == isolated / "default-music"

    def test_empty_output_dir_does_not_blank_the_path(self, isolated):
        (isolated / "settings.json").write_text(
            json.dumps({"output_dir": ""}), encoding="utf-8"
        )
        assert load_settings().output_dir == isolated / "default-music"


class TestEnvironmentPrecedence:
    def test_env_beats_the_saved_file(self, isolated, monkeypatch):
        """A one-off `--output` sets the env var; it must not be overridden by
        the saved value, nor overwrite it."""
        save_settings(Settings(output_dir=Path("D:/saved")))
        monkeypatch.setenv("LIBBER_OUTPUT", "E:/from-cli")
        assert load_settings().output_dir == Path("E:/from-cli")

    def test_env_concurrency_wins(self, isolated, monkeypatch):
        save_settings(Settings(concurrency=5))
        monkeypatch.setenv("LIBBER_CONCURRENCY", "2")
        assert load_settings().concurrency == 2

    def test_saved_value_used_when_env_is_absent(self, isolated):
        save_settings(Settings(output_dir=Path("D:/saved")))
        assert load_settings().output_dir == Path("D:/saved")
