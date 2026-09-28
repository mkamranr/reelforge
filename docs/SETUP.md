# Setup

Two supported paths. **Docker** is the one to use unless you are working on the
code — it carries ffmpeg, the fonts and the exact Python version. **Local** runs
everything inline in one process, which is easier to debug.

---

## 1. Prerequisites

| | Needed for | Notes |
|---|---|---|
| **Docker + Compose** | the container path | The only hard requirement there |
| **Python 3.12** | the local path | 3.10 works, but see the note on `fromisoformat` in [ARCHITECTURE.md](ARCHITECTURE.md) |
| **ffmpeg / ffprobe** | the local path | Must be on `PATH`. `brew install ffmpeg`, or `apt install ffmpeg` |
| **An LLM** | writing the script and storyboard | Hosted, or a local Ollama / vLLM box. See [CONFIGURATION.md](CONFIGURATION.md) |
| **A voice** | narration | Or upload your own recording — no TTS provider needed |

Nothing else is required. There is no database, no build step for the UI, and no
CDN.

### About `video/bin/`

The repository's own renderer can vendor static ffmpeg builds in `video/bin/`.
Those are **macOS x86_64** binaries and are not version-controlled. Inside a
container they are absent, and `align.py`'s `tool()` falls back to `PATH`, which
is the intended behaviour. Do not copy them into an image.

---

## 2. Get the code

```bash
git clone <your-repo-url> reelforge
cd reelforge
```

---

## 3. Credentials

Keys are never stored in `config.yaml`. It names the *environment variable* for
each provider (`api_key_env`); the value is read from the environment or from the
settings store.

```bash
cp .env.example .env
```

Fill in only what you actually use. Every line is optional:

```bash
# LLM — a self-hosted server (vLLM, LM Studio, Ollama) needs no key at all
OPENAI_API_KEY=
ANTHROPIC_API_KEY=

# TTS — metered per character. Leave empty if you upload your own narration.
ELEVENLABS_API_KEY=

# Visuals — optional. Stock stills and footage instead of a ComfyUI server.
# Free from pexels.com/api/; 200 requests an hour is roughly forty reels.
PEXELS_API_KEY=

# Ingest — optional. Raises the GitHub rate limit from 60 to 5000 requests/hour.
GITHUB_TOKEN=
HUGGINGFACE_TOKEN=

# Administration — see "Securing the settings API" below.
REELFORGE_ADMIN_TOKEN=
```

Keys added later through the **Settings** page go to `data/secrets.json` with
owner-only permissions (`0600`) instead. They are never returned by any endpoint
— only their origin and a masked form.

---

## 4a. Run with Docker

```bash
docker compose -f docker/docker-compose.yml up --build
```

Four services come up: `api`, `worker`, `renderer` and `redis`. Open
**http://localhost:8020/v2**.

Compose publishes on `127.0.0.1` deliberately — the settings API accepts API
keys. See *Securing the settings API*.

### Fully local, no keys

Two optional profiles add a local model server and a local voice:

```bash
docker compose -f docker/docker-compose.yml \
  --profile local-llm --profile local-tts up --build

# pull a model once the ollama service is up
docker compose -f docker/docker-compose.yml exec ollama ollama pull qwen2.5:7b-instruct
```

| Profile | Service | What it gives you |
|---|---|---|
| `local-llm` | `ollama` on `:11434` | Script and storyboard generation, offline |
| `local-tts` | `kokoro-fastapi` on `:8880` | Narration, offline |

Expect weaker scripts from a 7B model — see the routing note in
[CONFIGURATION.md](CONFIGURATION.md). Everything still validates and renders.

---

## 4b. Run locally, without Docker

```bash
pip install -r requirements-dev.txt
./docker/fetch-fonts.sh
uvicorn app.main:app --workers 1 --reload --port 8020
```

**`--workers 1` is not optional.** The queue takes a file lock, but the app
already assumes a single process: the runner's thread pool and the mode cache are
both per-process. More than one worker gives you more than one scheduler.

In this mode there is no Redis and no Celery. Stages run inline in the same
process, which is why it is the better path for development.

### Fonts

`./docker/fetch-fonts.sh` downloads the bundled open-source faces into
`docker/fonts/`. It is idempotent, and the renderer image runs it at build time.

Helvetica Neue and Menlo are macOS-only and not redistributable, so the renderer
is repointed at:

- **Inter** — display face, the closest widely available match to Helvetica Neue.
- **DejaVu Sans Mono** — mono face. Menlo descends from Bitstream Vera Sans Mono,
  DejaVu's ancestor, and DejaVu carries every symbol the storyboards draw.

JetBrains Mono is fetched as an alternative but is **not** the default: it lacks
`↻ ★ ☆`, which render as empty boxes with no error at all.

`app/render/fonts.py` asserts glyph coverage at startup, and the renderer image
asserts it at build time, so a missing glyph fails loudly instead of shipping a
reel full of tofu boxes.

---

## 5. Check the environment

```bash
python -m app.cli doctor
```

This is the fastest way to find a missing binary, an unreachable model server or
a font without the glyphs the storyboards need.

To see what is reachable right now, and what each key has left:

```bash
python -m app.cli providers
```

`providers` reports each ElevenLabs key's remaining balance without spending
anything.

---

## 6. First reel

```bash
python -m app.cli new https://github.com/owner/repo --run
```

Or open `/v2`, click **New reel**, and paste a URL.

By default the job pauses at `content` and `storyboard` for review. Add
`--no-gates` to run straight through. See [USAGE.md](USAGE.md).

---

## Securing the settings API

The settings API accepts API keys, so it is guarded:

| Mode | When | Who can change settings |
|---|---|---|
| **loopback** (default) | `REELFORGE_ADMIN_TOKEN` empty | This machine only |
| **token** | `REELFORGE_ADMIN_TOKEN` set | Any host presenting that bearer token |

Compose binds to `127.0.0.1` to match the default. If you publish the port
beyond localhost, set the token first.

---

## Upgrading

`config.yaml` is the committed baseline and is mounted read-only in the
container. Your own changes made through the UI land in `data/settings.yaml` and
are merged over it, so pulling a new `config.yaml` never overwrites your setup.

An older `config.yaml` using the pre-profiles shape (a `provider:` key beside
fixed per-vendor blocks) is still read and converted automatically.
