# Development notes

Working notes for the renderer in `video/` and the service in `app/`: the
constraints, the measurements behind the settings, and the gotchas that each
cost real time to find. For installing and running, start with
[README.md](README.md) and [docs/SETUP.md](docs/SETUP.md).

---

## Vertical reels for open-source dev tools

This repo turns a written script + a voice recording into a finished vertical video
that Instagram Reels and YouTube Shorts both accept without re-export.

Built (21). Each has `<name>-reel.mp4`, `-reel.png`, `-reel-notes.md`, a
storyboard, and a caption block in `captions.txt`.

- **Bloom** (`sbkit.py`), 11: `ECC`, `agentic-awesome-skills`,
  `deepseek-harness`, `open-design`, `deer-flow`, `ruflo`, `qwen3-8-27b`,
  `ltx-2-5`, `muse-glimmer-30b`, `agenticseek`, `nemoclaw`
- **Ledger** (`ledger.py`), 4: `caveman`, `graphify`, `colibri`, `odysseus`
- **Slab** (`slab.py`), 6: `anydoc`, `grok-build`, `qm`, `openworker`,
  `mempalace`, `career-ops`

Captions state, as of 22 Aug 2026: the four Ledger reels and `ltx-2-5` are the
five without burned-in captions (`ltx-2-5` was already switched off before this
default was set). The other Bloom reels still have them and would lose them on
a rebuild only if their storyboard gets the `CAPTIONS` flag too — none has been
changed. Not built: `ponytail` (deliberately skipped).

`qwen3-8-27b`, `ltx-2-5` and `muse-glimmer-30b` are **model** releases rather than
tools and share their own design language: a left rail carrying the model's own
structure, square panels with a lit left edge, and `covers.py`'s `layout="stack"`
renderer. The rail differs per model — Qwen's is its 64-layer stack, LTX's a filmstrip
of shot cells, Muse Glimmer's a token stream that fills across the runtime — and each
picks its own palette.

**Known pipeline bug.** `make.sh`'s single-pass `loudnorm` lands every reel over the
-1.5 dBTP ceiling: **AAC overshoots true peak after the filter has already hit its
target**. The fix is a two-pass loudnorm requesting a lower peak, re-muxed audio-only
with `-c:v copy`. The amount of overshoot is **material-dependent** — `I=-13.0 TP=-2.5`
worked for two reels and failed on a third (-1.1 dBTP, over the ceiling), which needed
`I=-11.8 TP=-2.9`. So: measure the mix, tune, and verify the ENCODED file each time.
Remember less negative is louder. See `muse-glimmer-30b-reel-notes.md` for the method
and a per-reel results table.

**A quiet reel cannot be fixed by asking for more loudness.** `nemoclaw` shipped at
-15.4 LUFS because its recording has a 23 dB crest factor (`I=-26.3 TP=-3.1`), and
reaching -14 under a -1.5 dBTP ceiling would need heavy compression, not
normalisation. Raising the requested `I` makes it *quieter* — the tighter peak
target makes the limiter work harder. The curve plateaus around -15.3 then falls.
Compression ahead of loudnorm buys 0.4 LU and costs real dynamics, so it was
rejected: narration stays untouched. The fix for that is a re-record, not a filter.
`nemoclaw-reel-notes.md` has both sweeps.

**Tag/slug collision.** The model-release storyboards put a left-aligned tag at y 206
and `sbkit.chrome` puts a right-aligned slug there too. Derive the tag's width limit
from the slug (`SLUG_X = RIGHT - tw(SLUG, m(25))`) rather than hardcoding an x —
a hardcoded 740 silently broke when a longer slug arrived.

Not built: `superpowers.txt` / `.mp3` / `.png`.

`agentic-awesome-skills-reel.mp4` is **truncated** — 36.57 s instead of 39.0 s,
missing its end card. A rebuild fixes it: `./make.sh aas ../agentic-awesome-skills-reel.mp4`

## The application (`app/`)

`app/` wraps this pipeline in a job-based service: paste a GitHub or Hugging Face
URL, and it fetches the facts, writes the script and per-platform copy, generates
or accepts narration, aligns it, writes the storyboard, renders, verifies and
packages. Two web UIs and a CLI:

