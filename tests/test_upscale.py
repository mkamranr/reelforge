"""Delivering the reel at 2K and 4K.

The reel is rendered and verified at 1080x1920 and that file stays the master;
these are Lanczos upscales of it, written beside it. Two things here are not
cosmetic. A 4K stream tagged H.264 level 4.1 is out of spec, and the audio
must survive untouched -- it was normalised to -14 LUFS / -1.5 dBTP and
checked there, so a second AAC pass would move it away from a number the
platforms read.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REELFORGE_PATHS__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REELFORGE_EXECUTOR", "inline")
    import app.config
    import app.runner

    app.config.get_config.cache_clear()
    app.runner.mode.cache_clear()
    from app.main import app as fastapi_app

    return TestClient(fastapi_app)


def _job(client) -> str:
    response = client.post("/api/jobs", json={
        "url": "https://github.com/owner/repo", "autostart": False})
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def _reel(path: Path, seconds: float = 1.0) -> Path:
    """A real 1080x1920 H.264 file with silent AAC, as the renderer leaves it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [shutil.which("ffmpeg"), "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc=size=1080x1920:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={seconds}",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-profile:v", "high", "-level", "4.1",
         "-c:a", "aac", "-b:a", "192k", "-shortest", str(path)],
        check=True, capture_output=True)
    return path


# ------------------------------------------------------------- the maths ---
def test_a_4k_stream_is_not_tagged_level_4_1():
    """4.1 tops out at 1080p. A player is entitled to refuse a 4K file that
    claims it, and some do."""
    from app.render.encode import RESOLUTIONS, h264_level

    assert h264_level(1920) == "4.1"
    assert h264_level(RESOLUTIONS["2k"][1]) == "5.1"
    assert h264_level(RESOLUTIONS["4k"][1]) == "5.1"


def test_every_offered_size_is_nine_by_sixteen():
    from app.render.encode import RESOLUTIONS

    for name, (width, height) in RESOLUTIONS.items():
        assert abs(width / height - 9 / 16) < 0.001, f"{name} is not 9:16"
    assert RESOLUTIONS["1080p"] == (1080, 1920), "the master size must not drift"


def test_the_command_scales_with_lanczos_and_never_re_encodes_the_audio():
    """The audio carries a verified loudness measurement. Re-encoding it is
    the one thing this must not do."""
    from app.render.encode import upscale_cmd

    cmd = upscale_cmd("ffmpeg", Path("in.mp4"), Path("out.mp4"), 2160, 3840)
    joined = " ".join(cmd)

    assert "scale=2160:3840:flags=lanczos" in joined
    assert "-c:a copy" in joined
    assert "-b:a" not in joined, "the audio is being re-encoded"
    assert "-level 5.1" in joined and "-level 4.1" not in joined
    assert "+faststart" in joined
    # the master's own preset is far too slow for a resample; see the comment
    assert "-preset veryfast" in joined and "-preset slow" not in joined


def test_building_the_command_does_not_mutate_the_shared_encode_args():
    """`VIDEO_ARGS` is a module-level list the render path also uses. Editing
    it in place would retag every subsequent chunk encode."""
    from app.render import encode

    before = list(encode.VIDEO_ARGS)
    encode.upscale_cmd("ffmpeg", Path("a.mp4"), Path("b.mp4"), 2160, 3840)
    assert encode.VIDEO_ARGS == before


# ---------------------------------------------------------------- the API ---
def test_an_unknown_size_is_refused_with_the_ones_that_exist(client):
    job_id = _job(client)
    response = client.post(f"/api/jobs/{job_id}/upscale", json={"resolution": "8k"})
    assert response.status_code == 422
    assert "4k" in response.json()["detail"]


def test_there_is_nothing_to_scale_before_the_reel_is_rendered(client):
    job_id = _job(client)
    response = client.post(f"/api/jobs/{job_id}/upscale", json={"resolution": "4k"})
    assert response.status_code == 409
    assert "no rendered reel" in response.json()["detail"]


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not on PATH")
def test_the_native_size_is_reported_rather_than_re_encoded(client, tmp_path):
    """Asking for 1080p must hand back the master untouched -- re-encoding the
    verified file to the size it already is would only lose a generation."""
    job_id = _job(client)
    from app.store import JobStore

    store = JobStore()
    master = store.paths(store.load(job_id)).reel_mp4
    _reel(master)
    before = master.read_bytes()

    body = client.post(f"/api/jobs/{job_id}/upscale", json={"resolution": "1080p"}).json()
    assert body["native"] is True
    assert master.read_bytes() == before, "the master was re-encoded"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not on PATH")
def test_a_real_upscale_lands_at_the_asked_for_size(client, tmp_path):
    job_id = _job(client)
    from app.store import JobStore

    store = JobStore()
    job = store.load(job_id)
    _reel(store.paths(job).reel_mp4)

    listed = client.get(f"/api/jobs/{job_id}/upscales").json()
    assert {s["resolution"] for s in listed["sizes"]} == {"1080p", "2k", "4k"}
    assert not any(s["exists"] for s in listed["sizes"] if s["resolution"] == "2k")

    body = client.post(f"/api/jobs/{job_id}/upscale", json={"resolution": "2k"}).json()
    made = store.paths(job).out / f"{job.slug}-reel-2k.mp4"
    assert made.exists() and body["bytes"] == made.stat().st_size

    probe = subprocess.run(
        [shutil.which("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,level", "-of", "csv=p=0:nk=1", str(made)],
        capture_output=True, text=True, check=True).stdout.strip()
    assert probe.startswith("1440,2560"), probe
    assert probe.endswith("51"), f"expected level 5.1, got {probe}"

    # and it now shows as available without being remade
    after = client.get(f"/api/jobs/{job_id}/upscales").json()
    assert next(s for s in after["sizes"] if s["resolution"] == "2k")["exists"] is True
