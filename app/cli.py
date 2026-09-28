"""Command line access to the same pipeline the UI drives.

    python -m app.cli new https://github.com/owner/repo --template research
    python -m app.cli run <job-id> --stage content
    python -m app.cli list
    python -m app.cli show <job-id>
    python -m app.cli providers
    python -m app.cli doctor

Useful headlessly, in CI, and inside the container when something needs poking
at without a browser.
"""
from __future__ import annotations

import argparse
import json
import sys

from app.config import get_config
from app.models.job import STAGE_ORDER, JobSource, ProviderChoice, Stage, Status
from app.store import JobStore


def _print(payload) -> None:
    print(json.dumps(payload, indent=2, default=str))


def cmd_new(args) -> int:
    from app.ingest import IngestError, detect

    try:
        kind = detect(args.url)
    except IngestError as exc:
        print(exc, file=sys.stderr)
        return 2
    store = JobStore()
    job = store.create(
        args.slug or args.url.rstrip("/").split("/")[-1],
        JobSource(kind=kind, url=args.url),
        template=args.template,
        providers=ProviderChoice(llm_provider=args.llm, tts_provider=args.tts),
        manual_stages=[] if args.no_gates else None,
    )
    print(job.id)
    if args.run:
        return cmd_run(argparse.Namespace(job=job.id, stage=None, until=None))
    return 0


def cmd_run(args) -> int:
    from app.stages.pipeline import run_stage

    store = JobStore()
    job = store.load(args.job)
    stages = [Stage(args.stage)] if args.stage else list(STAGE_ORDER)
    stop_at = Stage(args.until) if getattr(args, "until", None) else None

    for stage in stages:
        if job.is_done(stage):
            continue
        if stop_at and STAGE_ORDER.index(stage) > STAGE_ORDER.index(stop_at):
            break
        print(f"==> {stage.value}", flush=True)
        try:
            job = run_stage(job, stage, store,
                            progress=lambda m: print(f"    {m}", flush=True))
        except Exception as exc:
            print(f"    FAILED: {exc}", file=sys.stderr)
            return 1
        if job.state(stage).status is Status.REVIEW and not args.stage:
            print(f"    awaiting review; approve with: "
                  f"python -m app.cli approve {job.id} {stage.value}")
            break
    return 0


def cmd_approve(args) -> int:
    store = JobStore()
    job = store.load(args.job)
    job.mark(Stage(args.stage), Status.DONE)
    store.save(job)
    print(f"{args.stage} approved")
    return 0


def cmd_list(args) -> int:
    for job in JobStore().iter_jobs(limit=args.limit):
        marker = ("failed at " + job.failed_stage.value) if job.failed_stage else (
            job.next_stage().value if job.next_stage() else "complete")
        print(f"{job.id}  {job.slug:<22} {job.progress * 100:3.0f}%  {marker}")
    return 0


def cmd_show(args) -> int:
    job = JobStore().load(args.job)
    _print(json.loads(job.model_dump_json()))
    return 0


def cmd_providers(args) -> int:
    from app.api.routes_system import providers

    _print(providers())
    return 0


