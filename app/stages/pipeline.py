"""The stage runner: what each pipeline step does to a job.

One function per stage, all with the same shape -- take a job, do the work,
write artefacts into the job directory, record what happened. The Celery tasks
and the API both call these, so there is one definition of what a stage is.

Re-running a stage invalidates everything after it. Editing the script must not
leave a video rendered from the previous one.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

from app.config import get_config
from app.models.content import ReelContent
from app.models.facts import FactsBundle
from app.models.job import STAGE_ORDER, Job, Stage, Status
from app.models.platform import PlatformBundle
from app.store import JobStore, atomic_write

Progress = Callable[[str], None]


class StageFailed(RuntimeError):
    pass


def _ensure_workspace(paths) -> None:
    """Build (or repair) this job's symlink farm over `video/`.

    Idempotent, and called by every stage that touches the renderer rather than
    once at job creation: a job directory can be copied, restored from a backup
    or resumed after the `video/` path moved, and a farm of stale symlinks is
    worse than no farm at all.
    """
    from app.render.workspace import build_workspace

    build_workspace(paths.workspace)


def _note(job: Job, progress: Progress | None, message: str) -> None:
    job.note(message)
    if progress:
        progress(message)


# ------------------------------------------------------------- 1. ingest ---
def run_ingest(job: Job, store: JobStore, progress: Progress | None = None) -> dict:
    from app.ingest import ingest

    paths = store.paths(job)
    uploaded = None
    if job.source.uploaded_markdown_name:
        candidate = paths.uploads / job.source.uploaded_markdown_name
        uploaded = candidate if candidate.exists() else None

    _note(job, progress, f"fetching facts for {job.source.url or 'the uploaded file'}")
    facts = ingest(job.source.url, markdown_path=uploaded, slug=job.slug)
    atomic_write(paths.facts_json, facts.model_dump_json(indent=2))

    summary = {"slug": facts.slug, "fetched_at": facts.fetched_at.isoformat(),
               "sources": [s.api for s in facts.sources],
               "readme_chars": len(facts.readme_markdown or ""),
               "install_commands": facts.install_commands[:6]}
    if facts.github:
        summary |= {"stars": facts.github.stars, "forks": facts.github.forks,
                    "licence": facts.github.licence, "age_days": facts.github.age_days}
    if facts.huggingface:
        summary |= {"downloads": facts.huggingface.downloads,
                    "likes": facts.huggingface.likes,
                    "licence": facts.huggingface.licence}
    _note(job, progress, f"facts fetched from {', '.join(summary['sources']) or 'upload'}")
    return {"artifacts": {"facts": paths.rel(paths.facts_json)}, "meta": summary}


# ------------------------------------------------------------ 2. content ---
def run_content(job: Job, store: JobStore, progress: Progress | None = None) -> dict:
    from app.providers.llm import build_llm
    from app.render import documents
    from app.stages.content import fact_report, generate_content, generate_platforms
    from app.templates import load_template

    cfg = get_config()
    paths = store.paths(job)
    facts = FactsBundle.model_validate_json(paths.facts_json.read_text(encoding="utf-8"))
    template = load_template(job.template)

    overrides = {"provider": job.providers.llm_provider, "model": job.providers.llm_model}
    llm = build_llm("content", cfg, overrides)
    _note(job, progress, f"writing the script with {llm.name}/{llm.model}")

    content_out = generate_content(
        facts, llm, template_hint=template.tone_hint,
        max_attempts=cfg.storyboard.max_repair_attempts,
        fact_check=job.fact_check, progress=progress,
    )
    content: ReelContent = content_out.value

    facts_report = fact_report(content, facts)
    if not facts_report["ok"]:
        _note(job, progress,
              f"{len(facts_report['unsourced'])} number(s) do not trace to the "
              f"facts block or the project's README -- check them on the gate")

    _note(job, progress, "writing the per-platform copy")
    platform_llm = build_llm("content", cfg, overrides)
    platforms_out = generate_platforms(
        content, facts, platform_llm,
        max_attempts=cfg.storyboard.max_repair_attempts, progress=progress,
    )
    platforms: PlatformBundle = platforms_out.value

    atomic_write(paths.content_json, content.model_dump_json(indent=2))
    atomic_write(paths.platforms_json, platforms.model_dump_json(indent=2))

    _ensure_workspace(paths)
    paths.out.mkdir(parents=True, exist_ok=True)
    atomic_write(paths.script_txt, documents.script_txt(content, facts))
    atomic_write(paths.out / "captions.txt",
                 documents.caption_block(content, platforms))
    paths.phrases.mkdir(parents=True, exist_ok=True)
    atomic_write(paths.phrases_txt, documents.phrases_txt(content))

    metadata = documents.metadata_files(content, platforms)
    paths.metadata.mkdir(parents=True, exist_ok=True)
    for name, body in metadata.items():
        atomic_write(paths.metadata / name, body)

    _note(job, progress,
          f"script: {content.word_count} words, {len(content.phrases)} phrases, "
          f"~{content.estimated_seconds():.0f}s")
    return {
        "artifacts": {
            "content": paths.rel(paths.content_json),
            "platforms": paths.rel(paths.platforms_json),
            "script": paths.rel(paths.script_txt),
            "phrases": paths.rel(paths.phrases_txt),
        },
        "meta": {
            "words": content.word_count, "phrases": len(content.phrases),
            "estimated_seconds": content.estimated_seconds(),
            "hashtags": content.hashtags,
            "content_generation": content_out.as_dict(),
            "platform_generation": platforms_out.as_dict(),
            "youtube_title": platforms.youtube.title,
            "facts": facts_report,
            "fact_check": job.fact_check,
        },
    }


# -------------------------------------------------------------- 3. cover ---
def run_cover(job: Job, store: JobStore, progress: Progress | None = None) -> dict:
    from app.render.cover import render_cover

    cfg = get_config()
    paths = store.paths(job)
    _ensure_workspace(paths)
    content = ReelContent.model_validate_json(paths.content_json.read_text(encoding="utf-8"))

    # A generated backdrop under the typography, when visuals are on. Fail
    # soft: the cover is the one artefact every delivery needs, and a GPU box
    # that is down is no reason to ship without one.
    backdrop = None
    backdrop_note = ""
    backdrop_credit: dict = {}
    settings = visuals_settings(job)
    if settings["enabled"] and settings["cover"]:
        from app.providers.visuals import build_visuals
        from app.stages.visuals import cover_prompt, cover_query, seed_for

        prompt = cover_prompt(content, cfg.visuals.style)
        try:
            provider = build_visuals(cfg, {"profile": settings["profile"]})
            _note(job, progress, f"generating the cover backdrop with {provider.name}")
            from app.stages.visuals import TEXT_NEGATIVE

            result = provider.still(prompt, paths.cover_backdrop, width=1080, height=1920,
                                    seed=seed_for(job.id, "cover", 0, prompt),
                                    negative=", ".join(x for x in (TEXT_NEGATIVE, cfg.visuals.negative) if x),
                                    query=cover_query(content),
                                    progress=lambda m: _note(job, progress, m))
            backdrop = result.path
            backdrop_note = prompt
            # The cover stage writes no visuals.json, so the one place the
            # backdrop's attribution can live is this stage's own meta. The
            # package stage reads it back to build the credits.
            backdrop_credit = dict(result.meta or {})
            provider.release()
        except Exception as exc:
            _note(job, progress, f"cover backdrop skipped: {str(exc)[:200]}")
            backdrop_note = f"failed: {str(exc)[:200]}"

    _note(job, progress, "rendering the cover")
    out, fit_problems = render_cover(content, paths.workspace, paths.cover_png,
                                     backdrop=backdrop)
    if fit_problems:
        _note(job, progress,
              f"{len(fit_problems)} cover string(s) are clipped: "
              + ", ".join(p["label"] for p in fit_problems))
    artifacts = {"cover": paths.rel(out)}
    if backdrop:
        artifacts["backdrop"] = paths.rel(backdrop)
    return {"artifacts": artifacts,
            "meta": {"motif": content.cover.motif, "bytes": out.stat().st_size,
                     "fit_problems": fit_problems,
                     "backdrop": bool(backdrop), "backdrop_prompt": backdrop_note,
                     "backdrop_credit": backdrop_credit}}


# -------------------------------------------------------------- 4. audio ---
def run_audio(job: Job, store: JobStore, progress: Progress | None = None) -> dict:
    from app.providers.tts import UploadRequired, build_tts, join

    cfg = get_config()
    paths = store.paths(job)
    content = ReelContent.model_validate_json(paths.content_json.read_text(encoding="utf-8"))

    provider_name = job.providers.tts_provider or cfg.tts.active
    adapter, _ = cfg.tts.for_profile(provider_name)
    if adapter == "upload":
        if not paths.audio_mp3.exists():
            raise StageFailed(
                "the audio provider is 'upload' but no narration file has been "
                "supplied. Upload one, or choose a TTS provider."
            )
        _note(job, progress, f"using the uploaded narration ({paths.audio_mp3.name})")
        info = {"provider": "upload", "mp3": str(paths.audio_mp3)}
    else:
        engine = build_tts(cfg, {"provider": provider_name,
                                 "voice": job.providers.tts_voice})
        estimate = getattr(engine, "estimate", lambda _t: {})(content.phrase_lines())
        # a GPU voice shares the card with the image server, whose models stay
        # resident from whatever ran last -- any earlier job, not just this one's
        # visuals stage. Hand the VRAM back before asking the voice for room.
        settings = visuals_settings(job)
        if settings["enabled"]:
            from app.providers.visuals import build_visuals

            try:
                if build_visuals(cfg, {"profile": settings["profile"]}).release():
                    _note(job, progress, "image server models unloaded before synthesis")
            except Exception:
                pass
        _note(job, progress,
              f"synthesizing {len(content.phrases)} phrases with {provider_name}"
              + (f" ({estimate.get('characters')} characters)" if estimate else ""))
        try:
            spoken = engine.speak_all(
                content.phrases, hints=content.audio.pronunciations,
                progress=lambda frac, msg: progress(msg) if progress else None,
            )
        except UploadRequired as exc:
            raise StageFailed(str(exc)) from None
        info = join(spoken.phrases, content.phrases, paths.audio_mp3,
                    scratch=paths.audio_parts)
        info["provider"] = provider_name
        info["voice"] = spoken.voice
        rotations = getattr(engine, "rotations", [])
        if rotations:
            info["key_rotations"] = rotations
            _note(job, progress, f"rotated API key {len(rotations)} time(s)")

    from app.render.workspace import ffprobe_bin
    import subprocess

    proc = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(paths.audio_mp3)],
        capture_output=True, text=True, check=False)
    duration = float(proc.stdout.strip() or 0.0)
    info["duration_seconds"] = round(duration, 3)
    _note(job, progress, f"narration is {duration:.2f}s")

    # The finished video is the narration plus a beat, and verify holds it to
    # the platform window. Every fact needed to know that is available here, so
    # checking it now costs a second instead of a full render -- one job spent
    # 90s rendering 773 frames to be told the reel was 25.77s.
    from app.stages.content import reel_total

    low, high = cfg.content.target_seconds_min, cfg.content.target_seconds_max
    runtime = reel_total(duration, low)
    info["reel_seconds"] = round(runtime, 3)
    if runtime > duration + 1.5:
        _note(job, progress,
              f"holding the end card for {runtime - duration:.1f}s so the reel "
              f"reaches the {low:.0f}s floor")

    # The reel is as long as the narration plus its end card -- the storyboard
    # paces its screens to whatever the total is. The 36-44s target is script
    # guidance, not a gate: outside it, say so and carry on. What does fail
    # here is what verify would fail after a full render anyway: the platform
    # hard limits (Reels caps at 90s; verify holds 30-90).
    if not VERIFY_MIN_SECONDS <= runtime <= VERIFY_MAX_SECONDS:
        raise StageFailed(
            f"the narration is {duration:.2f}s, which makes a {runtime:.2f}s reel -- "
            f"outside the {VERIFY_MIN_SECONDS:.0f}-{VERIFY_MAX_SECONDS:.0f}s the "
            f"platforms accept, and verify will reject it after the render.\n"
            + ("Re-run content asking for a shorter script, or trim the recording."
               if runtime > VERIFY_MAX_SECONDS else
               "Even holding the end card for the maximum, this is short. Re-run "
               "content asking for a longer script, or upload a longer recording.")
        )
    if not low <= runtime <= high:
        _note(job, progress,
              f"the reel will run {runtime:.1f}s -- outside the {low:.0f}-{high:.0f}s "
              f"retention target, which is fine, just worth knowing")

    return {"artifacts": {"audio": paths.rel(paths.audio_mp3)}, "meta": info}


# -------------------------------------------------------------- 5. align ---
def run_align(job: Job, store: JobStore, progress: Progress | None = None) -> dict:
    from app.stages.align import align_job

    _ensure_workspace(store.paths(job))
    return align_job(job, store, progress)


#: What `verify` holds a finished file to (see app/render/verify.py): the real
#: platform bounds, not the retention target.
VERIFY_MIN_SECONDS = 30.0
VERIFY_MAX_SECONDS = 90.0


# ------------------------------------------------------------ 4. visuals ---
def visuals_settings(job: Job) -> dict:
    """The job's say over the configured defaults, resolved."""
    cfg = get_config()
    choice = job.visuals
    profile = choice.profile or cfg.visuals.active
    adapter = cfg.visuals.profiles.get(profile, cfg.visuals.profiles["none"]).adapter
    enabled = choice.enabled if choice.enabled is not None else adapter != "none"
    if adapter == "none":
        enabled = False
    return {
        "enabled": enabled,
        "profile": profile,
        "stills": choice.stills if choice.stills is not None else cfg.visuals.stills,
        "clips": choice.clips if choice.clips is not None else cfg.visuals.clips,
        "cover": choice.cover if choice.cover is not None else cfg.visuals.cover,
        "music": choice.music if choice.music is not None else cfg.visuals.music,
        "sfx_samples": cfg.visuals.sfx_samples,
    }