- `http://localhost:8020/v2` — the current one. Separate pages, no build step,
  no CDN. Add a page by dropping an `.html` into `app/ui_v2/` and a matching
  `static/<page>.js`; `tests/test_ui_v2.py` picks it up automatically.
- `http://localhost:8020/` — the original single-file SPA. **Frozen**: it is the
  escape hatch while v2 settles, and sharing code with it would couple the thing
  being replaced to its replacement.
- `python -m app.cli`

```bash
docker compose --env-file .env -f docker/docker-compose.yml up --build   # api + worker + renderer + redis
python -m app.cli doctor                                 # check the environment
python -m app.cli new https://github.com/o/r --run
uvicorn app.main:app --workers 1 --port 8020            # one worker; see the queue
```

### One reel at a time

Reels queue and run **one at a time, end to end**. A reel spends about 8.6 of its
11.3 minutes inside `content` and `storyboard`, both LLM calls — two at once is
two workloads against one GPU, and the executor used to do exactly that with
`ThreadPoolExecutor(max_workers=2)`.

- Queue state lives in `job.json` (`job.queue`), not a separate index, so it is
  rebuilt by scanning jobs and **survives a restart with no recovery code**. Only
  a job that was *running* is touched, and it leaves the queue rather than going
  back in — re-queueing would send it into the stage that just died, for ever.
- `app/queue_store.py` is all state and no threads; `app/scheduler.py` is one
  thread whose `tick()` is synchronous so the tests need no sleeps.
- A single-stage run or a retry **joins the queue at the front** rather than
  bypassing it. Bypassing is the second concurrent LLM call this exists to stop.
- Cancel takes effect at a stage boundary, never mid-stage — gotcha 8 below.
- Two consecutive failures pause the queue: that is a broken model server, not
  two broken reels.
- **Run uvicorn with `--workers 1`.** The queue takes a file lock, but the app
  already assumes one process (`runner`'s pool, `mode()`'s cache).
- Under Celery the broker owns concurrency; reorder and pause return 501 there.

### Reel geometry lives in Python

`GET /api/images/frame` serves the frame constants to the browser so the upload
preview cannot drift from the renderer. They live in three places —
`app/images.py`, inside the emitted storyboard template, and `video/sbkit.py`
(read-only) — and three of them moved in one afternoon. Never retype them into
JavaScript.

**`video/` is not modified by any of this, and must stay that way.** The app
drives it through two mechanisms, both of which exist because of constraints
documented in the gotchas below:

- **Per-job symlink farm** (`app/render/workspace.py`). `align.py` and
  `timing.py` resolve `build/` from their own `__file__`, and `align.py` uses one
  fixed scratch file, so concurrent jobs would collide. A directory of symlinks
  to the real modules moves `HERE` into the job directory — `os.path.abspath`
  normalises but does not resolve symlinks — giving each job a private `build/`.
- **Shims** (`app/render/shim_*.py`). Thin wrappers that patch a module global
  and delegate: `shim_render` replaces `kit.f`/`kit.m` with bundled OSS faces
  before `sbkit` is imported, `shim_align` merges the generated pronunciation map
  into `align.SPOKEN`, `shim_cover` injects a generated spec into `covers.SPECS`.

Things worth knowing before changing it:

- **Fonts.** Helvetica Neue and Menlo are macOS-only and not redistributable.
  The container uses Inter for display and **DejaVu Sans Mono** for mono — chosen
  because Menlo descends from Bitstream Vera Sans Mono, DejaVu's ancestor, and
  because JetBrains Mono is missing `↻ ★ ☆`, which draw as empty boxes with no
  error (gotcha 3). `app/render/fonts.py` asserts glyph coverage at startup and
  the renderer image asserts it at build time.
- **`video/bin/` does not run in a container** — those are macOS x86_64 builds.
  `align.py`'s `tool()` already falls back to `PATH`, which is what happens there.
