# Configuration

Three layers, each overriding the one above:

```
config.yaml            the committed baseline; mounted read-only in the container
data/settings.yaml     what the Settings UI writes; merged over the baseline
environment variables  REELFORGE_<SECTION>__<KEY>, and every API key
```

Because UI edits land in `data/settings.yaml`, the file you edit by hand stays
yours and pulling a new `config.yaml` never overwrites your setup.

**Keys are never written to `config.yaml`.** It names the environment variable
(`api_key_env`); the value comes from the environment or from
`data/secrets.json`, which is created `0600` and is never returned by any
endpoint — only its origin and a masked form.

---

## Profiles

Providers are profiles, not fixed blocks, so several can share one adapter. A
single OpenAI-compatible adapter reaches OpenAI, vLLM, LM Studio, Together, Groq,
OpenRouter and Azure alike — they differ only by `base_url`.

```yaml
llm:
  active: local-vllm
  profiles:
    local-vllm: {adapter: openai,  base_url: "http://localhost:8000/v1",
                 model: qwen, json_mode: text}
    ollama:     {adapter: ollama,  base_url: "http://localhost:11434",
                 model: "qwen2.5:7b-instruct-q4_K_M"}
    hosted:     {adapter: openai,  base_url: "https://openrouter.ai/api/v1",
                 model: "...", api_key_env: OPENROUTER_API_KEY}
  roles:                        # per-stage routing; empty means "use active"
    content:    {profile: hosted}
    storyboard: {profile: hosted}

tts:
  active: upload
  profiles:
    upload:     {adapter: upload}
    elevenlabs: {adapter: elevenlabs, voice_id: "...",
                 api_key_env: ELEVENLABS_API_KEY}
```

Available LLM adapters: `openai` (any OpenAI-compatible endpoint), `ollama`,
`anthropic`, `fake` (for tests).
TTS adapters: `upload`, `elevenlabs`, `openai`, `kokoro` (self-hosted, free),
`fish` (self-hosted voice cloning), `local` (Piper and other generic HTTP
engines), `say` (macOS, development only).

An older `config.yaml` using the pre-profiles shape — a `provider:` key beside
fixed per-vendor blocks — is still read and converted automatically.

Any value can be overridden by environment:

```bash
REELFORGE_LLM__PROVIDER=ollama
REELFORGE_RENDER__WORKERS=4
```

---

## Model routing

The stages are not the same kind of work, so they need not use the same model.
Three presets:

| Preset | What it does |
|---|---|
| All local | Everything through Ollama. Free and offline; the script reads like a 7B model wrote it |
| **Hybrid** | A strong hosted model for the script and the storyboard, local for the rest. **Recommended** |
| All hosted | Every stage hosted. Best results, still cents per reel |

Hybrid is the one worth understanding. Writing the script and writing a
storyboard module are where model quality shows. Extracting a fact sheet or
choosing a palette are structured tasks with validators behind them, and a small
local model handles those fine.

---

## `json_mode` matters more than it looks

**Reasoning models must not use `json_schema`.** Grammar-constrained decoding
forces JSON from the first token, leaving the model no room to think, and it
stalls. Measured against a local vLLM: over five minutes with no result under
`json_schema`, versus 40 seconds under `text`.

| Mode | Use for |
|---|---|
| `json_schema` | An instruct model. Grammar-constrained, exact |
| `json_object` | Valid JSON, shape unenforced |
| `text` | **A reasoning model.** Schema in the prompt, JSON recovered from the reply |

The adapter falls through the list in order, so a server that rejects one mode
still works.

---

## Script generation

`content.script_mode` chooses how the narration is written:

| Mode | |
|---|---|
| `whole` | One call for the whole script. Best narrative flow; use with a strong model |
| `per_scene` | An outline call, then one call per scene, with continuation rounds |
| `auto` *(default)* | One whole-script attempt, then switch approach rather than ask the same way again |

Measured on `qwen2.5:7b-instruct`: the single-pass ask stalled at 30–48 words
across every retry and phrasing; per-scene reached 92–100 words in under twenty
seconds. A model that returns 40 words when asked for 95 returns 25 when asked
for 25.

