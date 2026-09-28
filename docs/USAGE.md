# Usage

Three interfaces, one pipeline: the web UI, the CLI, and the HTTP API.

---

## The web UI

Open **http://localhost:8020/v2**.

| Page | What it is for |
|---|---|
| `/v2/` | Jobs and the queue. Reorder, pause, cancel; filter; active vs archived |
| `/v2/new` | Create a reel — source, content options, voice, screenshots |
| `/v2/job?id=…` | One job: stage rail, the artefact for each stage, live activity |
| `/v2/images?id=…` | Add or re-crop screenshots on a job that already exists |
| `/v2/settings` | Providers, model routing, API keys, approval gates |

A job page has a real URL, so it can be linked and reloaded.

The original single-file UI is still served at **`/`**. It is frozen and kept as a
fallback while the current one settles; both link to each other.

### Creating a reel

1. **Source** — a GitHub or Hugging Face URL. Optionally a slug and a template.
2. **Content** — captions on or off, fact validation on or off, target runtime.
3. **Voice** — synthesize, or upload your own recording.
4. **Screenshots** — optional, and labelled by role so the storyboard knows what
   each one is: *repo screenshot*, *output screenshot*, *app screenshot*.

Screenshots are cropped **at upload time**, against a live 9:16 frame showing the
real background, eyebrow and caption band. A collision with the burned-in
captions is therefore visible before the job is created rather than after a
render.

The frame constants come from `GET /api/images/frame`, served from Python, so the
preview cannot drift from the renderer.

---

## The queue

Reels queue and run **one at a time, end to end**. Roughly 76% of a run is the
two LLM stages, and two at once is two workloads against one GPU.

| Action | Effect |
|---|---|
| **Pause / Resume** | Stops the next job starting. The running one finishes |
| **Reorder** | Drag, or use the move controls |
| **Cancel** | Leaves the job, removes it from the line |
| **Retry a stage** | Joins the queue **at the front** rather than bypassing it |

Things worth knowing:

- **Cancel takes effect at a stage boundary**, never mid-stage.
- **Two consecutive failures pause the queue.** That is a broken model server, not
  two broken reels.
- **Queue state survives a restart.** It lives in each `job.json`, so it is
  rebuilt by scanning jobs. A job that was *running* when the process died leaves
  the queue rather than going back into it — re-queueing would send it straight
  back into the stage that just died, for ever.
- A review gate **releases the slot**; the job appears as blocked, not running.
- Under Celery the broker owns concurrency, and reorder/pause return `501`.

---

## Approval gates

By default a job pauses at `content` and `storyboard`. The first is what the
video says; the second is generated code. Both are cheaper to read than to
re-render.

At a gate you can edit the artefact in place — narration, storyboard source, the
phrase split — then approve. Editing a stage invalidates everything downstream,
so an approved script cannot leave a video rendered from the previous one.

Presets for all-manual and all-auto are in Settings; a per-job setting overrides
the saved default.

### Leaving a queue to run unattended

Generation is entirely server-side. The browser only watches: the progress
stream is one-way, nothing in the app is tied to a request, and closing the
tab, losing the network or shutting the laptop lid cannot affect a job that is
already running.

What *can* stop one:

- **The gates themselves.** This is the one that surprises people. A gated reel
  parks at `content` and **releases its place in the queue** so the next reel
  starts. Queue five reels on the default settings and you come back to five
  reels all waiting at the first gate, not five finished videos. For a batch
  you want finished, pick **Run unattended** on the New reel form, or set the
  all-auto preset in Settings.
- **The server process.** Without Redis the scheduler is a thread inside
  `uvicorn`, so that process *is* the worker: closing the terminal or letting
  the machine sleep stops everything. The Docker path runs the stages in
  separate `worker` and `renderer` containers, which is what survives.
- **`--reload`.** The development command restarts on any file save, and that
  kills the stage in flight. Do not use it while a queue is draining.
- **A restart costs exactly one reel.** The stage that was running is marked
  failed with an "Interrupted" note and that job leaves the queue — deliberately,
  because putting it back would send it straight into the stage that just died.
  Everything still queued resumes on its own.