- **Chunked rendering.** `render.py --only T0 T1` is the seam. Chunks are cut on
  whole 2 s GOP boundaries so the concat demuxer joins them with `-c copy`, and
  the joined frame count is asserted against `round(TOTAL*30)` before audio is
  attached (gotcha 8). A 40 s reel takes about 60 s instead of 7-9 minutes.
  `render.safe_encode: true` runs the single-process path, byte-for-byte the
  command in `make.sh`, as the reference to check a chunked result against.
- **Generated storyboards** pass eight checks before a full render: AST parse,
  import allowlist, the module contract, **every `ws()` cue word checked against
  the words actually spoken**, scene-table sanity, sample frames rendered in a
  limited subprocess, a legibility-based safe-area check, and optionally a
  multimodal review. The cue-word check is the one that earns its keep — `ws()`
  raises on an unknown word, and inventing one is the likeliest mistake.
- **Reasoning models and JSON grammar do not mix.** `json_mode: json_schema`
  forces JSON from the first token, leaving a reasoning model no room to think:
  measured against a local vLLM, over five minutes with no result versus 40
  seconds under `json_mode: text`. Use `text` for reasoning models.

## Three templates

Reels are built on one of three design languages. They share `kit.py`, the
renderer, the mixer and the encode settings, and nothing else.

| | **Bloom** (`sbkit.py`) | **Ledger** (`ledger.py`) | **Slab** (`slab.py`) |
|---|---|---|---|
| ground | near-black, radial blooms | near-black, faint grid | full-bleed colour FIELD, one per scene |
| layout | centred column | left-aligned off a spine at x 180 | left-aligned, edge to edge, no column |
| surfaces | rounded cards, glow | hairline rules, whitespace | solid fills only, no outlines anywhere |
| elements/frame | 8–15 | 8–15 | **at most 5** |
| min type | ~23px | ~24px | **34px**, headline 96–150 |
| type colour | hand-picked | hand-picked | **derived** from field luminance |
| entrance | fade up + slide | horizontal wipe from the spine | the type RISES into place, hard-edged |
| transition | sweep line crosses | spine flashes, tick runs down | a band of the INCOMING field sweeps down |
| chrome | progress bar + slug | gutter scene index | segmented rail, one segment per scene |
| sound | `thump` `swish` `tick` `sweep` | `click` `rule` `latch` `shift` | `slam` `paper` `chime` `riser` |
| cover | `render` / `render_stack` | `render_ledger` | `render_slab`, `layout="slab"` |

**Why Slab exists.** Bloom and Ledger differ in styling but share two real
weaknesses: both are near-black at low contrast, and both put 8–15 elements on a
frame, much of it mono type at 23–27px that is marginal on a phone held at arm's
length. Slab fixes those structurally rather than by taste — a full-bleed field
means there is no dead band by construction, and `slab.ink_for()` picks black or
white type from the field's own relative luminance, so an illegible frame is not
possible by accident. The trade is real and worth stating: Slab carries **less
information per frame**. A dense repo needs more scenes on Slab than on Ledger,
and a fact sheet that fits Ledger will not all fit Slab. That forced simplifying
is most of why it reads better.

Captions are off by default in all three (see below). The machinery lives in
`sbkit.captions` and `ledger.captions`; Slab has none at all, since a
field-based frame has no dark band for a caption plate to sit on.

**All three are selectable in ReelForge.** `app/render/fallback_storyboard.py`
carries a `FAMILIES` registry — `bloom` / `ledger` / `slab` — each naming an
emitted module template and a screen catalogue. All three consume the *same*
resolved DATA, so content work is shared and adding a fourth is one row plus a
YAML file. `Template.family` selects one; `ledger` and `slab` are registered as
deterministic templates, so they render from data with no codegen.

Only the deterministic templates get the repo scroll. The four codegen
templates (`cool-indigo`, `warm-amber`, `editorial`, `research`) produce
model-written storyboards that know nothing about it — if codegen exhausts its
attempts and falls back, the fallback does capture one.

