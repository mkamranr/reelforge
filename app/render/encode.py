"""ffmpeg command construction.

The settings are lifted verbatim from `video/make.sh`, which is the only place
they have ever lived. They are not to drift: 1080x1920 - 30 fps - H.264 High
@ 4.1 - yuv420p - CRF 18 - closed GOP every 2 s - bt709 tagged - AAC-LC 192 kbps
48 kHz stereo - +faststart, with audio normalised to -14 LUFS / -1.5 dBTP.

bt709 tagging matters: untagged, both platforms render the colours washed out.
The loudness target is what both platforms normalise to -- hit it and neither
turns the video down.
"""
from __future__ import annotations

from pathlib import Path


class EncodeError(RuntimeError):
    """An ffmpeg step failed. Carries the stderr, which is where the reason is."""

#: 30 fps with keyint=60 means a closed GOP every 2.0 s. Chunk boundaries must
#: be a whole number of these or the concat demuxer cannot cut cleanly.
GOP_SECONDS = 2.0
#: The colour primaries are set here, inside the bitstream's VUI, as well as
#: through ffmpeg's output options.
#:
#: The output options alone are not enough. On the container's ffmpeg build they
#: reach `color_space` but not `color_primaries` or `color_transfer`, which
#: probe back as "unknown" straight after the encode -- the vendored macOS build
#: writes all three. x264 writes them into the stream itself, which every later
#: stream-copy carries, so this is the version-independent half of the fix.
#: Untagged, both platforms render the colours washed out.
X264_PARAMS = (
    "keyint=60:min-keyint=60:scenecut=0:ref=4:bframes=3"
    ":colorprim=bt709:transfer=bt709:colormatrix=bt709"
)
#: What both platforms normalise to. Hit it and neither turns the video down.
TARGET_I, TARGET_TP, TARGET_LRA = -14.0, -1.5, 11.0

#: How far the integrated loudness may land from target before `verify` rejects
#: the file, and the margin the correction loop aims inside.
LOUDNESS_TOLERANCE = 0.7

#: The limiter's ceiling, below the true-peak target. Two separate reasons:
#: AAC is lossy and reconstructs peaks a little above what it was given, and
#: `loudnorm`'s own true-peak control is an estimate -- one narration came out
#: at -1.0 dBTP against a -1.5 requirement, which is a file both platforms turn
#: down. The limiter is a hard ceiling; loudnorm's TP setting is not.
LIMITER_CEILING = -2.0

#: The single-pass filter, kept because `single_pass_cmd` is the reference path
#: and must stay byte-for-byte identical to make.sh.
LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"


def measure_loudness(ffmpeg: str, audio: Path) -> dict:
    """Pass one: what this recording actually is.

    Single-pass loudnorm guesses these while it runs, which is why it misses.
    """
    import json as _json
    import re as _re
    import subprocess

    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", str(audio),
         "-af", f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}"
                ":print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, check=False)
    match = _re.search(r"\{[^{}]*input_i[^{}]*\}", proc.stdout + proc.stderr, _re.S)
    if not match:
        return {}
    try:
        return _json.loads(match.group(0))
    except ValueError:
        return {}


def loudnorm_filter(measured: dict, target_i: float = TARGET_I) -> str:
    """The audio chain: normalise to a measured target, then hard-limit peaks.

    Two passes rather than one, because loudnorm's first-pass estimate is what
    made the integrated loudness land correctly and the true peak land 0.5 dB
    over. The limiter afterwards is what actually guarantees the ceiling --
    loudnorm alone cannot, and for a recording with a wide crest (one narration
    measured -22.7 LUFS integrated with peaks at -0.95) nothing linear can hit
    both targets at once. Something has to reduce the crest; better a limiter
    that is asked to, than a normaliser that does it by accident.
    """
    base = f"loudnorm=I={target_i}:TP={TARGET_TP}:LRA={TARGET_LRA}"
    if measured.get("input_i"):
        base += (
            f":measured_I={measured['input_i']}"
            f":measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}"
            f":measured_thresh={measured['input_thresh']}"
            f":offset={measured.get('target_offset', 0)}"
            ":linear=false"
        )
    limiter = (f"alimiter=limit={LIMITER_CEILING}dB:level=disabled"
               ":attack=5:release=50")
    return f"{base},{limiter},aresample=48000"

