"""Runtime configuration and on-disk locations."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Where we keep the OAuth token cache and the credentials .env. Kept out of the
# output directory so wiping a music folder never logs you out.
APP_HOME = Path(os.environ.get("LIBBER_HOME") or (Path.home() / ".libber"))
ENV_FILE = APP_HOME / "credentials.env"
TOKEN_CACHE = APP_HOME / "spotify-token.json"
# spotipy defaults its client-credentials cache to ".cache" in the working
# directory, which drops a live access token into whatever folder the app was
# started from -- the repo root, typically. Keep it with the other secrets.
APP_TOKEN_CACHE = APP_HOME / "spotify-app-token.json"
# Settings changed in the UI live here. Without this they only ever existed in
# memory, so restarting silently reverted the download folder -- and an empty
# library at the default path means re-downloading everything.
SETTINGS_FILE = APP_HOME / "settings.json"

DEFAULT_OUTPUT = Path.home() / "Music" / "libber"

# Loopback only, and 127.0.0.1 rather than "localhost": Spotify rejects
# http://localhost redirect URIs on apps created after April 2025.
SERVER_HOST = "127.0.0.1"
SERVER_PORT = int(os.environ.get("LIBBER_PORT") or 8765)
REDIRECT_PATH = "/callback"

SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"


def redirect_uri(port: int = SERVER_PORT) -> str:
    return f"http://{SERVER_HOST}:{port}{REDIRECT_PATH}"


@dataclass
class Settings:
    client_id: str = ""
    client_secret: str = ""
    output_dir: Path = field(default_factory=lambda: DEFAULT_OUTPUT)
    concurrency: int = 3
    # Below this score a match is surfaced as "needs review" instead of being
    # downloaded silently. The risky flag now holds anything that looks like the
    # wrong *song* whatever this is set to, so what remains here is the wrong
    # *cut* -- a live take or single edit of the right track. Measured against
    # every questionable match a real library produced, 80 catches one more of
    # those than 70 while holding nothing extra; 85 starts holding correct
    # matches whose artist is written in another script.
    match_threshold: float = 80.0
    skip_low_matches: bool = True
    # YouTube gives no album, date or ISRC and only 16:9 artwork. Look the
    # recording up on Spotify (or YouTube Music) to fill those in.
    enrich_youtube: bool = True
    # YouTube now answers anonymous requests with "Sign in to confirm you're
    # not a bot", so cookies from a signed-in browser are effectively required.
    # Firefox forks (Zen, LibreWolf, Waterfox) work as "firefox" plus the path
    # to their profile, which yt-dlp cannot discover on its own.
    cookies_browser: str = ""
    cookies_profile: str = ""
    # Downloading a long playlist flat out is what triggers the block in the
    # first place. A short random gap between tracks costs little and looks far
    # less like a scraper.
    sleep_between: float = 1.0
    # "standard" takes the ~130 kbps Opus stream, "high" the ~260 kbps one that
    # YouTube Music offers a signed-in session. Opus is near-transparent by
    # about 128 kbps stereo, so "high" mostly buys file size -- it is worth it
    # only for difficult material on good headphones.
    audio_quality: str = "standard"

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)


PERSISTED = ("output_dir", "concurrency", "match_threshold", "skip_low_matches",
             "enrich_youtube", "cookies_browser", "cookies_profile", "sleep_between",
             "audio_quality")

# Opus only, and never a re-encode. "standard" caps the bitrate so the ~130 kbps
# stream wins over the ~260 kbps one; both are stream-copied, so the only
# difference is which one YouTube hands over.
FORMAT_STANDARD = ("bestaudio[acodec=opus][abr<160]/bestaudio[acodec=opus]"
                   "/bestaudio/best")
FORMAT_HIGH = "bestaudio[acodec=opus]/bestaudio/best"


def audio_format(settings: Settings) -> str:
    return FORMAT_HIGH if settings.audio_quality == "high" else FORMAT_STANDARD


def min_bitrate(settings: Settings) -> int:
    """The floor a file already on disk must clear to be reused as-is.

    Standard accepts anything: a 260 kbps file is not a reason to re-download.
    High sits above the ~130 kbps stream and below the ~260 one, so a file
    fetched before the setting changed is recognised as the lesser version.
    """
    return 190 if settings.audio_quality == "high" else 0


def js_runtime() -> dict:
    """A JavaScript runtime for yt-dlp, if one is installed.

    YouTube signs its media URLs with a challenge that has to be executed, so
    without a runtime the extractor gets metadata and storyboards but no audio
    at all -- reported as "Sign in to confirm you're not a bot" or "The page
    needs to be reloaded", neither of which points at the real cause. yt-dlp
    only looks for Deno unless told otherwise, so Node goes unused despite
    being far more commonly installed.
    """
    for name in ("deno", "node", "bun"):
        if shutil.which(name):
            return {name: {}}
    return {}


def probe_cookies(settings: Settings) -> dict:
    """Actually try reading the configured browser's cookies.

    Guessing which browsers work is a losing game: Chromium-based browsers on
    Windows encrypt their cookie store with App-Bound Encryption, which yt-dlp
    cannot decrypt, while Firefox and its forks are fine. Rather than maintain
    a matrix of platform and browser, read them and report what happened.
    """
    spec = cookie_option(settings)
    if not spec:
        return {"configured": False, "ok": False, "message": "No cookies configured."}

    from yt_dlp.cookies import extract_cookies_from_browser

    try:
        jar = extract_cookies_from_browser(*spec)
    except Exception as exc:
        detail = str(exc).splitlines()[0]
        if "DPAPI" in detail or "decrypt" in detail.lower():
            detail = (
                "This browser encrypts its cookies in a way yt-dlp can't read "
                "on Windows (Chrome 127+ App-Bound Encryption). Firefox, Zen, "
                "LibreWolf and Waterfox work."
            )
        elif "could not find" in detail.lower() or "not find" in detail.lower():
            detail = "No cookie store found — is that browser profile right?"
        return {"configured": True, "ok": False, "message": detail[:200]}

    youtube = sum(1 for c in jar if "youtube" in (c.domain or ""))
    signed_in = any(c.name == "LOGIN_INFO" and "youtube" in (c.domain or "") for c in jar)
    if not youtube:
        return {
            "configured": True, "ok": False,
            "message": "Cookies read, but none for YouTube — visit youtube.com "
                       "in that browser first.",
        }
    return {
        "configured": True, "ok": True, "signed_in": signed_in,
        "message": f"Read {youtube} YouTube cookies"
                   + (" from a signed-in session." if signed_in
                      else " (signed out — safer, and often enough)."),
    }


def cookie_option(settings: Settings) -> tuple | None:
    """yt-dlp's `cookiesfrombrowser` tuple, or None when unconfigured."""
    if not settings.cookies_browser:
        return None
    if settings.cookies_profile:
        return (settings.cookies_browser, settings.cookies_profile)
    return (settings.cookies_browser,)


