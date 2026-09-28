"""Job endpoints: create, inspect, advance, edit, download."""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from typing import Literal

from pydantic import BaseModel

from app.api.schemas import CreateJobRequest, JobView, RunRequest, VisualsChoiceIn
from app.models.content import ReelContent
from app.models.job import JobImage, STAGE_ORDER, JobSource, ProviderChoice, Stage, Status, JobVisuals
from app.runner import submit_pipeline, submit_stage
from app.store import JobStore, atomic_write

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def store() -> JobStore:
    return JobStore()


def _load(job_id: str):
    try:
        return store().load(job_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no such job: {job_id}") from None


@router.get("")
def list_jobs(
    limit: int = Query(default=50, ge=1, le=500),
    archived: bool | None = Query(
        default=False,
        description="false for current jobs, true for archived, omit-as-null for all",
    ),
) -> list[dict]:
    out = []
    for job in store().iter_jobs(limit=limit, archived=archived):
        out.append({
            "id": job.id, "slug": job.slug, "template": job.template,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
            "progress": job.progress,
            "next_stage": job.next_stage().value if job.next_stage() else None,
            "failed_stage": job.failed_stage.value if job.failed_stage else None,
            "url": job.source.url,
            "archived": job.archived,
            "archived_at": job.archived_at.isoformat() if job.archived_at else None,
        })
    return out


@router.get("/counts")
def job_counts() -> dict:
    """How many are current versus archived, for the tab labels."""
    current = sum(1 for _ in store().iter_jobs(archived=False))
    archived = sum(1 for _ in store().iter_jobs(archived=True))
    return {"current": current, "archived": archived, "total": current + archived}


@router.post("", status_code=201)
def create_job(request: CreateJobRequest) -> JobView:
    from app.ingest import IngestError, detect

    if not request.url:
        raise HTTPException(400, "a url is required (upload-only jobs use /uploads)")
    try:
        kind = detect(request.url)
    except IngestError as exc:
        raise HTTPException(400, str(exc)) from None

    slug = request.slug or request.url.rstrip("/").split("/")[-1]
    job = store().create(
        slug, JobSource(kind=kind, url=request.url),
        template=request.template,
        captions=request.captions,
        fact_check=request.fact_check,
        providers=ProviderChoice(
            llm_provider=request.llm_provider, llm_model=request.llm_model,
            tts_provider=request.tts_provider, tts_voice=request.tts_voice,
        ),
        manual_stages=request.manual_stages,
        visuals=(JobVisuals(**request.visuals.model_dump()) if request.visuals else None),
    )
    if request.autostart:
        submit_pipeline(job.id)
    return JobView.of(job)


@router.get("/{job_id}")
def get_job(job_id: str) -> JobView:
    return JobView.of(_load(job_id))


class JobSettingsIn(BaseModel):
    """Things about a job that can change after it was created."""

    template: str | None = None
    captions: bool | None = None
    fact_check: Literal["strict", "warn", "off"] | None = None
    manual_stages: list[str] | None = None
    llm_provider: str | None = None
    tts_provider: str | None = None
    tts_voice: str | None = None
    visuals: VisualsChoiceIn | None = None


@router.patch("/{job_id}")
def update_job(job_id: str, payload: JobSettingsIn = Body(...)) -> JobView:
    """Change a job's template, gates or providers, then re-run what it affects.

    Switching template is the common one: a storyboard that a model cannot write
    well is better rendered from data, and that decision is usually made after
    seeing the first attempt rather than before.
    """
    from app.templates import discover

    job = _load(job_id)

    # A running stage holds its own copy of the job and writes it back when it
    # finishes, so an edit made now would be silently discarded. Refusing is
    # better than losing the change without saying so.
    running = [s.value for s in STAGE_ORDER
               if job.state(s).status is Status.RUNNING]
    if running:
        raise HTTPException(
            409,
            f"{', '.join(running)} is running. Wait for it to finish, or stop it, "
            "before changing the job -- an edit made now would be overwritten "
            "when the stage saves its result.",
        )

    changed: list[str] = []

    if payload.template and payload.template != job.template:
        if payload.template not in discover():
            raise HTTPException(
                404, f"unknown template {payload.template!r}; "
                     f"available: {', '.join(sorted(discover()))}")
        job.template = payload.template
        changed.append("template")
        # the template decides how the storyboard is produced, so anything
        # already built from the old one no longer matches
        cleared = job.invalidate_from(Stage.ALIGN)
        job.note(f"template changed to {payload.template}; "
                 f"cleared {[c.value for c in cleared]}")

    if payload.captions is not None and payload.captions != job.captions:
        job.captions = payload.captions
        changed.append("captions")
        # captions are drawn by the storyboard, so the decision has to be made
        # before it is written -- flipping it after invalidates that work
        cleared = job.invalidate_from(Stage.STORYBOARD)
        job.note(f"burned-in captions {'on' if payload.captions else 'off'}; "
                 f"cleared {[c.value for c in cleared]}")

    if payload.fact_check and payload.fact_check != job.fact_check:
        job.fact_check = payload.fact_check
        changed.append("fact_check")
        # the rule is applied while the script is generated, so changing it
        # only means anything if the script is written again
        cleared = job.invalidate_from(Stage.CONTENT)
        job.note(f"fact checking set to {payload.fact_check}; "
                 f"cleared {[c.value for c in cleared]}")

    if payload.visuals is not None:
        new_visuals = JobVisuals(**payload.visuals.model_dump())
        if new_visuals != job.visuals:
            job.visuals = new_visuals
            changed.append("visuals")
            # the cover backdrop and every generated still come from these
            # choices, so both stages that draw them are built again
            cleared = job.invalidate_from(Stage.CONTENT)
            job.note(f"generated visuals set to {new_visuals.model_dump(exclude_none=True)}; "
                     f"cleared {[c.value for c in cleared]}")

    if payload.manual_stages is not None:
        known = {s.value for s in STAGE_ORDER}
        unknown = [s for s in payload.manual_stages if s not in known]
        if unknown:
            raise HTTPException(422, f"unknown stage(s): {unknown}")
        job.manual_stages = [s for s in STAGE_ORDER
                             if s.value in set(payload.manual_stages)]
        changed.append("manual_stages")

    for field in ("llm_provider", "tts_provider", "tts_voice"):
        value = getattr(payload, field)
        if value is None:
            continue
        # an empty string means "clear this override and follow the settings"
        setattr(job.providers, field, value.strip() or None)
        changed.append(field)
        if field == "llm_provider":
            # the script was written by a different model; what follows it was
            # built from that script
            job.invalidate_from(Stage.INGEST)

    if changed:
        job.note(f"settings changed: {', '.join(changed)}")
    store().save(job)
    return JobView.of(job)


@router.post("/{job_id}/archive")
def archive_job(job_id: str) -> dict:
    """Keep the job and its artefacts, take it out of the working list."""
    job = _load(job_id)
    store().save(job.archive())
    return {"id": job.id, "archived": True,
            "archived_at": job.archived_at.isoformat()}


@router.post("/{job_id}/unarchive")
def unarchive_job(job_id: str) -> dict:
    job = _load(job_id)
    store().save(job.unarchive())
    return {"id": job.id, "archived": False}


@router.delete("/{job_id}", status_code=204, response_model=None)
def delete_job(job_id: str) -> None:
    """Remove the job and everything it produced. Not reversible.

    Archiving is almost always what is wanted instead -- this also deletes the
    rendered video, the cover and the platform copy.
    """
    store().delete(job_id)


@router.post("/{job_id}/run")
def run(job_id: str, request: RunRequest = Body(default=RunRequest())) -> dict:
    job = _load(job_id)
    if request.stage:
        try:
            stage = Stage(request.stage)
        except ValueError:
            raise HTTPException(400, f"unknown stage: {request.stage}") from None
        blockers = job.blockers(stage)
        if blockers:
            raise HTTPException(
                409,
                f"{stage.value} is blocked by: {', '.join(b.value for b in blockers)}",
            )
        dispatch = submit_stage(job_id, stage)
        return {"queued": stage.value, "executor": dispatch}

    until = Stage(request.until) if request.until else None
    dispatch = submit_pipeline(job_id, until)
    return {"queued": "pipeline", "until": request.until, "executor": dispatch}


@router.post("/{job_id}/stages/{stage_value}/approve")
def approve(job_id: str, stage_value: str, advance: bool = True) -> dict:
    """Accept a stage that is waiting for review, and optionally carry on."""
    job = _load(job_id)
    try:
        stage = Stage(stage_value)
    except ValueError:
        raise HTTPException(400, f"unknown stage: {stage_value}") from None
    state = job.state(stage)
    if state.status not in (Status.REVIEW, Status.DONE):
        raise HTTPException(409, f"{stage.value} is {state.status.value}, not awaiting review")
    job.mark(stage, Status.DONE)
    job.note(f"{stage.value} approved")
    store().save(job)
    # `front` is the whole point of approving: the reel already had its turn
    # and only stopped because it was told to. Sending it to the back means a
    # reel queued while you were reading the script now runs first, and the
    # one you just approved waits again. `retry` has always done this.
    dispatch = submit_pipeline(job_id, front=True) if advance else None
    return {"approved": stage.value, "executor": dispatch}


@router.post("/{job_id}/stages/{stage_value}/retry")
def retry(job_id: str, stage_value: str) -> dict:
    job = _load(job_id)
    try:
        stage = Stage(stage_value)
    except ValueError:
        raise HTTPException(400, f"unknown stage: {stage_value}") from None
    cleared = job.invalidate_from(stage)
    job.note(f"{stage.value} re-queued; cleared {[c.value for c in cleared]}")
    store().save(job)
    return {"queued": stage.value, "invalidated": [c.value for c in cleared],
            "executor": submit_stage(job_id, stage)}


# ---------------------------------------------------------------- editing --
@router.get("/{job_id}/content")
def get_content(job_id: str) -> dict:
    job = _load(job_id)
    path = store().paths(job).content_json
    if not path.exists():
        raise HTTPException(404, "content has not been generated yet")
    return json.loads(path.read_text(encoding="utf-8"))


@router.put("/{job_id}/content")
def put_content(job_id: str, payload: dict = Body(...)) -> dict:
    """Replace the script after editing it.

    Everything downstream is invalidated: a video rendered from the previous
    script must not survive an edit to it.
    """
    job = _load(job_id)
    paths = store().paths(job)
    try:
        content = ReelContent.model_validate(payload)
    except Exception as exc:
        raise HTTPException(422, f"content is not valid: {exc}") from None

    from app.render import documents

    atomic_write(paths.content_json, content.model_dump_json(indent=2))
    atomic_write(paths.phrases_txt, documents.phrases_txt(content))
    cleared = job.invalidate_from(Stage.CONTENT)
    job.note(f"content edited by hand; cleared {[c.value for c in cleared]}")
    store().save(job)
    return {"ok": True, "invalidated": [c.value for c in cleared],
            "phrases": len(content.phrases), "words": content.word_count}


@router.get("/{job_id}/storyboard", response_class=PlainTextResponse)
def get_storyboard(job_id: str) -> str:
    job = _load(job_id)
    path = store().paths(job).storyboard_py
    if not path.exists():
        raise HTTPException(404, "no storyboard yet")
    return path.read_text(encoding="utf-8")


@router.put("/{job_id}/storyboard")
def put_storyboard(job_id: str, source: str = Body(..., media_type="text/plain")) -> dict:
    """Replace the storyboard by hand, and re-run the same checks generation faces."""
    job = _load(job_id)
    paths = store().paths(job)
    from app.stages.storyboard import run_smoke
    from app.validate.storyboard import check_source

    static = check_source(source, paths.timing_json)
    if not static.ok:
        raise HTTPException(422, {"problems": static.messages()})
    paths.storyboard_py.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(paths.storyboard_py, source)
    smoke = run_smoke(paths.workspace, job.slug, paths.smoke_dir)
    cleared = job.invalidate_from(Stage.STORYBOARD)
    job.note("storyboard edited by hand")
    store().save(job)
    return {"ok": smoke.get("ok"), "problems": [p["message"] for p in smoke.get("problems", [])],
            "invalidated": [c.value for c in cleared]}


@router.get("/{job_id}/alignment")
def get_alignment(job_id: str, drop: float = 32.0, min_sil: int = 220) -> dict:
    """Detected segments beside the phrase lines, for the split/merge view."""
    from app.stages.align import reconcile_preview

    job = _load(job_id)
    try:
        return reconcile_preview(job, store(), drop=drop, min_sil=min_sil)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from None


@router.put("/{job_id}/phrases")
def put_phrases(job_id: str, lines: list[str] = Body(...)) -> dict:
    """Re-split the phrase list so it matches what was actually spoken."""
    job = _load(job_id)
    paths = store().paths(job)
    content = ReelContent.model_validate_json(paths.content_json.read_text(encoding="utf-8"))
    if len(lines) < 1:
        raise HTTPException(422, "at least one phrase is required")

    # keep each phrase's scene, stretching the last one if lines were merged
    scenes = [p.scene_index for p in content.phrases]
    rebuilt = []
    for index, text in enumerate(lines):
        rebuilt.append({"text": text,
                        "scene_index": scenes[min(index, len(scenes) - 1)],
                        "pause_after_ms": 300})
    content = content.model_copy(update={"phrases": [
        type(content.phrases[0]).model_validate(p) for p in rebuilt
    ]})
    from app.render import documents

    atomic_write(paths.content_json, content.model_dump_json(indent=2))
    atomic_write(paths.phrases_txt, documents.phrases_txt(content))
    job.invalidate_from(Stage.ALIGN)
    job.note(f"phrase list re-split by hand to {len(lines)} lines")
    store().save(job)
    return {"ok": True, "phrases": len(lines)}


# --------------------------------------------------------------- uploads --
@router.post("/{job_id}/audio")
async def upload_audio(job_id: str, file: UploadFile) -> dict:
    job = _load(job_id)
    paths = store().paths(job)
    if not (file.filename or "").lower().endswith((".mp3", ".wav", ".m4a", ".aac", ".flac")):
        raise HTTPException(400, "expected an audio file (.mp3, .wav, .m4a, .aac, .flac)")
    paths.audio_mp3.parent.mkdir(parents=True, exist_ok=True)
    raw = await file.read()
    paths.audio_mp3.write_bytes(raw)
    job.providers.tts_provider = "upload"
    job.invalidate_from(Stage.AUDIO)
    job.note(f"narration uploaded: {file.filename} ({len(raw) // 1024} KiB)")
    store().save(job)
    return {"ok": True, "bytes": len(raw), "path": paths.rel(paths.audio_mp3)}


@router.post("/{job_id}/markdown")
async def upload_markdown(job_id: str, file: UploadFile) -> dict:
    job = _load(job_id)
    paths = store().paths(job)
    if not (file.filename or "").lower().endswith((".md", ".markdown", ".txt")):
        raise HTTPException(400, "expected a .md or .txt file")
    paths.uploads.mkdir(parents=True, exist_ok=True)
    target = paths.uploads / Path(file.filename).name
    target.write_bytes(await file.read())
    job.source.uploaded_markdown_name = target.name
    job.invalidate_from(Stage.INGEST)
    job.note(f"markdown uploaded: {target.name}")
    store().save(job)
    return {"ok": True, "name": target.name}


@router.get("/{job_id}/upscales")
def list_upscales(job_id: str) -> dict:
    """What sizes exist, and what could be made."""
    from app.render.encode import RESOLUTIONS

    job = _load(job_id)
    paths = store().paths(job)
    out = []
    for name, (width, height) in RESOLUTIONS.items():
        target = paths.reel_mp4 if name == "1080p" else paths.out / f"{job.slug}-reel-{name}.mp4"
        exists = target.exists()
        out.append({"resolution": name, "width": width, "height": height,
                    "native": name == "1080p", "exists": exists,
                    "bytes": target.stat().st_size if exists else None,
                    "path": paths.rel(target) if exists else None})
    return {"master": paths.rel(paths.reel_mp4) if paths.reel_mp4.exists() else None,
            "sizes": out}


@router.post("/{job_id}/upscale")
def upscale(job_id: str, resolution: str = Body(..., embed=True)) -> dict:
    """Scale the rendered reel up to 2K or 4K, beside the verified master.

    The 1080x1920 file stays the deliverable `verify` checked and the one both
    platforms want; this is an extra. Nothing here invents detail -- it is a
    Lanczos resample of the same frames -- so it is offered rather than
    default.
    """
    from app.render.encode import RESOLUTIONS, upscale_cmd
    from app.render.workspace import ffmpeg_bin

    key = (resolution or "").strip().lower()
    if key not in RESOLUTIONS:
        raise HTTPException(422, f"unknown resolution {resolution!r}; "
                                 f"expected one of {', '.join(RESOLUTIONS)}")
    job = _load(job_id)
    paths = store().paths(job)
    if not paths.reel_mp4.exists():
        raise HTTPException(409, "there is no rendered reel yet to scale up")
    if key == "1080p":
        return {"resolution": key, "path": paths.rel(paths.reel_mp4),
                "bytes": paths.reel_mp4.stat().st_size, "native": True}

    width, height = RESOLUTIONS[key]
    paths.out.mkdir(parents=True, exist_ok=True)
    target = paths.out / f"{job.slug}-reel-{key}.mp4"
    cmd = upscale_cmd(ffmpeg_bin(), paths.reel_mp4, target, width, height)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise HTTPException(500, "upscale failed: " + (proc.stderr or "").strip()[-400:])

    job.note(f"upscaled to {key} ({width}x{height})")
    store().save(job)
    return {"resolution": key, "width": width, "height": height,
            "path": paths.rel(target), "bytes": target.stat().st_size, "native": False}


# ------------------------------------------------------------- artifacts --
@router.get("/{job_id}/artifacts/{path:path}")
def artifact(job_id: str, path: str):
    job = _load(job_id)
    root = store().dir_for(job.id).resolve()
    target = (root / path).resolve()
    if not str(target).startswith(str(root)) or not target.is_file():
        raise HTTPException(404, "no such artifact")
    # filename= sets Content-Disposition, so "save as" offers the artifact's
    # own name (requests-bundle.zip) rather than whatever the browser guesses.
    # Archives download outright; everything else renders inline as before.
    disposition = "attachment" if target.suffix.lower() in (".zip", ".gz", ".tar") else "inline"
    return FileResponse(target, filename=target.name,
                        content_disposition_type=disposition)


# ---------------------------------------------------------------- images --
class ImageSettingsIn(BaseModel):
    """How an uploaded image should be shown. Every field optional: the crop UI
    saves the crop, the role picker saves the role, neither needs the other."""

    role: Literal["repo", "app", "output", "other"] | None = None
    fit: Literal["panel", "full"] | None = None
    position: Literal["top", "centre", "bottom"] | None = None
    crop_x: float | None = None
    crop_y: float | None = None
    crop_w: float | None = None
    crop_h: float | None = None
    caption: str | None = None


def _prepare_image(job, paths, image) -> None:
    """Re-cut the bitmap the renderer pastes, from the stored crop."""
    from app import images as imagelib

    source = paths.images / image.filename
    target = paths.prepared_images / f"{image.id}.png"
    width, height = imagelib.prepare(
        source, target, fit=image.fit,
        crop=imagelib.Crop(image.crop_x, image.crop_y, image.crop_w, image.crop_h))
    image.prepared = paths.rel(target)
    image.prepared_width, image.prepared_height = width, height


@router.post("/{job_id}/images", status_code=201)
async def upload_images(job_id: str, files: list[UploadFile],
                        role: str = Query("other")) -> dict:
    """Add one or more screenshots. Optional -- a job with none renders exactly
    as it did before, because the screen catalogue skips layouts it cannot fill.
    """
    from app import images as imagelib

    job = _load(job_id)
    paths = store().paths(job)
    paths.images.mkdir(parents=True, exist_ok=True)
    if role not in imagelib.ROLES:
        raise HTTPException(422, f"unknown role {role!r}; "
                                 f"known: {sorted(imagelib.ROLES)}")

    added, problems = [], []
    for upload in files:
        name = Path(upload.filename or "image").name
        stored = paths.images / f"{uuid.uuid4().hex[:8]}-{name}"
        stored.write_bytes(await upload.read())
        try:
            imagelib.inspect(stored)
            width, height = imagelib.shrink_source(stored)
        except imagelib.ImageError as exc:
            stored.unlink(missing_ok=True)
            problems.append(str(exc))
            continue

        fit = imagelib.default_fit(width, height)
        crop = imagelib.default_crop(width, height, fit)
        image = JobImage(
            id=uuid.uuid4().hex[:8], filename=stored.name, role=role, fit=fit,
            crop_x=crop.x, crop_y=crop.y, crop_w=crop.w, crop_h=crop.h,
            source_width=width, source_height=height,
        )
        _prepare_image(job, paths, image)
        job.images.append(image)
        added.append(image.model_dump())

    if added:
        # the storyboard decides which screens exist, so a new image changes it
        job.invalidate_from(Stage.STORYBOARD)
        job.note(f"{len(added)} image(s) uploaded as {role}")
        store().save(job)
    if problems and not added:
        raise HTTPException(400, "; ".join(problems))
    return {"added": added, "problems": problems,
            "images": [i.model_dump() for i in job.images]}


@router.patch("/{job_id}/images/{image_id}")
def update_image(job_id: str, image_id: str,
                 payload: ImageSettingsIn = Body(...)) -> dict:
    """Save a crop, a role or a caption, and re-cut the prepared bitmap."""
    job = _load(job_id)
    paths = store().paths(job)
    image = next((i for i in job.images if i.id == image_id), None)
    if image is None:
        raise HTTPException(404, f"no image {image_id!r} on this job")

    changed = payload.model_dump(exclude_none=True)
    for field, value in changed.items():
        setattr(image, field, value)
    if changed:
        _prepare_image(job, paths, image)
        job.invalidate_from(Stage.STORYBOARD)
        job.note(f"image {image_id} updated: {', '.join(sorted(changed))}")
        store().save(job)
    return image.model_dump()


@router.delete("/{job_id}/images/{image_id}")
def delete_image(job_id: str, image_id: str) -> dict:
    job = _load(job_id)
    paths = store().paths(job)
    image = next((i for i in job.images if i.id == image_id), None)
    if image is None:
        raise HTTPException(404, f"no image {image_id!r} on this job")
    (paths.images / image.filename).unlink(missing_ok=True)
    if image.prepared:
        (paths.root / image.prepared).unlink(missing_ok=True)
    job.images = [i for i in job.images if i.id != image_id]
    job.invalidate_from(Stage.STORYBOARD)
    job.note(f"image {image_id} removed")
    store().save(job)
    return {"ok": True, "images": [i.model_dump() for i in job.images]}


@router.get("/images/roles")
def image_roles() -> dict:
    """The labelled slots the new-reel form offers."""
    from app import images as imagelib

    return {"roles": [{"key": key, **value}
                      for key, value in sorted(imagelib.ROLES.items(),
                                               key=lambda kv: kv[1]["order"])]}