def run_visuals(job: Job, store: JobStore, progress: Progress | None = None) -> dict:
    from app.providers.visuals import build_visuals
    from app.stages import visuals as V

    cfg = get_config()
    paths = store.paths(job)
    settings = visuals_settings(job)
    if not settings["enabled"]:
        V.write_assets(paths.visuals_json, [], provider="none")
        return {"artifacts": {"visuals": paths.rel(paths.visuals_json)},
                "meta": {"enabled": False, "stills": 0, "clips": 0}}

    content = ReelContent.model_validate_json(paths.content_json.read_text(encoding="utf-8"))
    provider = build_visuals(cfg, {"profile": settings["profile"]})
    clips = settings["clips"] if provider.supports_clips else 0
    if settings["clips"] and not provider.supports_clips:
        _note(job, progress, f"{provider.name} has no video workflow; stills only")
    music_hint = None
    if settings["music"] and provider.supports_audio:
        from app.templates import load_template

        music_hint = load_template(job.template).music
    elif settings["music"]:
        _note(job, progress, f"{provider.name} has no audio workflow; no music bed")

    sfx_problems: dict = {}
    if settings["sfx_samples"] and provider.supports_audio:
        sfx_problems = V.ensure_sfx_library(provider, sfx_library_dir(settings["profile"]),
                                            progress=lambda m: _note(job, progress, m))

    # Text-free shot descriptions from the language model, so the picture
    # carries the idea and the renderer carries the words. Every body scene
    # that could get a picture is described once; the plan picks among them.
    directions: dict = {}
    if cfg.visuals.art_director and (settings["stills"] or clips):
        from app.providers.llm import build_llm

        candidates = [sc.index for sc in V.body_scenes(content)]
        try:
            llm = build_llm("review", cfg, {"provider": job.providers.llm_provider})
            directions = V.art_direct(content, candidates, llm,
                                      progress=lambda m: _note(job, progress, m))
        except Exception as exc:
            _note(job, progress, f"art direction unavailable ({str(exc)[:120]}); "
                                 "prompts cleaned by rule instead")

    assets = V.plan(content, job_id=job.id, stills=settings["stills"], clips=clips,
                    style=cfg.visuals.style, still_fit=cfg.visuals.still_fit,
                    music=music_hint, directions=directions)
    if not assets:
        V.write_assets(paths.visuals_json, [], provider=provider.name)
        return {"artifacts": {"visuals": paths.rel(paths.visuals_json)},
                "meta": {"enabled": True, "stills": 0, "clips": 0,
                         "note": "nothing to generate for this script"}}

    earlier = {f"{a.kind}-{a.index}": a for a in V.read_assets(paths.visuals_json)}
    _note(job, progress, f"{provider.name}: {sum(a.kind == 'still' for a in assets)} still(s), "
                         f"{sum(a.kind == 'clip' for a in assets)} clip(s)"
                         + (", a music bed" if music_hint else ""))
    _, provider_settings = cfg.visuals.for_profile(settings["profile"])
    still_size = (int(provider_settings.get("image_width") or 1152),
                  int(provider_settings.get("image_height") or 1536))
    negative = ", ".join(x for x in (V.TEXT_NEGATIVE, cfg.visuals.negative) if x)
    V.generate(assets, provider, job_root=paths.root, visuals_dir=paths.visuals_dir,
               prepared_dir=paths.prepared_images, still_size=still_size,
               clip_seconds=cfg.visuals.clip_seconds, negative=negative,
               progress=lambda m: _note(job, progress, m), reuse=earlier)
    V.write_assets(paths.visuals_json, assets, provider=provider.name,
                   extra={"profile": settings["profile"], "style": cfg.visuals.style})
    # the GPU box is shared: hand the VRAM back before the TTS needs it
    if provider.release():
        _note(job, progress, f"{provider.name}: models unloaded, VRAM released")

    done = [a for a in assets if a.ok]
    failed = [a for a in assets if not a.ok]
    if not done:
        raise StageFailed("every generated visual failed:\n"
                          + "\n".join(f"  - {a.kind} {a.index}: {a.error}" for a in failed))
    artifacts = {"visuals": paths.rel(paths.visuals_json)}
    for asset in done:
        artifacts[f"{asset.kind}-{asset.index}"] = asset.source or asset.file
    problems = [{"asset": f"{a.kind}-{a.index}", "error": a.error} for a in failed]
    problems += [{"asset": f"sfx-{kind}", "error": err} for kind, err in sfx_problems.items()]
    return {"artifacts": artifacts,
            "meta": {"enabled": True, "provider": provider.name,
                     "stills": sum(a.kind == "still" for a in done),
                     "clips": sum(a.kind == "clip" for a in done),
                     "music": any(a.kind == "music" for a in done),
                     "sfx_samples": bool(settings["sfx_samples"] and provider.supports_audio),
                     "problems": problems}}


