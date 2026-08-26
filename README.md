# libber

Build an offline music library from playlists you already have. Paste a Spotify
or YouTube link and get back a folder of properly tagged `.opus` files. Runs as
a small local web app.

Spotify's API gives you metadata but never audio, so the audio comes from
YouTube Music. The hard part isn't downloading, it's making sure the thing you
downloaded is actually the recording Spotify listed, which is what most of this
codebase is about.

## What you get

- **Two sources.** *From Spotify* — playlists, albums, single tracks and Liked
  Songs, matched onto YouTube Music. *From YouTube* — paste a YouTube or
  YouTube Music playlist or video link and it downloads that exact recording,
  no matching, no Spotify account, no sign-in of any kind.
- **Real audio, no re-encode.** YouTube already serves Opus, so the extract step
  is a stream copy — the bytes you get are the bytes Google served, at whichever
  bitrate you pick.
- **Proper tags.** Title, artist, album, album artist, date, track/disc number,
  ISRC, and the full-size Spotify cover art embedded in every file.
- **Match verification.** Every candidate is scored on title, artist, and
  duration, then penalised for words that imply a *different recording* — live,
  remix, cover, karaoke, sped-up, extended. Anything suspicious is held for
  review instead of silently downloading the wrong take.
- **Fix-match picker.** Anything held for review shows the ranked alternatives
  with a preview link; pick one and it re-downloads. When none of them is the
  recording — or search found nothing at all — paste the YouTube link yourself
  and it downloads that instead.
- **The review queue waits for you.** Held tracks are written to disk with the
  candidates already found, so they're still listed after a restart and can be
  dealt with days later without re-searching the playlist.
- **Cheap re-syncs.** A `library.json` tracks what's on disk, so running the
  same playlist again only fetches what's genuinely new. Delete a file and it
  comes back next run; rename or move one and it is found again, because every
  file carries its Spotify id in its own tags.
- **`.m3u8` playlist file** written in playlist order with relative paths, so
  the library can be copied to a phone and still open correctly. Copy the whole
  download folder rather than one playlist — a playlist may reference tracks
  living in another one (see below).
- **Each recording downloaded once.** A track already on disk from another
  playlist isn't fetched again, and two Spotify ids that resolve to the same
  video share one file. The playlists still list it: the `.m3u8` points at
  wherever the file actually lives, `../Other Playlist/Track.opus` included, so
  every playlist plays in full.

## Requirements