def _read_saved() -> dict:
    """Whatever was last saved from the UI. Never fatal: a corrupt or missing
    file just means falling back to defaults."""
    if not SETTINGS_FILE.exists():
        return {}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(settings: Settings) -> None:
    """Write the UI-adjustable settings so a restart keeps them.

    The download folder matters most: losing it points the app at an empty
    default library, which re-downloads everything into the wrong place.
    """
    APP_HOME.mkdir(parents=True, exist_ok=True)
    payload = {
        "output_dir": str(settings.output_dir),
        "concurrency": settings.concurrency,
        "match_threshold": settings.match_threshold,
        "skip_low_matches": settings.skip_low_matches,
        "enrich_youtube": settings.enrich_youtube,
        "cookies_browser": settings.cookies_browser,
        "cookies_profile": settings.cookies_profile,
        "sleep_between": settings.sleep_between,
        "audio_quality": settings.audio_quality,
    }
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


def load_settings() -> Settings:
    """Defaults, overlaid with what was saved, overlaid with the environment.

    The environment wins so a one-off `--output` doesn't overwrite the folder
    the user configured in the UI.
    """
    APP_HOME.mkdir(parents=True, exist_ok=True)
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE, override=False)
    load_dotenv(override=False)  # a project-local .env still wins over nothing

    saved = _read_saved()
    settings = Settings(
        client_id=os.environ.get("SPOTIFY_CLIENT_ID", "").strip(),
        client_secret=os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip(),
    )

    if saved.get("output_dir"):
        settings.output_dir = Path(str(saved["output_dir"])).expanduser()
    for key in ("concurrency", "match_threshold", "skip_low_matches", "enrich_youtube",
                "cookies_browser", "cookies_profile", "sleep_between", "audio_quality"):
        if key in saved and saved[key] is not None:
            setattr(settings, key, type(getattr(settings, key))(saved[key]))

    if os.environ.get("LIBBER_OUTPUT"):
        settings.output_dir = Path(os.environ["LIBBER_OUTPUT"]).expanduser()
    if os.environ.get("LIBBER_CONCURRENCY"):
        settings.concurrency = int(os.environ["LIBBER_CONCURRENCY"])
    return settings


def save_credentials(client_id: str, client_secret: str) -> None:
    """Persist credentials so the user only pastes them once."""
    APP_HOME.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(
        f"SPOTIFY_CLIENT_ID={client_id.strip()}\n"
        f"SPOTIFY_CLIENT_SECRET={client_secret.strip()}\n",
        encoding="utf-8",
    )
    try:  # tighten perms where the platform supports it
        ENV_FILE.chmod(0o600)
    except OSError:
        pass
    os.environ["SPOTIFY_CLIENT_ID"] = client_id.strip()
    os.environ["SPOTIFY_CLIENT_SECRET"] = client_secret.strip()