#: ffmpeg's own colour flags. Encode-time only: passing them alongside
#: `-c:v copy` is rejected outright by some builds (exit 234), so the concat and
#: mux steps rely on the VUI that x264 wrote instead.
COLOUR_ARGS = [
    "-colorspace", "bt709", "-color_primaries", "bt709",
    "-color_trc", "bt709", "-color_range", "tv",
]
VIDEO_ARGS = [
    "-c:v", "libx264", "-preset", "slow", "-crf", "18",
    "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
    "-x264-params", X264_PARAMS,
    *COLOUR_ARGS,
]
AUDIO_ARGS = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]

#: Deliverable sizes, all 9:16. 1080x1920 is what the pipeline renders and what
#: `verify` checks; the other two are upscales of that same file, offered
#: because some platforms treat a larger upload more generously on the bitrate
#: ladder. They add no detail -- nothing here invents pixels -- so the master
#: stays the master.
RESOLUTIONS: dict[str, tuple[int, int]] = {
    "1080p": (1080, 1920),
    "2k": (1440, 2560),
    "4k": (2160, 3840),
}


def h264_level(height: int) -> str:
    """The lowest level that legally carries this frame size at 30 fps.

    4.1 tops out at 1080p; a 4K stream tagged 4.1 is out of spec and players
    are entitled to refuse it. 5.1 covers everything up to 4096x2176@30, which
    is both of the upscales.
    """
    return "4.1" if height <= 1920 else "5.1"


def upscale_cmd(ffmpeg: str, source: Path, out: Path, width: int, height: int) -> list[str]:
    """Scale an encoded reel up, re-encoding video and copying audio.

    Lanczos because it is the sharpest of ffmpeg's general resamplers and this
    is the one job it is for. The audio is stream-copied deliberately: it was
    normalised to -14 LUFS / -1.5 dBTP and verified there, and a second AAC
    pass would move it for no gain.
    """
    video_args = [a for a in VIDEO_ARGS]
    video_args[video_args.index("-level") + 1] = h264_level(height)
    # `slow` is right for the master, where the encoder is deciding how to
    # spend bits on real detail. Here the input is an already-compressed 1080
    # file resampled up: the result is smooth, cheap to encode, and carries no
    # detail a slower search could find. Measured on a 40 s reel at 4K: slow
    # did not finish in two minutes, veryfast took 72 s, and the two are
    # indistinguishable on resampled frames.
    video_args[video_args.index("-preset") + 1] = "veryfast"
    return [ffmpeg, "-y", "-v", "error", "-i", str(source),
            "-vf", f"scale={width}:{height}:flags=lanczos",
            *video_args, "-c:a", "copy", "-movflags", "+faststart", str(out)]


def raw_input_args(width: int, height: int, fps: int) -> list[str]:
    return ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{width}x{height}",
            "-r", str(fps), "-i", "-"]


def chunk_cmd(ffmpeg: str, out: Path, width: int, height: int, fps: int) -> list[str]:
    """Encode one video-only chunk from raw RGBA on stdin."""
    return [ffmpeg, "-y", "-v", "warning",
            *raw_input_args(width, height, fps),
            "-an", *VIDEO_ARGS, str(out)]