def sfx_library_dir(profile: str) -> Path:
    """Generated one-shots are per profile, not per job: the same twelve
    sounds serve every reel, and a different server may draw them differently."""
    return get_config().paths.data / "sfx" / profile


def render_audio(job: Job, paths) -> dict:
    """What the mixer gets beyond the narration: the bed and the sample set."""
    from app.stages import visuals as V

    settings = visuals_settings(job)
    out: dict = {}
    if not settings["enabled"]:
        return out
    cfg = get_config()
    assets = V.read_assets(paths.visuals_json)
    bed = V.music_for_render(assets, paths.root)
    if bed and settings["music"]:
        out.update({"music": bed, "music_gain_db": cfg.visuals.music_gain_db,
                    "music_duck_db": cfg.visuals.music_duck_db})
    if settings["sfx_samples"]:
        library = sfx_library_dir(settings["profile"])
        if library.is_dir() and any(library.glob("*.wav")):
            out["sfx_dir"] = library
    return out


def stage_clips(job: Job, paths) -> list[dict]:
    """Generated clips, their frames copied beside the storyboard like stills."""
    from app.stages import visuals as V

    clips = V.clips_for_storyboard(V.read_assets(paths.visuals_json), paths.root)
    if not clips:
        return []
    destination = paths.workspace / "images"
    out = []
    for clip in clips:
        source: Path = clip.pop("path")
        target = destination / clip["dir"]
        stamp = target / ".copied"
        if not stamp.exists() or stamp.stat().st_mtime < source.stat().st_mtime:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(source, target)
            stamp.touch()
        out.append(clip)
    return out


