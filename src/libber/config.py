"""Runtime configuration and on-disk locations."""

from __future__ import annotations

import json
import os
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
    # downloaded silently. Tuned against live/remix/karaoke false positives.
    match_threshold: float = 70.0
    skip_low_matches: bool = True
    # YouTube gives no album, date or ISRC and only 16:9 artwork. Look the
    # recording up on Spotify (or YouTube Music) to fill those in.
    enrich_youtube: bool = True

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)


PERSISTED = ("output_dir", "concurrency", "match_threshold", "skip_low_matches",
             "enrich_youtube")


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
    for key in ("concurrency", "match_threshold", "skip_low_matches", "enrich_youtube"):
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