**Ledger and Slab are additive beyond that, and ReelForge could not see either
until this was wired.** Both import
`kit` and nothing else — never `sbkit`, never each other, so no two templates
share module state. The app's `ALLOWED_IMPORTS` in
`app/validate/storyboard.py` is `{…, kit, sbkit, timing}`, so a generated
storyboard cannot import `ledger` or `slab` — which is intended. Adding either
there is the opt-in if ReelForge should ever emit those looks. Edits to shared
files are additive only: new kinds in `sfx.py`'s `make_gens()`, new `layout=`
branches in `covers.py`. Verified after Ledger: all nine existing covers
re-rendered byte-identical.

Slab's grid: `CX 96` · `CR 984`, dropping to `CR_LOW 932` below y 1000 for the
platform button column (`slab.right_edge(y)`); rail at y 128, footer at 1524.
`slab.cut()` must be called **before** `rail` and `footer` — the band is opaque
and would otherwise blank the chrome on every transition.

Ledger's own grid: `GUT_X 118` (index numerals) · `RULE_X 180` (spine) ·
`CX 232` (content) · `RIGHT 996`, dropping to `RIGHT_LOW 932` below y 1000 for
the platform button column. `ledger.right_edge(y)` returns the correct one.

## The repo-scroll B-roll

`app/render/reposhot.py` screenshots the repository page and the storyboard pans
it top to bottom under the narration, as one screen about 7-13 s in.

**One tall screenshot, not a screen recording.** The reel is drawn frame by
frame in PIL, so a video would have to be decoded back into frames anyway. A
single full-page PNG is ~200x fewer captures, pans at sub-pixel smoothness with
any easing, costs one file instead of a few hundred, and decouples capture time
from scene duration. A repo page is static, so nothing is lost.

- Captured at **1080 wide**, the reel's own width, so there is no rescale later.
  Trimmed at 14000px; declined below 2600px, where there is nothing to scroll.
- **Best-effort by design.** No browser, a page that will not load, or a page
  too short all return `None`, and the reel is built without that screen. A
  missing B-roll costs nothing; a broken one is on screen for eight seconds.
- Placement is not special-cased: the layout is weighted 99, above everything
  else in the body, and `screens.pick()` fills the middle in weight order — so
  it lands at index 1, right after the opener.
- **Distance is `speed x time`, not "the whole page however tall it is".**
  `slab.SCROLL_SPEED` is 400 frame-pixels/second and both families honour a
  per-layout `speed:` in the catalogue. The first version had no rate at all --
  it fitted the capture into whatever time the scene had, which for an 8768px
  GitHub page in a 6s scene meant **~1600 px/s and read as a blur**. Reaching
  the page's footer is not the goal; showing the repo is, and 4-5s of legible
  travel does that.
- The pan **holds at the top** for 12% of the screen before travelling, so the
  repo name and header are readable rather than smearing past, and eases in and
  out of the travel.
- Both families support it. Slab is full bleed; **Bloom pans inside the panel
  band**, because Bloom burns captions in and caption text over a scrolling
  README is unreadable.

`playwright` is in `requirements.txt` and needs `playwright install chromium`.
On a host where that is refused — this one reports "does not support chromium on
mac12" — `reposhot._executable()` finds a Chromium already in the Playwright
cache or an installed Chrome and drives that instead.

Scrim geometry is worth knowing before changing it: the scrims are **solid**
field colour across the bands the chrome actually occupies, then fade out. A
first pass faded to zero by y 250 and restarted at y 1600, which left the rail
at y 128 half-covered and the footer at y 1524 not covered at all — legible in
a still, illegible in the render.

## Generated clips and stills

`app/stages/visuals.py` asks a picture backend (`app/providers/visuals/`) —
a ComfyUI server that draws them, or Pexels, which searches for them — and
delivers them the only way this renderer can use them: a still is
a prepared PNG beside the uploaded screenshots, a clip is a directory of JPEG
frames at 1080x1920 / 30 fps under `<job>/video/images/clip-<n>/`. The emitted
storyboards gain a `clip` slot (`sl_clip` in all three families) that pastes
frame `int((t - t0) * fps)` through a twelve-frame LRU and ping-pongs past the
end, and the catalogues gain a `motion` layout weighted between the repo scroll
and the screenshot.

