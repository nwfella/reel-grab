# reel-grab

A local YouTube downloader that lives in **one Python file**. Standard library only —
no pip install to run it, no cloud, no accounts, no telemetry. The UI is served on
loopback and the whole engine is provisioned into a private folder on first run.

```
python reel_grab.py
```

That opens `http://127.0.0.1:8730/?t=<one-time-token>` in your browser.

## Why this one can't be a static page like the other projects

Every other project here ships as a single HTML file on GitHub Pages. This one can't,
and it isn't a policy preference — it's three hard walls:

| Wall | Evidence |
|---|---|
| Media bytes come from `*.googlevideo.com`, which sends **no `Access-Control-Allow-Origin`** | a browser `fetch()` can never read the stream; a probe from a page origin returns 403 with no ACAO header |
| Stream URLs embed the **extracting IP** and expire | `videoplayback?...&ip=187.15.109.165&expire=...` — not shareable, not replayable elsewhere |
| YouTube **withholds every video format** unless the client presents a **PO token** | `yt-dlp -F` returns storyboards only; `--extractor-args youtube:formats=missing_pot` reveals the withheld 140/160/133/… formats |

So the engine (yt-dlp + ffmpeg + a PO-token provider) has to run on your machine. This
file is that app: one file to copy around, and it builds its own engine.

## Requirements

* Python 3.9+ (uses the stdlib only)
* [uv](https://docs.astral.sh/uv/) — installs the engine into a private env
* Node.js 22+ — runs the PO-token provider
* `git` and `npm` — used once, to build the provider
* ffmpeg on `PATH` (or `~/bin`) — needed to merge video+audio and to make mp3s

Everything except Python is checked at startup and reported in the UI's Engine panel.

## Engine layout

Nothing lands in your project folder or system Python. Default `~/.reel-grab`:

```
~/.reel-grab/
  venv/        yt-dlp + bgutil-ytdlp-pot-provider
  pot/         PO-token provider source + build (node, serves 127.0.0.1:4416)
```

Override with `--engine-home D:/reel-grab` or `REEL_GRAB_ENGINE`.

## Usage

```
python reel_grab.py                       # default port 8730, opens the browser
python reel_grab.py --port 9000
python reel_grab.py --no-browser
python reel_grab.py --no-provision        # UI only, don't touch the engine
python reel_grab.py --engine-home D:/rg
```

Paste a URL → **Fetch** → pick mode/quality/save-folder → **Download**. Playlists are
detected and listed; downloads queue and run one at a time, with live percent, speed,
ETA and per-file phase (`Downloading N format(s)` is folded into a single bar, so a
video+audio mux reads 0-100% once, not twice).

Modes:

* **Video (merged mp4)** — `bv*[height<=N]+ba/b`, muxed to mp4 by ffmpeg
* **Audio only** — `-x` to mp3 / m4a / opus

Filenames are `Title [videoId].ext`, so re-runs dedupe instead of colliding.

## Security

The server binds `127.0.0.1` only, and every request must satisfy three checks:

1. **Host** must be a loopback name (blocks DNS-rebinding attempts).
2. **Origin**, when present, must be this exact origin (blocks a random web page from
   driving the API — any site can `POST` to `127.0.0.1`, so this matters).
3. A **per-run random token** (`X-ReelGrab-Token` header, or `?t=` on first load).

The token is regenerated each start and printed in the console. Nothing is sent anywhere
except to YouTube on your behalf.

## GitHub Pages build (`docs/`)

Live at **https://nwfella.github.io/reel-grab/**. `docs/index.html` is the same UI as the
local one — generated from the same `PAGE` string by `scripts/build_pages.py`, so the two
can never drift. `python scripts/build_pages.py --check` fails when the committed file is
stale, and the Python gate runs that check.

It still cannot download on its own; nothing served over HTTP can (see the table above). It
drives the engine on your machine instead: start `python reel_grab.py`, open the live page,
and paste the engine URL and the token the app printed — or just open the URL the app prints,
which already carries the token in its fragment. Cross-origin calls are accepted only from
origins you list with `--allow-origin` (default `https://nwfella.github.io`) **and** only
with that run's token, and the server still binds `127.0.0.1`: the request is made by *your*
browser to *your* loopback, never by GitHub.

```
node scripts/pages_check.js                                        # gate the artifact locally
node scripts/pages_check.js --pages=https://nwfella.github.io/reel-grab/   # gate the live one
```

That gate is the only one that exercises the cross-origin path — preflight, allowlist and
Chrome's private-network opt-in — by pasting the token into the published page and running a
real Fetch and Download through it.

## Verification

```
python scripts/verify_app.py
```

Boots the real server, provisions the real engine, then: checks the loopback auth and
cross-origin blocks, probes a Creative-Commons video (the step a static app fails),
downloads it at 360p, reads the result back with `ffprobe` (video **and** audio streams,
duration), downloads an mp3, and exercises the error paths. Exit code 0 only if all of
it passes. `--keep-going` reports every failure instead of stopping at the first.

```
node scripts/browser_check.js
```

Tier-2 gate: drives the real UI in headless Chrome over the DevTools Protocol (throwaway
profile, no dependencies). Waits on the engine-ready *state* before asserting, checks all
eight steps, proves no horizontal overflow at 390/1024/1500px, clicks **Fetch** and waits
for the probe card to render real formats, then clicks **Download** and waits for the job
row to reach `done` — then reads the path out of the row's own Reveal button and confirms
that file exists on disk with a plausible size. Run it in the foreground on Windows: a
backgrounded `node … > log` on this host dies with `stdin is not a tty` before printing a
single assertion.

The test video is *Big Buck Bunny* (Blender Foundation, CC-BY) — a licence that permits
downloading. Use this on content you own, CC-licensed material, or public domain.

## Policy notes

* This is a legitimate tool for **your own uploads, CC-licensed and public-domain
  content**. Downloading third-party copyrighted video violates YouTube's Terms of
  Service; that's your call to make, not something the tool pretends away.
* This machine is IT-constrained. A downloader like this is an ordinary open-source
  tool, but running it on a monitored corporate machine may breach policy regardless of
  what you download — check before you do, or keep it on a personal box.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "NO VIDEO FORMATS" after Fetch | PO token provider down. Open **Engine setup**, re-check; confirm `curl 127.0.0.1:4416/ping` |
| Every client says "Requested format is not available" | yt-dlp needs `youtube:fetch_pot=always` (already passed by this app) *and* a live token provider |
| `Failed to decrypt with DPAPI` | yt-dlp can't read Edge/Chrome cookies on Windows (yt-dlp issue 10927). Not used by this app — that's why it ships a token provider |
| Slow first run | One-time: `npm install` + `tsc` for the provider, ~2-4 min |
| Videos download without sound | ffmpeg missing, so `+ba` couldn't be merged. Install ffmpeg and re-run a download |

## Licence

MIT — see [LICENSE](LICENSE).
