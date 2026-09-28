"""ffmpeg post-processing shared by the visuals adapters.

Whatever a backend hands back -- a generated mp4 from ComfyUI, a stock clip
downloaded from a CDN -- has to arrive in the renderer's two formats: JPEG
frames at the reel's own size and frame rate, and 48 kHz stereo wav for the
mixer. Neither is specific to one backend, so both live here.

These were methods of the ComfyUI adapter until a second adapter needed them;
`comfyui` still re-exports both names.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from app.providers.visuals.base import VisualsError

__all__ = ["extract_frames", "to_wav48"]


def to_wav48(source: Path, out: Path) -> float:
    """Whatever the Save node wrote -> 48 kHz stereo 16-bit wav, the mixer's
    own format. Returns the length in seconds."""
    from app.render.workspace import ffmpeg_bin

    out.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == out.resolve():
        raise VisualsError("audio source and target are the same file")
    cmd = [ffmpeg_bin(), "-y", "-v", "error", "-i", str(source), "-ac", "2", "-ar", "48000",
           "-c:a", "pcm_s16le", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise VisualsError("audio conversion failed: " + proc.stderr.strip()[-400:])
    import wave

    with wave.open(str(out)) as w:
        return w.getnframes() / float(w.getframerate())


def extract_frames(source: Path, out_dir: Path, *, fps: int, width: int, height: int,
                   seconds: float | None = None, start: float = 0.0) -> int:
    """mp4 -> out_dir/00001.jpg ... at the reel's size and frame rate.

    Scale to cover then centre-crop, so a 9:16 clip fills the frame exactly
    and anything else loses its edges rather than letterboxing.

    `start` and `seconds` trim the source before any of that. A generated clip
    is made to length and needs neither, but a stock clip is whatever the
    photographer shot -- often half a minute -- and a reel wants five seconds
    of it. Trimming here rather than afterwards is the difference between
    writing 150 JPEGs and writing 900.
    """
    from app.render.workspace import ffmpeg_bin

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.jpg"):
        old.unlink()
    filters = (f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase,"
               f"crop={width}:{height}")
    cmd = [ffmpeg_bin(), "-y", "-v", "error"]
    # -ss before -i seeks by keyframe, which is fast and accurate enough for
    # picking a point inside a shot; -t after -i counts decoded output.
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(source)]
    if seconds is not None:
        cmd += ["-t", f"{seconds:.3f}"]
    cmd += ["-vf", filters, "-q:v", "3", str(out_dir / "%05d.jpg")]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise VisualsError("frame extraction failed: " + proc.stderr.strip()[-400:])
    count = len(list(out_dir.glob("*.jpg")))
    if count == 0:
        raise VisualsError("frame extraction produced no frames")
    return count