Two things the smoke check taught while building it. A full-bleed picture
trips the **safe-area** rung at the sides unless its alpha fades over the
margins (`_side_mask` in Slab, `_edge_fade` in Bloom) -- the check measures
bright pixels under the platform's UI columns, and a photo has them. And a
dark clip under the scrims trips the **sparse** rung, so every clip screen
also carries its scene title as text. The Slab family already trips safe-area
with the test fixture's light palette, clip or no clip; that is a pre-existing
condition the visuals tests compare against rather than hide.

The one thing a searching backend needs that a generating one does not is a
**search term**. A diffusion prompt is 25-45 words of cinematic description
and matches nothing in a stock library, so `query` travels beside `prompt`
all the way from the art director (a fourth field next to `still`, `clip` and
`heading`) to `VisualsProvider.still` / `.clip`. ComfyUI ignores it. Without
an art director, `search_query()` cuts the shot description down to three
nouns by rule, because a picture with no term at all is a hole in the reel.
The seed then picks among the search results rather than seeding a sampler,
which keeps the stage's reuse check honest: same prompt and seed, same
photograph, no second fetch. Note that a stock clip is whatever the
photographer shot — often half a minute — so `extract_frames` trims with
`-ss`/`-t` rather than exploding all of it.

Audio goes in through `shim_sfx.py` the same way, from `app/render/soundbed.py`:
`sfx.make_gens` is replaced on the loaded module so generated one-shots stand
in for the synthesized kinds (same `amp`, same `dur`), and the music bed is
mixed under the narration *after* `sfx.build()` has written the mix -- the
narration buffer is never touched. One pre-existing bug surfaced the first
time a bed went through: `chunked.py`'s loudness message called `progress`
with one argument, and only a multi-pass loudnorm ever reached it.

The cover backdrop goes in through `shim_cover.py` the same way the spec does:
`covers.ground` and the motif are module attributes, so the shim replaces them
with `app/render/backdrop.py`'s darkened picture and `covers.py` never learns
about it.

## Layout

```
<name>.txt                 script: scenes, narration read-through, fact sheet
<name>.mp3                 the voice recording (the clock for everything)
<name>-reel.png            cover art -- also the source of truth for styling
<name>-reel.mp4            OUTPUT
<name>-reel-notes.md       OUTPUT: specs, rationale, Shorts title/description
captions.txt               Instagram captions for all three reels

video/
  kit.py                   canvas, palette, fonts, easing, drawing primitives
  align.py                 audio -> phrase segments -> word-level timing
  timing.py                loads build/<sb>.timing.json; word lookup by name
  sfx.py                   narration + sound-design events -> 48k stereo mix
  render.py                storyboard -> raw RGBA frames on stdout
  covers.py                reel cover art (four example specs, one per layout)
  sbkit.py                 shared storyboard machinery -- Theme, ground, chrome,
                           captions, and the statcard/tile/terminal/counter/bar
                           components. New storyboards should use this; ecc.py
                           and aas.py predate it and inline their own copies.
  make.sh                  align -> mix -> render -> encode -> verify
  verify.sh                check a finished file against platform requirements
  frames.sh                pull frames from an mp4, tile with UI chrome overlaid
  safecheck.py             the chrome overlay used by frames.sh
  phrases/<sb>.txt         narration split to one line per voiced phrase
  storyboards/<sb>.py      the video itself: palette, scene list, frame(t)
  bin/ffmpeg, bin/ffprobe  vendored static builds (~135 MB, not from brew)
  build/                   generated timing / mixes / logs -- disposable
```

`video/bin/` and `video/build/` are generated artifacts, and both are in
`.gitignore` along with `data/` and `.env`.

**Reel artefacts are not version-controlled.** The repository is the
application; the reels are its output. Scripts, narration, cover art, notes and
the finished `.mp4` are all ignored — they belong in `data/jobs/<id>/` and
`out/`. The four storyboards the templates use as worked examples
are shipped in `app/templates/examples/`, read as text for the prompt and never
imported.

The files described in the layout above still live in this working directory;
they are simply not committed.

## Cover art

`video/covers.py` renders a cover from a spec — palette, wordmark, hook, three
stat cards, a command bar, a background motif — and writes `<name>-reel.png`.