- Python 3.12+
- [ffmpeg](https://ffmpeg.org/) on your `PATH`
- A JavaScript runtime on your `PATH` — **[Node](https://nodejs.org/)**, Deno or Bun

```
winget install Gyan.FFmpeg.Essentials      # Windows
brew install ffmpeg                        # macOS
sudo apt install ffmpeg                    # Debian/Ubuntu
```

The JS runtime is not optional. YouTube signs its media URLs with a challenge
that has to be executed, and without a runtime the extractor gets titles and
thumbnails but **no audio at all**. It surfaces as *"Sign in to confirm you're
not a bot"* or *"The page needs to be reloaded"*, neither of which points at the
real cause. libber detects Deno, Node or Bun automatically and tells you in
Settings which one it found.

## Install

```
uv sync
```

## Run

```
uv run libber
```

It opens <http://127.0.0.1:8765> in your browser.

```
uv run libber --port 9000        # different port
uv run libber --output D:/Music  # different download folder
uv run libber --jobs 5           # more parallel downloads
uv run libber --no-browser       # don't open a browser
```

Downloads default to `~/Music/libber`, one folder per playlist. Change it in
Settings and it sticks — the folder and your other preferences are saved to
`~/.libber/settings.json`, so a restart keeps them. `--output` overrides for a
single run without changing what's saved.

## One-time Spotify setup

Reading playlists needs a free Spotify API app — the app walks you through this
on first run, but for reference:

1. Open the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
   and click **Create app**.
2. Name it anything. For **Redirect URI** paste exactly
   `http://127.0.0.1:8765/callback` (the app shows the exact string to use, and
   it changes if you pass `--port`).
3. Tick **Web API**, save, then open **Settings** to reveal the client ID and
   secret. Paste both into libber.

Credentials are stored in `~/.libber/credentials.env` and never leave your
machine except to talk to Spotify.

### What signing in actually gives libber

Being asked to sign in to Spotify by a program deserves
suspicion, so here is exactly what it does and what it cannot do.

**You are not giving libber your password.** The sign-in happens on Spotify's
own page at `accounts.spotify.com`. libber opens it, Spotify hands back a token
for the redirect URI on `127.0.0.1`, and that token is cached in
`~/.libber/spotify-token.json`. Your password is never typed into libber and
never seen by it.

**There is no third party.** You create your own Spotify app, so the client ID
and secret are yours and the token is issued to *you*. Nothing is sent to the
author of this project or to any server other than Spotify's. All of it runs on
your machine, on loopback.

**Three scopes are requested, all read-only:**

| Scope | Lets libber |
| --- | --- |
| `playlist-read-private` | read your private playlists |
| `playlist-read-collaborative` | read collaborative playlists you're part of |
| `user-library-read` | read your Liked Songs |

That is the whole list — check `SCOPES` in `config.py`, and the calls in
`spotify.py`, which are all reads.

**What it therefore cannot do**, because the scopes don't exist in the grant:

- create, rename, reorder, or delete any playlist
- add or remove tracks, including from Liked Songs
- start, stop, or control playback on any device
- see your email address or your subscription tier — `user-read-email` and
  `user-read-private` aren't requested, which is why libber only ever shows
  your display name
- follow or unfollow anything, or read your listening history

**Revoke it whenever you like** at
[spotify.com/account/apps](https://www.spotify.com/account/apps/) — the app is
yours, so you can also delete it outright in the developer dashboard. Deleting
`~/.libber/` removes the cached token locally.

### You also need to sign in

Client ID and secret alone are not enough for playlists. Spotify requires user
authentication on `GET /playlists/{id}/items` — **including public playlists**,
not just your own. So click **Sign in with Spotify** after saving credentials.

The split, confirmed against the live API:

| Endpoint | App token alone |
| --- | --- |
| Playlist metadata (`/playlists/{id}`) | works |
| **Playlist contents** (`/playlists/{id}/items`) | **401, sign-in required** |
| Albums, album tracks, single tracks, search | works |

Because the metadata endpoint still answers an app token, a signed-out failure
looks odd: the playlist *name* resolves and the *tracks* 401. libber asks for
the sign-in up front instead.

### You can only read your own playlists

Signing in is necessary but not sufficient. Also confirmed against the live API:

| Playlist | Contents readable |
| --- | --- |
| One you own | yes |
| Liked Songs | yes |
| **Someone else's — even fully public** | **no (403)** |
| **Spotify editorial / algorithmic** (RapCaviar, Discover Weekly, Daily Mix, Release Radar) | **no (404)** |

This is a Spotify API restriction, not something libber can route around. To
grab a playlist you don't own: open it in Spotify, select all the tracks, add
them to a playlist of your own, then load that one here. Albums and single
tracks are unaffected — those load regardless of who made them.

### Reusing a Spotify app from another project

Add `http://127.0.0.1:8765/callback` to the app's **Redirect URIs** (Dashboard →
your app → Settings → Edit). An app can hold several, so adding this one does
not disturb whatever your other project already uses. Without it, Spotify
refuses the login with *INVALID_CLIENT: Invalid redirect URI*.

> `127.0.0.1` rather than `localhost` is deliberate: Spotify rejects
> `http://localhost` redirect URIs on apps created after April 2025.

## How matching works

A search alone is not enough — YouTube Music will happily return a live cut, a
sped-up edit, or a karaoke backing track as the top hit for an ordinary query.
So each candidate gets a weighted score:

| Signal | Weight | Notes |
| --- | ---: | --- |
| Title similarity | 42% | Fuzzy, after stripping remaster/deluxe/feat. noise |
| Artist similarity | 33% | Best match across all credited artists |
| Duration | 25% | Full marks within 2s, nothing past 15s |

Then the penalties, which are what actually catch bad matches:

- **Variant words** the source track doesn't have (live, remix, cover, karaoke,
  acoustic, sped up, demo, mashup, extended) — checked against both the title
  *and* the album, since karaoke and tribute pressings often have a clean track
  title and give themselves away only in the album name.
- **Different artist** — how covers sneak in. Without this, Pentatonix's
  "Bohemian Rhapsody" outranks Queen's.
- **Wildly wrong length** — under 85% or over 125% of the expected runtime is an
  excerpt, a full-album upload, or an extended mix, not the track.
- **A different title** — the title is the only signal that says *which* song
  this is. Artist and duration agree for every other track on the same album,
  so a same-script title that doesn't match is held no matter how well
  everything else scores.

Artists written in **different scripts are not compared at all**. Spotify
romanises names that YouTube Music leaves in their own script, so `Yousei
Teikoku` and `妖精帝國` are one act with no characters in common. Scoring that as
a mismatch buried correct official releases below fan uploads, so when the two
names share no writing system the artist is neither credited nor penalised and
title and duration decide. Covers by a genuinely different artist in the same
script are still caught.

Anything tripping those is marked *risky* and parked for review no matter how
well it scored otherwise. Tracks scoring below the confidence threshold (70 by
default, adjustable in Settings) are parked too. Everything else downloads,
walking down the ranked list if the top pick turns out to be unavailable.

## Where things live

| Path | What |
| --- | --- |
| `~/Music/libber/` | Downloads, one folder per playlist |
| `~/Music/libber/.libber/library.json` | What's downloaded, for cheap re-syncs |
| `~/.libber/settings.json` | Download folder and preferences, kept across restarts |
| `~/.libber/credentials.env` | Spotify client ID + secret |
| `~/.libber/spotify-token.json` | OAuth token cache (your sign-in) |
| `~/.libber/spotify-app-token.json` | App token cache |

Credentials live outside the music folder on purpose, so wiping a download
folder never logs you out.

## Layout

```
src/libber/
  __main__.py   CLI entry point, ffmpeg check, launches uvicorn
  config.py     settings, paths, redirect URI
  models.py     Track / Playlist, shared by both sources
  spotify.py    Spotify auth + playlist/album/liked-songs reading
  youtube.py    YouTube + YouTube Music playlists and videos, title cleanup
  matcher.py    YouTube Music search, scoring, variant detection
  enrich.py     album/date/ISRC/artwork lookup for YouTube-sourced tracks
  download.py   yt-dlp fetch, filename sanitising, Vorbis tags + cover art
  jobs.py       thread-pool orchestration, live progress events
  library.py    on-disk state, sync reports, .m3u8 writing
  server.py     FastAPI routes + SSE progress stream
  static/       the UI
```

Both sources produce the same `Playlist` of `Track`s, so everything downstream —
downloading, tagging, dedup, `.m3u8`, re-sync — is shared. A track carrying a
`video_id` came from YouTube and skips the matcher entirely.

## The YouTube card

No credentials, no sign-in, no matching — you linked the recording, so that is
what gets downloaded. Accepts `youtube.com`, `music.youtube.com`, `youtu.be`,
and `/shorts/` links, a playlist or a single video, or a bare 11-character
video id.

- A link carrying both `v=` and `list=` loads the **playlist**, except when the
  list is an auto-generated `RD…` radio mix — those are effectively endless, so
  the single video wins.
- Titles get cleaned for tagging: `(Official Video)`, `[Official Audio]`,
  `(Lyrics)`, `(HD)` and friends are stripped, and `Artist - Title` is split
  apart when the uploader used that form. Otherwise the channel name becomes
  the artist, minus YouTube's ` - Topic` suffix.
- Private, deleted and live entries are skipped and reported rather than
  failing the whole playlist.

### Filling in what YouTube doesn't give you

A YouTube track arrives with a title, a channel and a 16:9 thumbnail — no album,
no release date, no ISRC. Spotify's *search* endpoint has all of it and answers
an app token, so libber looks the recording up there and copies the tags across,
including square 640×640 cover art in place of the video thumbnail. It falls
back to YouTube Music's own fields when Spotify has no credentials or no
confident match, and to the playlist title for `OLAK5uy_…` album playlists.

The bar is deliberately higher than for downloading. A mediocre download still
gets you the song; a mediocre metadata match silently mislabels your library
forever. So candidates are scored the same way, then held to a stricter
threshold — wrong durations, different artists and live-versus-studio
mismatches are refused outright. **No album beats the wrong album.**

Turn it off in Settings if you'd rather keep YouTube's own metadata.

## Audio quality and size

YouTube offers the same recording as two Opus streams, and libber stream-copies
whichever you choose — neither is re-encoded, so this is purely which bytes
Google hands over.

| Setting | Stream | Bitrate | 1000 tracks |
| --- | --- | --- | --- |
| **Standard** (default) | itag 251 | ~130 kbps | ~4 GB |
| High | itag 774 | ~260 kbps | ~8 GB |

Standard is the default because Opus is near-transparent by roughly 128 kbps in
stereo — it is a far more efficient codec than MP3, and 130 kbps Opus is in the
region of 192–256 kbps MP3. Doubling the bitrate doubles the disk and the
download time for a difference most listeners cannot pick out in a blind test.

High is worth it if you listen on good headphones to material that stresses
codecs — applause, harpsichord, castanets, dense electronic music are the
classic cases. It needs cookies, since itag 774 is only offered to a signed-in
session.

Switching to High doesn't re-download a library you already have — a track
already on disk stays as it is, but it is now labelled with its bitrate and
counted in the summary, and a **Below quality** selector picks exactly those.
Selecting an already-downloaded track is taken as a request to fetch it again,
so an upgrade is something you choose rather than something a setting triggers.
The better stream replaces the file in place — including when the file lives
under a different playlist because the recording is shared, in which case every
playlist using it gets the better audio and their `.m3u8` references still
resolve. It does stop a *shared* recording being reused
at the lower bitrate: when one is fetched again at High, the better stream
replaces the old file in place and every track pointing at it follows, rather
than a second copy appearing beside the first.

## When YouTube blocks you

Past a few hundred downloads YouTube starts answering with *"Sign in to confirm
you're not a bot"*. It applies to the whole connection, not one video, so
everything fails at once. Sometimes it goes further and refuses to serve any
audio format even after the check passes — that shows up as *"The page needs to
be reloaded"*.

Two things help, both in Settings:

- **Pause between downloads**, and fewer in parallel. Downloading a long
  playlist flat out is what triggers it. A second or two between tracks and two
  at a time is far less likely to get flagged than five back-to-back.
- **Cookies from a browser**, which get past the check. Installed browsers are
  detected for you. Firefox forks (Zen, LibreWolf, Waterfox) work too — yt-dlp
  only knows the name `firefox`, so libber supplies the profile path alongside
  it.

If you're already blocked, neither fixes it retroactively. Wait a few hours.

A run stops itself rather than working through the rest of the playlist: three
failures in a row that look like a refused connection, or five of any kind, and
it gives up and says why. Leaving a long run unattended shouldn't mean coming
back to hundreds of identical failures against a service that started saying no
early on. Nothing is lost — the library resumes where it stopped, and the
review queue is on disk.

> ### Use a spare Google account for cookies
>
> Cookies from a signed-in browser attach every download to that Google
> account, and Google restricts accounts it judges to be automating downloads.
> A blocked IP clears by itself; a suspended Google account takes your mail,
> drive and everything else attached to it.
>
> **At Standard quality you do not need to be signed in.** Anonymous cookies
> from a browser that has merely visited youtube.com get past the check and
> reach the ~130 kbps stream — measured by stripping every authentication
> cookie from a working profile and retrying. Only High needs a signed-in
> session, because itag 774 is offered to nothing else.
>
> So the safest setup is a second browser profile, logged into nothing, used
> only by libber; every Firefox-family profile on the machine is listed
> separately in Settings so you can point at it. Choose that unless you
> specifically want High, and the worst case becomes a temporary IP block.
>
> libber reads cookies locally through yt-dlp and sends them only to YouTube.
> It never stores or logs them: `settings.json` holds the browser name and
> profile path, nothing more. The feature is off unless you turn it on.

## Tests

```
uv run pytest              # offline only -- fast, no network, no credentials
uv run pytest --network    # also hits YouTube and Spotify for real
```

The default run is pure logic: match scoring, URL parsing, filename sanitising,
library state and enrichment decisions. It needs nothing configured and takes
under a second, so there's no excuse not to run it.

`--network` adds tests that call the live services. They're opt-in because they
are slow, they go red whenever a provider changes a response shape, and one of
them downloads audio (a Creative Commons track, deliberately). Anything needing
Spotify credentials is additionally marked `spotify` and skips itself when none
are configured:

```
uv run pytest --network -m "network and not spotify"
```

Most of the matcher tests exist because the matcher got it wrong first — a cover
outranking the original, a 64-second excerpt beating the full song, a karaoke
pressing hiding behind a clean track title. The comments name the failure, so
the tests read as a record of what actually goes wrong when matching music.

`tests/test_regressions.py` is the same idea made explicit: every test there is
a bug that actually shipped, including the one that mattered most — Spotify
renaming the playlist-entry payload from `track` to `item`, which silently
skipped every track in a 655-track playlist while looking like it worked.

| file | what it covers |
| --- | --- |
| `test_matcher.py` | scoring, variant detection, tie-breaking |
| `test_regressions.py` | bugs that shipped, pinned so they can't return |
| `test_jobs.py` | skip / review / reuse / retry / rename decisions |
| `test_server.py` | routes, validation, error messages |
| `test_library.py` | sync reports, dedup, `.m3u8`, filenames |
| `test_config.py` | settings persistence, cookie probing, quality selection |
| `test_spotify.py`, `test_youtube.py` | link parsing, response shaping |
| `test_enrich.py` | when metadata is accepted, and when it's refused |
| `test_download.py` | tag round-trip against a real Opus file |
| `test_live.py` | opt-in, calls the real services |

CI runs the offline suite on every push (`.github/workflows/tests.yml`). The
network tests stay out of CI deliberately: they need credentials and would fail
for reasons unrelated to the commit.

## Where this stands legally

libber saves audio that YouTube serves. It's worth being plain about what that
does and doesn't mean, rather than leaving you to find out.

**It breaks YouTube's Terms of Service.** They permit downloading only through
their own offline feature. That's a contract between you and Google, and the
realistic consequence is account-level — rate limiting, or in principle a
restricted account — not a courtroom. It's also why the cookie setting carries
the warning it does: cookies tie the activity to a Google account.

**It does not circumvent DRM, deliberately.** Spotify's audio is protected, and
libber never touches it — Spotify is used for metadata, which is what its API
is for. YouTube hands the audio to any client that plays it; the bot check that
cookies get past is anti-scraping, not a protection measure. That distinction
carries real legal weight in most jurisdictions, and it is why the project is
built this way rather than the more direct way.

**Copyright depends on where you live.** Many countries allow private copying
for personal, non-commercial use. Whether that covers downloading, as opposed
to format-shifting something you already own, is contested and varies. This
project can't answer that for you, and neither can anyone who hasn't read your
jurisdiction's law.

**Personal use is the line that matters in practice.** Keeping offline copies of
music you already listen to is one thing. Redistributing them is another, and
that's where real consequences live.

**Artists are paid less.** An offline file doesn't generate the per-play royalty
a stream does. If you subscribe to the services you're pulling from, the
marginal difference is small — but it isn't nothing, and it's the honest cost of
doing this.

## Notes

- Local files and podcast episodes in a playlist are skipped — there's nothing
  to match them against.