def stage_images(job: Job, paths) -> list[dict]:
    """Prepared screenshots, copied where the storyboard can open them.

    The storyboard resolves paths from its own `__file__`, which the symlink
    farm puts inside the job directory -- so the bitmaps go beside it rather
    than being referenced from anywhere else, and a chunk process running with
    a different cwd still finds them.
    """
    import shutil

    from app.images import role_eyebrow, role_order

    from app.stages import visuals as V

    generated = V.stills_for_storyboard(V.read_assets(paths.visuals_json), paths.root)
    if not job.images and not generated:
        return []

    # the storyboard sits in workspace/storyboards/, and resolves ../images
    destination = paths.workspace / "images"
    destination.mkdir(parents=True, exist_ok=True)

    out = []
    for still in generated:
        source: Path = still.pop("path")
        target = destination / source.name
        if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
            shutil.copy2(source, target)
        out.append(still)
    for image in sorted(job.images, key=lambda i: (role_order(i.role), i.id)):
        prepared = paths.root / image.prepared if image.prepared else None
        if not prepared or not prepared.exists():
            continue
        target = destination / prepared.name
        if not target.exists() or target.stat().st_mtime < prepared.stat().st_mtime:
            shutil.copy2(prepared, target)
        out.append({
            "file": prepared.name, "fit": image.fit, "role": image.role,
            "position": image.position,
            "eyebrow": role_eyebrow(image.role), "caption": image.caption,
            "width": image.prepared_width, "height": image.prepared_height,
        })
    return out