`SPECS` holds four **examples**, one per layout (`example`, `example-stack`,
`example-ledger`, `example-slab`), kept as the reference for the shape each
renderer expects. The covers actually shipped are job output and live in their
job directory; the app never edits this table — `app/render/shim_cover.py`
injects a generated spec into `SPECS` at render time and calls the matching
renderer. `CoverSpec` in `app/models/content.py` mirrors the shape, so a field
added here has to be added there too.

```bash
cd video && python3 covers.py            # every spec in the table
python3 covers.py example-ledger         # one
```

Instagram shows a **centre-square crop** of the cover in the grid, so the
wordmark and hook both sit inside y 420–1500 and survive that crop. Sample each
palette from the repo's own branding, and keep the cover headline and the video's
opening line the same words — that pairing is what the search index reads.

The command bar takes real, runnable commands only. When a repo has no short
one, set `prompt=False` and the bar renders as a feature strip instead of faking
a shell prompt.

## Build a video

```bash
cd video
./make.sh ecc ../ECC-reel.mp4          # ~9 min; ALWAYS run in the background
```

Rendering is ~3-9 fps, so a 40-second reel takes 7-9 minutes. Launch it in the
background and tail `build/<sb>.render.log`. Never sit in the foreground waiting.

Both shipped mp4s are reproducible with this command.

## Making a new one

1. **Write the script.** Follow the shape of `ECC.txt`: scene-by-scene with an
   on-screen column, then a clean narration read-through, then a fact sheet. The
   fact sheet is what fills the screen, so make it dense and specific.
2. **Record the narration**, then find the phrase boundaries:
   ```bash
   python3 align.py probe ../NEW.mp3
   ```
3. **Write `phrases/new.txt`** — one line per detected segment, in order, covering
   the whole narration. `align.py build` refuses to run unless the counts match,
   and prints a side-by-side diff when they don't.
   **Then sanity-check it:** every phrase break must land on a real clause or
   sentence boundary. If a break falls mid-clause, the split is wrong — re-read
   the probe output rather than reaching for `--min-sil`.
4. **Copy the closest storyboard** (`ecc.py` warm, `aas.py` cool) and edit it.
5. `./make.sh new` in the background, then **look at the result** (see Verifying).

## Writing a storyboard

Copy the closest existing one and edit. `deepseek-harness.py`, `open-design.py`,
`deer-flow.py` and `ruflo.py` all build on `sbkit.py` and are the models to
follow; `ecc.py` and `aas.py` work but duplicate machinery that now lives in
sbkit.

The pattern each uses: a `Theme`, a scene table `SC=[(name,start,end),…]`, a
`scene_at(t)` dispatcher, one function per scene, then `S.chrome` / `S.cut_sweep`
/ `S.captions` applied to every frame. Scene functions take `(ov,d,t,t0)` and
draw everything into `ov`.

**Cut on the narration, not on round numbers.** Twice now a scene boundary landed
just before the line that pays the scene off, leaving text on screen for a tenth
of a second. After setting boundaries, check each one against the phrase list in
`build/<sb>.timing.json`.

## The storyboard contract

`render.py`, `sfx.py` and `make.sh` need exactly this from `storyboards/<sb>.py`:

```python
NAME, AUDIO, PHRASES, TOTAL, FPS      # AUDIO relative to repo root
frame(t) -> PIL RGBA Image, 1080x1920
SFX = [{"t":…, "kind":"thump|swish|tick|sweep", "amp":…, …}]
```

Everything else is that file's own business. Both existing storyboards happen to
share a structure worth copying: a scene table `S=[(name,start,end),…]`, a
`scene_at(t)` dispatcher, one function per scene, plus `chrome`, `captions` and
`cut_sweep` applied to every frame.

Time is always **absolute seconds of the finished video**, and every visual beat is
keyed off a spoken word: `wstart("plan")`, `wend("improve.")`. Never hardcode a
number you could look up.

## Styling a new video

Change these, in this order:

- **Palette.** `aas.py` shows the pattern: define local colours, then
  `kit.set_palette(...)` so `kit`'s own defaults follow. One accent, one
  support colour, white, two greys, plus semantic green/red. Sample it from the
  cover art so the reel and its cover match — that pairing is what the platform
  search index reads.
