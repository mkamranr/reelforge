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