# --------------------------------------------------------- 6. storyboard ---
def run_storyboard(job: Job, store: JobStore, progress: Progress | None = None) -> dict:
    from app.providers.llm import build_llm
    from app.stages.storyboard import generate_storyboard, write_fallback
    from app.templates import load_template

    cfg = get_config()
    paths = store.paths(job)
    _ensure_workspace(paths)
    content = ReelContent.model_validate_json(paths.content_json.read_text(encoding="utf-8"))
    timing = json.loads(paths.timing_json.read_text(encoding="utf-8"))
    from app.stages.content import reel_total

    total = round(reel_total(float(timing["duration"]),
                             cfg.content.target_seconds_min), 2)
    template = load_template(job.template)

    if template.deterministic:
        _note(job, progress, f"template {template.name!r} renders from data, no codegen")
        write_fallback(content, paths.workspace, total,
                       f"{content.slug}.mp3", f"phrases/{content.slug}.txt",
                       captions=job.captions, timing_json=paths.timing_json,
                       images=stage_images(job, paths), clips=stage_clips(job, paths),
                       family=template.family, repo_url=content.repo_url)
        from app.stages.storyboard import run_smoke

        smoke = run_smoke(paths.workspace, content.slug, paths.smoke_dir,
                          expected_duration=total)
        if not smoke.get("ok"):
            raise StageFailed("the deterministic template failed to render:\n" +
                              "\n".join(p["message"] for p in smoke.get("problems", [])))
        return {"artifacts": {"storyboard": paths.rel(paths.storyboard_py)},
                "meta": {"used_fallback": True, "total": total,
                         "frames": [f["path"] for f in smoke.get("frames", [])]}}

    overrides = {"provider": job.providers.llm_provider, "model": job.providers.llm_model}
    llm = build_llm("storyboard", cfg, overrides)
    vision = build_llm("review", cfg) if cfg.storyboard.vision_review else None
    _note(job, progress, f"writing the storyboard with {llm.name}/{llm.model}")

    result = generate_storyboard(
        content, paths.workspace, paths.timing_json, llm=llm, total=total,
        audio_rel=f"{content.slug}.mp3", phrases_rel=f"phrases/{content.slug}.txt",
        smoke_dir=paths.smoke_dir, template_example=template.example_source,
        vision_llm=vision, captions=job.captions,
        images=stage_images(job, paths), clips=stage_clips(job, paths),
        progress=progress,
    )
    meta = result.as_dict() | {"total": total}
    _note(job, progress,
          f"storyboard ready after {len(result.attempts)} attempt(s)"
          + (" (fallback)" if result.used_fallback else ""))
    return {"artifacts": {"storyboard": paths.rel(result.path)}, "meta": meta}