Accumulated output is de-duplicated, fragments are merged into breath-sized
phrases, and split grouped numbers like `80, 000` are repaired.

Runtime targets live beside it:

```yaml
content:
  script_mode: auto
  target_seconds_min: 36.0
  target_seconds_max: 44.0
  max_attempts: 3
```

Keep a reel 30–45 seconds. One file then serves Reels (≤90 s) and Shorts (≤3 min).

---

## Approval gates

```yaml
approval:
  manual_stages: [content, storyboard]
```

Choose which stages stop for you. Presets for all-manual and all-auto are in
Settings, and a per-job setting overrides the saved default.

---

## Per-reel settings

Two things are chosen per reel rather than globally, on the **New reel** form:

- **Captions.** Burned-in, word-synced captions are the single highest-leverage
  thing in the pipeline, because most feed views start muted. Both platforms also
  auto-caption, so this is a real choice.
- **Fact validation.** Every claim on screen is a hostage. The checker compares
  what the script says against what `ingest` actually found.

---

## Generated imagery (ComfyUI)

```yaml
visuals:
  active: comfyui              # `none` switches the whole thing off
  profiles:
    comfyui:
      adapter: comfyui
      base_url: http://gpu-box:8188
      image_workflow: app/workflows/image_qwen_image_2512.json
      video_workflow: app/workflows/video_ltx2_5_t2v.json
  stills: 2
  clips: 1
  clip_seconds: 5.0
  cover: true
```

A third profile kind beside models and voices, with the same Settings page
controls: add, test, use. `none` is always present and is the default, so a
clone behaves exactly as it did before this existed.

With a ComfyUI profile active, a `visuals` stage runs after `cover`:

- **Stills.** One per body scene (never the hook or the close), prompted from
  the scene's `on_screen` direction with the typography instructions stripped
  out -- "the word *requests* types itself in" is a picture of text, which the
  storyboard draws itself anyway. Placed by the same screen catalogue as an
  uploaded screenshot, so the `screenshot` layouts carry them.
- **Clips.** From the scene with a written `b_roll` shot (the content prompt
  asks for one; nothing read it until now), else the longest body scene.
  Delivered as JPEG frames at 1080x1920 / 30 fps -- the renderer is a PIL
  frame pump and cannot decode video, the same reason the repo B-roll is one
  tall screenshot. A `motion` layout in every family pastes frame *n* and
  ping-pongs when the scene outlasts the clip.
- **Cover backdrop.** A 1080x1920 picture darkened toward the palette under
  the wordmark and hook, replacing the procedural ground. The typography, the
  stat cards and the Instagram centre-crop rule are untouched. Fails soft: a
  server that is down gives the ordinary cover.

Every prompt gets the template's palette words and the `style` suffix, so a
reel's assets share one look. Seeds are derived from the job and the prompt,
so a re-run with nothing changed reproduces the same picture and skips the
generation; editing `visuals/visuals.json` is not the way to change a prompt --
edit the scene's direction on the content gate and re-run.