def cmd_doctor(args) -> int:
    """Check that everything the pipeline depends on is actually present."""
    import shutil

    from app.render.fonts import available as fonts_available
    from app.render.workspace import ffmpeg_bin, vendored_ffmpeg_usable
    from app.runner import mode

    cfg = get_config()
    checks: list[tuple[str, bool, str]] = []

    # What matters is that *an* ffmpeg runs here, not which one. The vendored
    # video/bin builds are macOS x86_64 and are used when they work; otherwise
    # align.py's own tool() falls back to PATH, which is the container's case.
    on_path = shutil.which("ffmpeg")
    vendored = vendored_ffmpeg_usable(str(cfg.paths.video))
    try:
        resolved = ffmpeg_bin()
    except Exception:
        resolved = ""
    checks.append(("ffmpeg available", bool(resolved),
                   resolved or "neither video/bin nor PATH has a runnable ffmpeg"))
    checks.append(("  from", True,
                   "vendored video/bin" if vendored else
                   ("PATH" if on_path else "nowhere")))
    if resolved:
        from app.render.workspace import MIN_FFMPEG, ffmpeg_version

        version = ffmpeg_version(resolved)
        recent = version is not None and version >= MIN_FFMPEG
        checks.append(("  version", recent,
                       (f"{version[0]}.{version[1]}" if version else "unreadable")
                       + ("" if recent else
                          f" -- the encoder needs {MIN_FFMPEG[0]}.{MIN_FFMPEG[1]}+; install a "
                          "current ffmpeg and point render.ffmpeg_dir at it")))
    checks.append(("video pipeline present", (cfg.paths.video / "kit.py").exists(),
                   str(cfg.paths.video)))
    checks.append(("bundled fonts", fonts_available(),
                   str(cfg.fonts.dir_path())))
    checks.append(("job directory writable", cfg.paths.jobs.exists(),
                   str(cfg.paths.jobs)))
    checks.append((f"executor ({mode()})", True, "celery if redis is reachable"))
    if cfg.visuals.enabled:
        from app.providers.visuals import probe

        health = probe(cfg)
        # What is worth printing differs by backend: a GPU server is its
        # devices, a keyed web API is its quota.
        detail = health.get("error") or " ".join(x for x in (
            str(health.get("base_url") or ""),
            ", ".join(f"{d.get('name')} {d.get('vram_gb')} GB"
                      for d in health.get("devices") or []),
            (f"{health['quota_remaining']} requests left"
             if health.get("quota_remaining") is not None else ""),
        ) if x)
        checks.append((f"{health.get('provider') or 'visuals'} ({cfg.visuals.active})",
                       bool(health.get("reachable")) and not health.get("error"), detail))
    else:
        checks.append(("pictures", True, "off (no visuals profile active)"))

    ok = True
    for name, passed, detail in checks:
        ok = ok and passed
        print(f"  {'ok  ' if passed else 'FAIL'}  {name:34} {detail}")

    if fonts_available():
        try:
            sys.path.insert(0, str(cfg.paths.video))
            import kit

            from app.render.fonts import check_symbols, patch_kit

            patch_kit(kit)
            check_symbols(kit)
            print(f"  ok    {'mono face has every symbol':34} {kit.MN.split('/')[-1]}")
        except Exception as exc:
            ok = False
            print(f"  FAIL  {'mono face symbols':34} {exc}")
    return 0 if ok else 1


def cmd_sfx_library(args) -> int:
    """Generate (or regenerate) the one-shot cut sounds for the active profile.

    The render stage makes them on demand the first time a reel asks for
    samples; this is for doing it up front, or for redoing one kind after
    editing its description in app/stages/visuals.py.
    """
    from app.providers.visuals import build_visuals
    from app.stages import visuals as V
    from app.stages.pipeline import sfx_library_dir

    cfg = get_config()
    if not cfg.visuals.enabled:
        print("no ComfyUI profile is active; nothing to generate")
        return 1
    provider = build_visuals(cfg)
    if not provider.supports_audio:
        print(f"profile {cfg.visuals.active!r} has no audio workflow")
        return 1
    directory = sfx_library_dir(cfg.visuals.active)
    kinds = {k: v for k, v in V.SFX_KINDS.items() if not args.kind or k in args.kind}
    if args.force:
        for kind in kinds:
            (directory / f"{kind}.wav").unlink(missing_ok=True)
    failed = V.ensure_sfx_library(provider, directory, kinds=kinds, progress=lambda m: print("  " + m))
    present = sorted(p.stem for p in directory.glob("*.wav"))
    print(f"{len(present)} one-shot(s) in {directory}: {', '.join(present)}")
    for kind, err in failed.items():
        print(f"  FAIL  {kind}: {err}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reelforge", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("new", help="create a job")
    p.add_argument("url")
    p.add_argument("--slug")
    p.add_argument("--template", default="cool-indigo")
    p.add_argument("--llm")
    p.add_argument("--tts")
    p.add_argument("--no-gates", action="store_true",
                   help="run straight through without pausing for review")
    p.add_argument("--run", action="store_true")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("run", help="run a job, or one stage of it")
    p.add_argument("job")
    p.add_argument("--stage", choices=[s.value for s in STAGE_ORDER])
    p.add_argument("--until", choices=[s.value for s in STAGE_ORDER])
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("approve", help="accept a stage awaiting review")
    p.add_argument("job")
    p.add_argument("stage", choices=[s.value for s in STAGE_ORDER])
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("list", help="list jobs")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="print a job's full state")
    p.add_argument("job")
    p.set_defaults(func=cmd_show)

    sub.add_parser("providers", help="what is reachable right now").set_defaults(
        func=cmd_providers)
    sub.add_parser("doctor", help="check the environment").set_defaults(func=cmd_doctor)
    p = sub.add_parser("sfx-library", help="generate the cut-sound one-shots with the audio workflow")
    p.add_argument("kind", nargs="*", help="only these kinds (default: every kind)")
    p.add_argument("--force", action="store_true", help="regenerate kinds that already exist")
    p.set_defaults(func=cmd_sfx_library)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