- **Typeface.** `kit.set_fonts(display=…, mono=…, weight_index=…)`. Probe any new
  family first (see gotcha 3).
- **Ground.** Each storyboard's `build_base()` — two or three radial blooms, a
  vertical falloff so captions sit on darker ground, and grain. Keep the grain:
  it is what stops flat backgrounds banding after the platform re-encodes.
- **Motion.** Easing lives in `kit`: `eo3`/`eo4` (settle), `eob` (overshoot, for
  things that should land hard), `eio`, `pulse` (breathing). Entrances are
  ~0.30-0.40 s with a small upward slide; `enter()` returns `(alpha, dy)`.

## What makes these work

Retention rules that the existing two follow, worth keeping:

- **The hook is fully on screen at frame 0.** No logo intro, no fade up. ECC opens
  on three failure lines already legible; AAS opens mid-scroll.
- **Captions are OFF by default.** They are not burned in unless the brief asks
  for them (instruction, 22 Aug 2026). The machinery stays in `sbkit.captions`
  and `ledger.captions`; storyboards simply set `CAPTIONS = False` and guard the
  call, so one reel can turn them back on with a one-line flip.
  Two consequences worth knowing: word-synced captions used to be the single
  highest-leverage retention item here, because most Reels views start muted --
  that is the tradeoff being accepted. And removing them frees the y 1440-1600
  band, so scene content has to move down or it leaves a ~400px void; the Ledger
  storyboards use `CONTENT_DY = 150`, which shifts the scene layer only, leaving
  chrome and the end card where they are.
- **A cut every 2-4 seconds.** If a layout must hold longer, give it an event every
  1-2 seconds inside that hold.
- **One idea per screen**, and let information accumulate rather than replacing it.
- **Give the differentiating claim a physical beat.** ECC's loop visibly closes;
  AAS stamps NOTHING WRITTEN YET on the word "written". Pick the one line that
  matters and punctuate it.
- **End on a save prompt.** Saves and shares outrank likes in both rankers.
- **Every reel closes with "Follow for more."** — spoken as the last line of the
  narration *and* on the end card. The on-screen half lives in `sbkit.endcard()`,
  so any sbkit storyboard gets it free; `ecc.py` and `aas.py` inline their own end
  cards and need it added by hand if they are rebuilt. Put the line in the
  READ-THROUGH **before recording** — retrofitting means a re-record, because
  `phrases/<name>.txt` has to match the audio segment for segment.
- **Safe areas:** content inside `y 150-1600`, `x 84-996` — but **below `y 1000`
  the right edge is `x 932`, not 996.** Both platforms put their action-button
  column at `x 960-1080, y 1000-1700` (`safecheck.py`). `agenticseek.py` had three
  right-aligned labels at 966 — inside the documented rule and underneath the
  buttons. Card *edges* may pass under the column; text may not. Verify with
  `frames.sh`, don't assume.
- Sound design stays subtle — soft impact on cuts, tick on discrete beats.
  Narration is mixed in untouched and must stay dominant.

## Encode settings (do not drift)

1080x1920 · 30 fps · H.264 High @ 4.1 · yuv420p · CRF 18 · closed GOP every 2 s ·
bt709 tagged · AAC-LC 192 kbps 48 kHz stereo · `+faststart`.

Audio is normalised to **-14 LUFS / -1.5 dBTP** by `loudnorm`, which is what both
platforms normalise to — hit it and neither will turn the video down. The bt709
tags matter: untagged, both platforms render the colours washed out.

One file serves Reels (<=90 s) and Shorts (<=3 min). Keep runtime 30-45 s.

## Verifying

The renderer is not the deliverable. Always inspect the **encoded mp4**:

```bash
./verify.sh ../ECC-reel.mp4                       # streams, loudness, faststart
./frames.sh ../ECC-reel.mp4 6 150 400 700 1000 1300
```

`frames.sh` overlays both platforms' UI chrome (magenta = Reels, blue = Shorts) on
real frames, so you can see what the app will cover. Then open the contact sheet
and actually look at it. Every bug in the gotchas below was found by looking, and
none of them raised an error.