**Workflows.** The two shipped files are ComfyUI *API-format* exports. To use
your own: Workflow > Export (API), save it under `app/workflows/`, and set
`image_workflow` / `video_workflow`. The adapter finds the prompt (the text
node feeding the sampler's `positive`, through a switch if there is one), the
seed, the empty-latent size and the Save node by walking the graph; for a
graph where that guesses wrong, name the node ids in `image_nodes` /
`video_nodes` -- see the shipped map in `config.yaml`. A UI-format export (the
one with a `nodes` list) is refused with a message saying which export to use.

**Testing a profile** probes `/system_stats`, reports the GPUs, and checks
every `*Loader` node's model name against what the server lists, so a missing
checkpoint shows up before a job spends ten minutes finding out.

**Per reel:** the New reel form has *use configured / on / off*, and
`PATCH /api/jobs/{id}` takes `{"visuals": {"enabled": ..., "stills": ...,
"clips": ..., "cover": ..., "profile": ...}}`. Changing it re-runs from
`content` downstream, because the backdrop is drawn by the cover stage.

## Stock footage (Pexels)

The other way to get pictures, and the one that needs no hardware:

```yaml
visuals:
  active: pexels
  profiles:
    pexels:
      adapter: pexels
      api_key_env: PEXELS_API_KEY
      orientation: portrait
      pool: 15
  stills: 1
  clips: 3
```

A free key from [pexels.com/api](https://www.pexels.com/api/), pasted into
Settings or set in `.env`, and every reel gets photographs and real motion
footage instead of generated stills. Same profile controls as any other —
add, test, use — and the same `stills` / `clips` / `cover` counts. Nothing
else in the pipeline changes: the pictures are placed by the same screen
catalogue, the clips are the same JPEG frames, the `motion` layout ping-pongs
them the same way.

**It searches, it does not draw.** That is the whole difference, and it shows
up in three places:

- **The search term matters more than the prompt.** The art director already
  rewrites each scene into a text-free shot; it now also returns a two-to-four
  word `query` for that same subject, and that is what gets searched. With
  `art_director: false`, or when the model call fails, the query is cut down
  from the shot description by rule instead — blunter, but it still finds
  something.
- **The seed picks rather than generates.** One search returns `pool` results
  and the scene's seed chooses among them, so an unchanged direction
  reproduces the same photograph on a re-run, exactly as an unchanged prompt
  reproduces the same generation. Two scenes never get the same picture.
  Raising `pool` varies the choice and costs no extra request.
- **The pictures have authors.** Pexels asks for a link back and a credit, so
  each asset records its photographer and page URL in `visuals.json`, and the
  package stage writes them into the bundle: a `## Credits` section in
  `<slug>-reel-notes.md`, and a `Footage: Pexels (…)` line appended to
  `captions.txt` and every file under `metadata/`. Nothing is burned into the
  video.

**Clips** are trimmed, not padded. Stock footage is ten to thirty seconds and a
reel wants `clip_seconds` of it, so ffmpeg cuts the length it needs starting
`clip_start` seconds in — stock shots routinely open on a fade or a settling
camera, and the reel cuts to the first frame with no run-up. Of the files
Pexels offers for a clip, the smallest one at least 1080 wide wins: both are
centre-cropped to the same 1080×1920 and only one of them is a 300 MB
download. Anything over 120 MB is refused outright.

**Keep `orientation: portrait`.** Frames are scaled to cover and centre-cropped,
so a landscape source keeps a 1080-wide sliver out of the middle and throws
away the rest.

**No audio.** Pexels has no sound library, so `visuals.music` and
`sfx_samples` do nothing on this profile — the stage says so and carries on
with the synthesized cut sounds. A music bed needs a ComfyUI profile with an
`audio_workflow`.

**Rate limits.** 200 requests an hour and 20,000 a month on a free key. A reel
spends one request per picture, so about forty reels an hour. **Test** reports
what is left on the quota.

### Generated audio: a music bed and the cut sounds

The same ComfyUI profile can carry a text-to-audio workflow (`audio_workflow`;
a Stable Audio 3 graph ships in `app/workflows/`). It is **not speech** -- a
text-to-audio model has no words, and narration stays with the voice profiles
(see *Fish-Speech* below for a self-hosted voice). What it gives a reel:

- **A music bed.** `visuals.music: true` (or per reel) plans one 60 s
  instrumental from the template's `music` hint -- lo-fi for `warm-amber`,
  sparse piano for `editorial` -- and the render stage mixes it under the
  narration: `music_gain_db` below full scale, ducked a further
  `music_duck_db` while the narrator speaks (an envelope follower with a fast
  attack and a slow release, so pauses let it breathe without pumping), faded
  in at the top and out under the end card. The narration itself is never
  touched and the loudness pass downstream sees the same kind of material.
  Off by default: DEVELOPMENT.md's rule is voice only, and a bed anyone
  notices is too loud.
- **Cut sounds.** `visuals.sfx_samples: true` replaces the synthesized
  thump/swish/tick set (twelve kinds across the three families, see
  `video/sfx.py`) with generated one-shots. Made once per profile into
  `data/sfx/<profile>/` -- `python -m app.cli sfx-library` does it up front,
  or the first reel that asks does -- and swapped in by the mix shim with the
  storyboard's `amp` and `dur` honoured, so no storyboard changes.

### Kokoro narration

The cheapest good voice: self-hosted, free, no key, and it runs on CPU.

```yaml
tts:
  active: kokoro
  profiles:
    kokoro:
      adapter: kokoro
      base_url: http://localhost:8080
      voice: af_heart
      speed: 1.0
```

**Two servers speak this API and one adapter drives both.**

| | port | voices | how |
|---|---|---|---|
| [Kokoro-TTS-OpenAPI](https://github.com/mkamranr/Kokoro-TTS-OpenAPI) | 8080 | 28 English, graded | `./scripts/setup_mac.sh` then `python -m app` — CPU-native on macOS, no container |
| [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) | 8880 | more, multilingual | `docker compose -f docker/docker-compose.yml --profile local-tts up -d tts` |

Synthesis is identical on both — `POST /v1/audio/speech`, OpenAI-shaped. They
differ only in where the voice list lives (`/voices` and `/v1/audio/voices`
respectively), and the adapter tries both rather than making you say which you
run. Point `base_url` at whichever you have.

**Voices are listed, not memorised.** The profile editor's **Voice** field is
a picker filled from the server. Where the server describes its voices
properly, the description carries through — *Heart — American female (A)* —
and the **quality grade is the useful part**: an A voice and a C voice are
audibly different, and the narration is the entire soundtrack of a reel. The
field still accepts anything typed, so a weighted blend works:
`af_bella(2)+af_heart(1)` is a 67/33 mix the server resolves itself.

**Test checks the voice, not just the port.** A voice id that does not exist
is not rejected when you save it — it fails on the first phrase, a dozen calls
into a narration. So the probe compares the configured voice against the
served list and names the near miss (`af_heartt` → *close by: af_heart,
af_bella*).

**Speed** changes how long the reel is, not just how it sounds: the storyboard
paces its screens to the narration, so a slower voice makes a longer video.

**It is not fast on CPU.** Measured on an Intel Mac against
Kokoro-TTS-OpenAPI, synthesis runs about **4× slower than real time** — a
4.7-second phrase took ~20 seconds. A 40-second narration is therefore a few
minutes, spread over a dozen per-phrase calls, and `request_timeout` defaults
to 300 s for that reason. A GPU changes this completely; a hosted engine
avoids it entirely.

If the server has `KOKORO_API_KEY` set, name the variable holding it in **API
key variable** and it is sent as a bearer token. Most self-hosted setups have
none.

For **Piper** or any other self-hosted engine that takes `{text, voice}` over
HTTP, use the generic `local` adapter instead; it has no voice listing,
because those servers do not offer one.

### Fish-Speech narration

```yaml
tts:
  active: fish
  profiles:
    fish:
      adapter: fish
      base_url: http://gpu-box:7860      # the Gradio app
      reference_audio: ""                # a 10-30 s wav of the voice to clone
      reference_text: ""                 # its transcript
      seed: 1
```

A voice adapter for Fish-Speech / OpenAudio behind its Gradio app (needs
`gradio_client`, in `requirements.txt`). Phrases are synthesized one by one,
and without a reference the model is free to pick a different timbre per
call -- so the adapter always uses one. Upload a recording to clone in the
profile's editor (**Sample voice**, either UI; it lands in
`data/tts/references/<profile>.<ext>` and goes to the Gradio API as
`reference_audio` with every phrase, the transcript as `reference_text`), or
let it fix a voice itself: one seeded calibration sentence, generated once into
`data/tts/fish/` and fed back as the reference for every phrase of every reel.
Change the seed to change the voice.

The reel is as long as the narration turns out to be: the storyboard paces
its screens to the total, so a slow voice simply makes a longer reel. The
36-44s target remains script guidance; the only hard bounds are the
platforms' own 30-90s, checked before the render rather than after it.

**Codegen templates** (`cool-indigo`, `warm-amber`, `editorial`, `research`)
are told which files exist, with a four-line snippet for drawing a still or a
clip frame, and may use them in the scenes they belong to. The deterministic
templates and the fallback always do.

---

## Multiple ElevenLabs keys

Credits are metered per character and a narration is a dozen calls, so an
allowance can run out mid-job. List several keys and the next takes over
automatically:

```bash
# .env — either comma-separated in one variable...
ELEVENLABS_API_KEY=key_one,key_two

# ...or separate variables named in config.yaml under api_key_envs
ELEVENLABS_API_KEY_2=
```

```yaml
tts:
  elevenlabs:
    api_key_envs: [ELEVENLABS_API_KEY, ELEVENLABS_API_KEY_2, ELEVENLABS_API_KEY_3]
    active_key: null                 # null rotates; a label or index pins one
    max_characters_per_job: 4000     # a 40 s reel is ~550-700 characters
```

`max_characters_per_job` exists so an accidentally long job fails **before** it
spends anything. `GET /api/config/providers` reports each key's remaining balance
without spending anything.

---

## Rendering

`render.ffmpeg_dir` names a directory holding the ffmpeg/ffprobe to use; it is
put first on PATH for the app and every child it spawns. The encode settings
need ffmpeg 4.0 or later -- an older build rejects `-colorspace bt709` and
the chunk renderer fails with a bare broken pipe. `doctor` reports the
version it found.

```yaml
render:
  chunk_seconds: 4.0      # must be a whole number of 2 s GOPs
  workers: 0              # 0 = decide from the machine (cores - 1, capped at 8)
  safe_encode: false      # true = single-process pipe, exactly like make.sh
  fps: 30
  width: 1080
  height: 1920
```

Each worker runs a renderer *and* an encoder, so setting `workers` to twice the
core count does not go faster — it thrashes.

`safe_encode: true` runs the reference path, byte-for-byte the command in
`video/make.sh`. Use it to check a chunked result against.

### Encode settings — do not drift

```
1080x1920 · 30 fps · H.264 High @ 4.1 · yuv420p · CRF 18
closed GOP every 2 s · bt709 tagged
AAC-LC 192 kbps 48 kHz stereo · +faststart
audio normalised to -14 LUFS / -1.5 dBTP
```

−14 LUFS is what both platforms normalise to: hit it and neither turns the video
down. **The bt709 tags matter** — untagged, both platforms render the colours
washed out.

---

## Storyboard validation

```yaml
storyboard:
  max_repair_attempts: 3
  vision_review: false           # needs a multimodal provider
  fallback_to_template: true
  smoke_frames: 16
  smoke_timeout_seconds: 90
  memory_limit_mb: 3072
```

`fallback_to_template: true` is what guarantees a job always produces a video:
when generation exhausts its repair budget, the job renders through
`safe-deterministic` instead.

---

## Ingest

```yaml
ingest:
  github_token_env: GITHUB_TOKEN
  hf_token_env: HUGGINGFACE_TOKEN
  timeout_seconds: 30
  readme_max_chars: 60000
```

A GitHub token is optional and raises the rate limit from 60 to 5000 requests an
hour. A Hugging Face token is only needed for gated repositories.

---

## Securing the settings API

The settings API accepts API keys, so it is guarded:

| Mode | When | Who can change settings |
|---|---|---|
| **loopback** *(default)* | `REELFORGE_ADMIN_TOKEN` empty | This machine only |
| **token** | `REELFORGE_ADMIN_TOKEN` set | Any host presenting that bearer token |

The token applies to **everyone** once set, including a browser on the machine
itself — the check tests the token before it tests the peer address. The
Settings page asks for it and keeps it in that browser's local storage. Empty
or whitespace counts as unset.

It goes in the root `.env` (passed into the containers), not `docker/.env`
(compose's own variables). And it guards `/api/settings` only: jobs, the
queue, artifact download and the provider key balances stay open to anyone who
can reach the port.

Compose binds to `127.0.0.1` by default; `REELFORGE_BIND` in `docker/.env`
widens it. See **[SETUP.md](SETUP.md)** — *Securing the settings API* and
*Publishing the port*.