- **Two consecutive failures pause the whole queue.** That pattern usually means
  a broken model server rather than two bad reels, so nothing further runs until
  you press Resume.

---

## Deliverable size: 1080p, 2K and 4K

The pipeline renders and verifies one file, **1080x1920**, which is what
Instagram Reels, YouTube Shorts and Facebook Reels all want. That file is the
master and the 18 platform checks are about it.

On the job page, the **render** pane also offers 2K (1440x2560) and 4K
(2160x3840). These are Lanczos upscales of the finished reel, written beside it
as `out/<slug>-reel-2k.mp4` and `-4k.mp4`:

```bash
curl -X POST localhost:8020/api/jobs/<id>/upscale \
     -H 'Content-Type: application/json' -d '{"resolution":"4k"}'
curl localhost:8020/api/jobs/<id>/upscales          # what exists, what could
```

Two things worth knowing before you use them. **They add no detail** — nothing
here invents pixels, so a 4K upload is the same picture in a bigger frame; the
reason to want one is that some platforms give a larger upload a more generous
bitrate ladder. And **the audio is stream-copied, never re-encoded**, because it
was normalised to -14 LUFS / -1.5 dBTP and verified there.

The upscale re-encodes video at `veryfast` rather than the master's `slow`:
resampled frames carry no detail a longer search could find, and on a 40-second
reel the difference is 70 seconds versus not finishing in two minutes. Expect
roughly 30 s for 2K and 70 s for 4K. They are tagged H.264 level 5.1, not the
master's 4.1, which tops out at 1080p.

An upscale made after `package` has already run is not in `bundle.zip`; re-run
`package` if you want it there.

---

## The CLI

```bash
python -m app.cli <command>
```

| Command | What it does |
|---|---|
| `new <url>` | Create a job |
| `run <job>` | Run a job, or one stage of it |
| `approve <job> <stage>` | Accept a stage awaiting review |
| `list` | List jobs |
| `show <job>` | Print a job's full state |
| `providers` | What is reachable right now |
| `doctor` | Check the environment |

### `new`

```bash
python -m app.cli new https://github.com/owner/repo \
  --slug my-reel \
  --template research \
  --llm my-vllm \
  --tts elevenlabs \
  --no-gates \
  --run
```

| Flag | |
|---|---|
| `--slug` | Override the derived name |
| `--template` | `cool-indigo` · `warm-amber` · `editorial` · `research` · `safe-deterministic` |
| `--llm` / `--tts` | Use a named profile for this job |
| `--no-gates` | Run straight through without pausing for review |
| `--run` | Start immediately |

### `run`

```bash
python -m app.cli run <job-id>                      # to the next gate
python -m app.cli run <job-id> --stage content      # exactly one stage
python -m app.cli run <job-id> --until storyboard   # stop after this stage
```

Stages: `ingest` `content` `cover` `audio` `align` `storyboard` `render`
`verify` `package`.

### Everything else

```bash
python -m app.cli list --limit 20
python -m app.cli show <job-id>
python -m app.cli providers
python -m app.cli doctor
```

The CLI runs stages synchronously and does not touch the scheduler, so it works
the same inside the container.

---

## The HTTP API

Interactive docs at **http://localhost:8020/docs**.

### Jobs

| | |
|---|---|
| `GET` `/api/jobs` | List. `GET /api/jobs/counts` for the summary |
| `POST` `/api/jobs` | Create |
| `GET` `PATCH` `DELETE` `/api/jobs/{id}` | Read, update, remove |
| `POST` `/api/jobs/{id}/run` | Run the pipeline, or one stage |
| `POST` `/api/jobs/{id}/stages/{stage}/approve` | Accept a gated stage |
| `POST` `/api/jobs/{id}/stages/{stage}/retry` | Re-run a stage |
| `POST` `/api/jobs/{id}/archive` · `/unarchive` | |
| `GET` `/api/jobs/{id}/events` | Server-sent progress events |

### Artefacts