def concat_cmd(ffmpeg: str, list_file: Path, out: Path) -> list[str]:
    """Join chunks without re-encoding. Every chunk starts on an IDR frame."""
    return [ffmpeg, "-y", "-v", "warning",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", "-movflags", "+faststart", str(out)]


def mux_cmd(ffmpeg: str, video: Path, audio: Path, out: Path,
            audio_filter: str | None = None) -> list[str]:
    """Attach the mixed narration, normalised, without touching the video."""
    chain = audio_filter or f"{LOUDNORM},aresample=48000"
    return [ffmpeg, "-y", "-v", "warning",
            "-i", str(video), "-i", str(audio),
            "-filter_complex", f"[1:a]{chain}[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", *AUDIO_ARGS,
            "-movflags", "+faststart", "-shortest", str(out)]


def single_pass_cmd(
    ffmpeg: str, audio: Path, out: Path, width: int, height: int, fps: int
) -> list[str]:
    """The reference path: byte-for-byte `make.sh`'s command, frames on stdin.

    Kept so a chunked result can always be checked against the known-good one.
    """
    return [ffmpeg, "-y", "-v", "warning",
            *raw_input_args(width, height, fps),
            "-i", str(audio),
            "-filter_complex", f"[1:a]{LOUDNORM},aresample=48000[a]",
            "-map", "0:v", "-map", "[a]",
            *VIDEO_ARGS, *AUDIO_ARGS,
            "-movflags", "+faststart", "-shortest", str(out)]


def plan_chunks(total: float, chunk_seconds: float, fps: int) -> list[tuple[float, float]]:
    """Split [0, total] on GOP boundaries.

    The final chunk absorbs the remainder, so it is the only one that may not be
    a whole number of GOPs -- which is fine, nothing is concatenated after it.
    """
    if chunk_seconds < GOP_SECONDS or abs(chunk_seconds / GOP_SECONDS - round(chunk_seconds / GOP_SECONDS)) > 1e-6:
        raise ValueError(f"chunk_seconds must be a whole multiple of {GOP_SECONDS}")
    bounds: list[tuple[float, float]] = []
    start = 0.0
    while start < total - 1e-9:
        end = min(round(start + chunk_seconds, 6), total)
        # never leave a runt final chunk shorter than one GOP
        if 0 < total - end < GOP_SECONDS:
            end = total
        bounds.append((start, end))
        start = end
    return bounds


def expected_frames(total: float, fps: int) -> int:
    """What ffprobe's nb_frames must report. A mismatch means the pipe broke
    mid-render and the tail of the video is missing (DEVELOPMENT.md gotcha 8)."""
    return int(round(total * fps))


def measure_output(ffmpeg: str, media: Path) -> tuple[float, float]:
    """Integrated loudness and true peak of a finished file, via ebur128."""
    import re as _re
    import subprocess

    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", str(media),
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True, check=False)
    text = proc.stdout + proc.stderr
    loudness = _re.findall(r"I:\s*(-?\d+\.?\d*)\s*LUFS", text)
    peak = _re.findall(r"Peak:\s*(-?\d+\.?\d*)\s*dBFS", text)
    return (float(loudness[-1]) if loudness else 0.0,
            float(peak[-1]) if peak else 0.0)


def mux_normalised(ffmpeg: str, video: Path, audio: Path, out: Path, *,
                   run, attempts: int = 3) -> dict:
    """Mux the narration and land inside the loudness window, by measuring.

    A fixed filter cannot do this. The limiter costs some loudness, how much
    depends on the recording's crest, and AAC then adds a little peak back --
    so the only way to know where a file landed is to look at it. Each pass
    corrects the loudness target by the error the last one produced, which
    converges in one step for every recording measured here.

    `run` is the subprocess runner, injected so this stays testable without
    ffmpeg.
    """
    measured = measure_loudness(ffmpeg, audio)
    target = TARGET_I
    history: list[dict] = []

    for attempt in range(1, attempts + 1):
        chain = loudnorm_filter(measured, target)
        proc = run(mux_cmd(ffmpeg, video, audio, out, audio_filter=chain))
        if proc.returncode != 0 or not out.exists():
            raise EncodeError(f"audio mux failed:\n{proc.stderr.strip()}")

        loudness, peak = measure_output(ffmpeg, out)
        error = TARGET_I - loudness
        history.append({"attempt": attempt, "target_i": round(target, 2),
                        "measured_i": loudness, "measured_tp": peak})
        within = (abs(error) <= LOUDNESS_TOLERANCE and peak <= TARGET_TP)
        if within or attempt == attempts:
            return {"loudness": loudness, "true_peak": peak,
                    "ok": within, "passes": history}
        # aim as far the other way as this pass missed
        target += error
    return {"loudness": 0.0, "true_peak": 0.0, "ok": False, "passes": history}


def retime(audio: Path, factor: float) -> float:
    """Speed `audio` up by `factor` in place, pitch preserved. Returns the new
    duration in seconds.

    ffmpeg's atempo takes 0.5-2.0 per instance; the factors used here are
    small, so one instance does. Re-encoded at the narration's own settings
    (48 kHz mono mp3), which is what the joiner writes.
    """
    import subprocess
    import tempfile

    from app.render.workspace import ffmpeg_bin, ffprobe_bin

    factor = max(0.5, min(2.0, float(factor)))
    fd, tmp_name = tempfile.mkstemp(suffix=audio.suffix, dir=str(audio.parent))
    import os

    os.close(fd)
    tmp = Path(tmp_name)
    proc = subprocess.run(
        [ffmpeg_bin(), "-y", "-v", "error", "-i", str(audio),
         "-filter:a", f"atempo={factor:.4f}", "-ac", "1", "-ar", "48000", str(tmp)],
        capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise EncodeError(f"retime failed: {proc.stderr.strip()[-300:]}")
    tmp.replace(audio)
    probe = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(audio)],
        capture_output=True, text=True, check=False)
    return float(probe.stdout.strip() or 0.0)