# ------------------------------------------------------------- 7. render ---
def run_render(job: Job, store: JobStore, progress: Progress | None = None) -> dict:
    from app.render.chunked import render

    paths = store.paths(job)
    _ensure_workspace(paths)
    total = float(job.state(Stage.STORYBOARD).meta.get("total") or 0.0)
    if not total:
        from app.stages.content import reel_total

        timing = json.loads(paths.timing_json.read_text(encoding="utf-8"))
        total = round(reel_total(float(timing["duration"]),
                                 get_config().content.target_seconds_min), 2)

    audio = render_audio(job, paths)
    if audio.get("music"):
        _note(job, progress, f"mixing the music bed at {audio['music_gain_db']} dB, "
                             f"ducked {audio['music_duck_db']} dB under the voice")
    if audio.get("sfx_dir"):
        _note(job, progress, "cut sounds from the generated sample library")
    _note(job, progress, f"rendering {total:.2f}s")
    result = render(
        paths.workspace, job.slug, total, paths.reel_mp4,
        progress=lambda tag, frac, msg: progress(f"{tag} {frac * 100:.0f}%")
        if progress else None,
        audio=audio or None,
    )
    from app.render.chunked import LAST_MIX_REPORT

    mix_report = {k: v for k, v in LAST_MIX_REPORT.items() if k in ("music", "sfx_samples")}
    _note(job, progress,
          f"rendered {result.frames} frames in {result.seconds}s "
          f"({result.mode}, {result.chunks} chunk(s))")
    if result.loudness:
        _note(job, progress,
              f"audio at {result.loudness['loudness']:.1f} LUFS / "
              f"{result.loudness['true_peak']:.1f} dBTP after "
              f"{len(result.loudness['passes'])} normalisation pass(es)")
    return {"artifacts": {"video": paths.rel(paths.reel_mp4)},
            "meta": {"frames": result.frames, "expected_frames": result.expected_frames,
                     "seconds": result.seconds, "mode": result.mode,
                     "chunks": result.chunks, "total": total,
                     "loudness": result.loudness, **mix_report}}


# ------------------------------------------------------------- 8. verify ---
def run_verify(job: Job, store: JobStore, progress: Progress | None = None) -> dict:
    from app.render.verify import contact_sheet, verify

    paths = store.paths(job)
    meta = job.state(Stage.RENDER).meta
    expected_frames = meta.get("expected_frames")
    total = meta.get("total")

    _note(job, progress, "verifying the encoded file")
    report = verify(paths.reel_mp4, expected_frames=expected_frames,
                    expected_duration=total)
    atomic_write(paths.verify_json, json.dumps(report.as_dict(), indent=2))

    frames = [6, int((expected_frames or 1164) * 0.2), int((expected_frames or 1164) * 0.4),
              int((expected_frames or 1164) * 0.6), int((expected_frames or 1164) * 0.8),
              max(0, (expected_frames or 1164) - 14)]
    try:
        contact_sheet(paths.reel_mp4, paths.contact_png, frames)
    except Exception as exc:
        _note(job, progress, f"contact sheet could not be built: {exc}")

    if not report.ok:
        raise StageFailed(
            "the encoded file does not meet platform requirements:\n"
            + "\n".join(f"  {c.name}: expected {c.expected}, got {c.actual}"
                        for c in report.failures)
        )
    _note(job, progress, f"verified: {len(report.checks)} checks passed")
    return {"artifacts": {"verify": paths.rel(paths.verify_json),
                          "contact": paths.rel(paths.contact_png)},
            "meta": {"checks": len(report.checks), "loudness": report.loudness,
                     "ok": True}}