| | |
|---|---|
| `GET` `PUT` `/api/jobs/{id}/content` | The script and platform copy |
| `GET` `PUT` `/api/jobs/{id}/storyboard` | The storyboard source |
| `GET` `/api/jobs/{id}/alignment` · `PUT` `/phrases` | Word timing, phrase split |
| `POST` `/api/jobs/{id}/audio` | Upload narration |
| `POST` `PATCH` `DELETE` `/api/jobs/{id}/images…` | Screenshots and crops |
| `GET` `/api/jobs/{id}/upscales` | Which sizes exist, and which could be made |
| `POST` `/api/jobs/{id}/upscale` | Scale the reel to `2k` or `4k` beside the master |
| `GET` `/api/jobs/{id}/artifacts/{path}` | Any file in the job directory |

### Queue

| | |
|---|---|
| `GET` `/api/queue` | Position, state and ETA for everything |
| `POST` `/api/queue/pause` · `/resume` | |
| `PUT` `/api/queue/order` · `POST` `/api/queue/{id}/move` | Reorder |
| `POST` `DELETE` `/api/queue/{id}` | Enqueue, cancel |

### System and settings

| | |
|---|---|
| `GET` `/api/health` `/api/stages` `/api/templates` | |
| `GET` `/api/config/profiles` | Provider profiles, no network probe |
| `GET` `/api/config/providers` | Live reachability and key balances |
| `GET` `/api/images/frame` | Reel frame geometry, for the upload preview |
| `GET` `/api/settings` · `PUT` `/api/settings/…` | See [CONFIGURATION.md](CONFIGURATION.md) |

`/api/config/profiles` is the probe-free one. Reading `/api/config/providers` on
page load means waiting for live network probes — that is what once turned a form
load into 16 seconds.

---

## Where the output goes

```
data/jobs/<id>/
  job.json               state, queue position, stage records
  facts.json             what ingest found
  content.json           script, phrases, fact sheet, cover spec, platform copy
  <slug>.txt             the script, in the hand-written format
  <slug>-reel.png        cover art
  <slug>.mp3             narration
  <slug>-reel.mp4        the reel
  verify.json            18 assertions
  contact.png            frames with both platforms' UI overlaid
  build/                 timing, mixes, logs
  out/  bundle.zip       the delivery bundle
```

Open `contact.png` before publishing. It overlays the Instagram and YouTube
action-button columns on real frames, so you can see what the app will cover.

---

## Troubleshooting

**A stage says "Interrupted: the process running this stage stopped".**
The server restarted mid-stage. Retry that stage; nothing downstream was written.

**The queue is not moving.**
Check whether it is paused — two consecutive failures pause it automatically.
`GET /api/queue` reports `paused` and the reason.

**"the encoded file does not meet platform requirements: true peak".**
AAC overshoots true peak after `loudnorm` has already hit its target. The
loudness chain measures the encoded file and corrects, but a recording with a
very high crest factor may still land quiet. Raising the requested loudness makes
it *quieter* — the tighter peak target makes the limiter work harder. The fix for
a genuinely quiet recording is a re-record, not a filter. See
[DEVELOPMENT.md](../DEVELOPMENT.md).

**The narration does not match the phrase list.**
`align.py` refuses to run unless the counts match, and prints a side-by-side
diff. The UI shows the detected segments beside your phrase lines so you can
split or merge. Every phrase break must land on a real clause boundary — if one
falls mid-clause the split is wrong, and lowering the silence threshold will not
fix it.

**Storyboard generation keeps failing.**
Each failure becomes the repair prompt, and after the repair budget is spent the
job falls back to `safe-deterministic` and still produces a video. If it fails
early and often, check `json_mode` — a reasoning model must not use
`json_schema`. See [CONFIGURATION.md](CONFIGURATION.md).

**Symbols render as empty boxes.**
The mono font lacks those glyphs. Use DejaVu Sans Mono, not JetBrains Mono.
`python -m app.cli doctor` checks this.

**Two schedulers, or odd queue behaviour.**
You started `uvicorn` with more than one worker. Use `--workers 1`.