## Gotchas — all of these cost real time here

1. **`ImageDraw` does not alpha-blend; it replaces pixels.** Drawing a translucent
   full-frame scrim with `d.rectangle` erases everything already in that layer.
   Large translucent overlays must go through `alpha_composite`:
   ```python
   base.alpha_composite(Image.new("RGBA",(W,H),rgba(colour,a)))
   ```
   Small shapes drawn *before* their contents (card fills, pills) are fine.
2. **Z-order is layer order, not call order.** `grad_text`/`put_glow` composite into
   the image passed as their first argument; `d.*` draws into the overlay. Mixing
   the two silently puts gradients *behind* card fills. `frame()` passes the
   overlay `ov` to every scene so call order == z-order. Keep it that way.
3. **Helvetica Neue has no `✓ ✕ → ↓ ↻`** — they render as tofu boxes, no error.
   Use Menlo (`m()`) for symbols, or draw them with lines. Probe any new family:
   ```bash
   python3 -c "import kit; print(kit.probe_glyphs(kit.HN,1))"
   ```
4. **Never index the word list by a scene-local index.** An early version keyed a
   headline's word-by-word reveal to `WORDS[0..7]` — the first eight words of the
   *whole* narration — so it appeared all at once, 8 seconds early. Use
   `wstart("word")`, or `_T.index("word")` for a deliberate offset.
5. **One storyboard per process.** `kit.set_palette` mutates module globals, so
   importing two storyboards into one interpreter leaves the second one's palette
   applied to both. `render.py` imports one; keep any comparison scripts to
   separate processes.
6. **Tiled scrolling backgrounds:** modulo the offset into `[0, tile_height)` and
   composite two tiles. A tile taller than the frame is fine; a negative dest is
   not what you want. Check that highlighted rows actually land on screen —
   pick the freeze offset as a whole number of tiles and the arithmetic gets easy.
7. **`align.py` has a `SPOKEN` table** for initialisms, because the syllable
   counter reads "ECC" as one syllable when it is spoken as three. Add new
   initialisms there or their phrase drifts by ~200 ms.
8. `nb_frames` from ffprobe should be exactly `TOTAL * FPS`. If it isn't, the pipe
   broke mid-render and the tail of the video is missing. This is also what a
   killed render looks like: `make.sh` writes straight over the previous mp4, so
   an interrupted rebuild leaves a shorter, playable, wrong file. Render to a
   temp name and move it into place if the old file still matters.
   **Never background a render with `nohup ... &`.** It survives the harness
   tearing its shell down, so a "killed" render keeps writing while the rebuild
   you launch next writes to the same path. The result decoded 345 of 1545
   frames while its container still claimed 51.5 s, and it passed `verify.sh`
   outright — nothing in that script decoded video. `verify.sh` now decodes
   every frame and compares against the container duration.
9. **Storyboard module names contain hyphens**, so `import <name>` is a syntax
   error. `render.py` and `sfx.py` use `__import__(str)` and are fine; `make.sh`
   uses `importlib`. Any new helper that imports a storyboard must do the same.
10. **Reveal order must follow the narration, not the diagram's logic.** Ruflo's
   federation gates were keyed to the wire order (strip → sign → verify) while
   the script says "identity verified, personal data stripped" — so they lit out
   of sync with the words. Order visual beats by when they are *spoken*.
11. **A panel screenshot and the burned-in captions want the same pixels.**
   `PANEL_BOTTOM` was 1470 and the captions start at 1450, so *every*
   bottom-placed screenshot overlapped them by 20px. The floor is now
   caption-aware (1440 with captions, 1580 without) and `PANEL_MAX_H` came down
   to 1060 so even the tallest panel fits the band. Found by the upload preview
   built to warn about collisions — it reported this on the first screenshot
   anyone placed in it.
12. **Star counts and ages go stale.** Every claim on screen is a hostage. Re-check
   with `gh api repos/<owner>/<repo> --jq .stargazers_count` before recording,
   and keep the `ACCURACY NOTES` / `TIME-SENSITIVE` sections at the bottom of
   each script up to date — `deepseek-harness.txt` leads with "six days", which
   expires fast.