# ------------------------------------------------------------ 9. package ---
def run_package(job: Job, store: JobStore, progress: Progress | None = None) -> dict:
    import zipfile

    from app.render import documents

    paths = store.paths(job)
    content = ReelContent.model_validate_json(paths.content_json.read_text(encoding="utf-8"))
    platforms = PlatformBundle.model_validate_json(paths.platforms_json.read_text(encoding="utf-8"))
    facts = FactsBundle.model_validate_json(paths.facts_json.read_text(encoding="utf-8"))
    report = json.loads(paths.verify_json.read_text(encoding="utf-8")) if paths.verify_json.exists() else {}

    streams = report.get("streams", {}) or {}
    video = streams.get("video") or {}
    spec = {
        "duration_seconds": float((streams.get("format") or {}).get("duration", 0.0)),
        "frames": job.state(Stage.RENDER).meta.get("frames"),
        "fps": 30,
        "integrated_lufs": (report.get("loudness") or {}).get("integrated_lufs"),
        "width": video.get("width"), "height": video.get("height"),
    }
    # Who to credit for the pictures. The content stage wrote captions.txt and
    # metadata/ long before the visuals stage knew which photographs it would
    # find, so both are re-emitted here with the attribution the licence asks
    # for. A reel that used no stock footage gets an empty credit and output
    # identical to what it was.
    from app.stages import visuals as V

    cover_meta = (job.state(Stage.COVER).meta or {}).get("backdrop_credit") or {}
    credits = V.credits_from(V.read_assets(paths.visuals_json), cover_meta)
    credit = V.credit_line(credits)

    atomic_write(paths.notes_md,
                 documents.notes_md(content, platforms, spec=spec, facts=facts,
                                    credits=credits))

    paths.out.mkdir(parents=True, exist_ok=True)
    if credit:
        atomic_write(paths.out / "captions.txt",
                     documents.caption_block(content, platforms, credit=credit))
        paths.metadata.mkdir(parents=True, exist_ok=True)
        for name, body in documents.metadata_files(content, platforms, credit=credit).items():
            atomic_write(paths.metadata / name, body)
    for source, name in ((paths.reel_mp4, f"{job.slug}-reel.mp4"),
                         (paths.cover_png, f"{job.slug}-reel.png"),
                         (paths.facts_json, "facts.json"),
                         (paths.content_json, "content.json"),
                         (paths.verify_json, "verify.json"),
                         (paths.contact_png, "contact.png")):
        if source.exists():
            shutil.copy2(source, paths.out / name)

    with zipfile.ZipFile(paths.bundle_zip, "w", zipfile.ZIP_DEFLATED) as bundle:
        for item in sorted(paths.out.rglob("*")):
            if item.is_file() and item != paths.bundle_zip:
                bundle.write(item, item.relative_to(paths.out))

    _note(job, progress, f"packaged {paths.bundle_zip.name}")
    return {"artifacts": {"notes": paths.rel(paths.notes_md),
                          "bundle": paths.rel(paths.bundle_zip),
                          "out_dir": paths.rel(paths.out)},
            "meta": {"spec": spec, "bytes": paths.bundle_zip.stat().st_size}}


RUNNERS: dict[Stage, Callable[[Job, JobStore, Progress | None], dict]] = {
    Stage.INGEST: run_ingest,
    Stage.CONTENT: run_content,
    Stage.COVER: run_cover,
    Stage.VISUALS: run_visuals,
    Stage.AUDIO: run_audio,
    Stage.ALIGN: run_align,
    Stage.STORYBOARD: run_storyboard,
    Stage.RENDER: run_render,
    Stage.VERIFY: run_verify,
    Stage.PACKAGE: run_package,
}


def run_stage(job: Job, stage: Stage, store: JobStore,
              progress: Progress | None = None) -> Job:
    """Run one stage, recording its outcome on the job either way."""
    blockers = job.blockers(stage)
    if blockers:
        raise StageFailed(
            f"{stage.value} cannot run yet: {', '.join(b.value for b in blockers)} "
            "must finish first."
        )
    job.mark(stage, Status.RUNNING)
    job.invalidate_from(stage)
    store.save(job)
    try:
        outcome = RUNNERS[stage](job, store, progress)
    except Exception as exc:
        job.mark(stage, Status.FAILED, error=f"{type(exc).__name__}: {exc}")
        job.note(f"{stage.value} failed: {exc}")
        store.save(job)
        raise
    status = Status.REVIEW if job.pauses_at(stage) else Status.DONE
    job.mark(stage, status, artifacts=outcome.get("artifacts"), meta=outcome.get("meta"))
    store.save(job)
    return job


# ---------------------------------------------------------------- walking ---
# `runner._run_inline` and `tasks.run_pipeline_task` were near-identical copies
# of the loop below, and they had already drifted: the Celery one could never
# publish `{"type": "pipeline", "status": "failed"}`, because `run_stage_task`
# re-raises and the FAILED check after it was unreachable. A browser watching a
# Celery run was told the stage failed and never that the pipeline had stopped.
#
# So the walk lives here, next to `run_stage`, which is already the one
# definition of what a stage is.
import threading  # noqa: E402  (deliberately local to this section)
import traceback  # noqa: E402

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def job_lock(job_id: str) -> threading.Lock:
    """One stage at a time per job -- `job.json` has a single writer."""
    with _locks_guard:
        return _locks.setdefault(job_id, threading.Lock())


def run_one(job_id: str, stage: Stage, store: JobStore | None = None) -> str:
    """Run exactly one stage. Returns "done" | "review" | "failed:<stage>"."""
    from app.progress import publish, reporter

    store = store or JobStore()
    with job_lock(job_id):
        job = store.load(job_id)
        publish(job_id, {"type": "stage", "stage": stage.value, "status": "running"})
        try:
            job = run_stage(job, stage, store, progress=reporter(job_id, stage.value))
        except Exception as exc:
            publish(job_id, {"type": "stage", "stage": stage.value, "status": "failed",
                             "error": str(exc)[:2000]})
            traceback.print_exc()
            return "failed:" + stage.value
        state = job.state(stage)
        publish(job_id, {"type": "stage", "stage": stage.value,
                         "status": state.status.value, "artifacts": state.artifacts})
        return "review" if state.status is Status.REVIEW else "done"


def walk(job_id: str, store: JobStore | None = None, *,
         until: Stage | None = None,
         should_stop: Callable[[Job], bool] | None = None) -> str:
    """Run stages until the pipeline ends, a gate stops it, or something fails.

    Returns why it stopped, which is what the queue records as the job's reason:
    "complete" | "review:<stage>" | "failed:<stage>" | "until:<stage>" |
    "cancelled" | "gone".

    `should_stop` is checked against the freshly-loaded job at the top of each
    iteration, which is free -- the job is loaded there anyway -- and is how a
    cancel takes effect at a stage boundary rather than mid-stage. Mid-stage is
    not an option: DEVELOPMENT.md gotcha 8 is that a killed render leaves a shorter,
    playable, wrong file, and a killed content stage leaves half a content.json.
    """
    from app.progress import publish, reporter

    store = store or JobStore()
    with job_lock(job_id):
        while True:
            try:
                job = store.load(job_id)
            except FileNotFoundError:
                return "gone"

            if should_stop and should_stop(job):
                publish(job_id, {"type": "pipeline", "status": "cancelled"})
                return "cancelled"

            stage = job.next_stage()
            if stage is None:
                publish(job_id, {"type": "pipeline", "status": "complete"})
                return "complete"
            if until and STAGE_ORDER.index(stage) > STAGE_ORDER.index(until):
                return "until:" + until.value

            publish(job_id, {"type": "stage", "stage": stage.value, "status": "running"})
            try:
                job = run_stage(job, stage, store, progress=reporter(job_id, stage.value))
            except Exception as exc:
                publish(job_id, {"type": "stage", "stage": stage.value,
                                 "status": "failed", "error": str(exc)[:2000]})
                publish(job_id, {"type": "pipeline", "status": "failed",
                                 "stage": stage.value})
                traceback.print_exc()
                return "failed:" + stage.value

            state = job.state(stage)
            publish(job_id, {"type": "stage", "stage": stage.value,
                             "status": state.status.value,
                             "artifacts": state.artifacts})
            if state.status is Status.REVIEW:
                publish(job_id, {"type": "pipeline", "status": "awaiting_review",
                                 "stage": stage.value})
                return "review:" + stage.value
            if state.status is Status.FAILED:
                # reachable here, unlike in the Celery copy this replaced
                publish(job_id, {"type": "pipeline", "status": "failed",
                                 "stage": stage.value})
                return "failed:" + stage.value
