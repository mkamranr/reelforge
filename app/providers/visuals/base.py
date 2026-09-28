"""What an image/video generator looks like to the pipeline.

Two operations, both synchronous and slow: a still for a scene, and a short
clip. A clip is delivered as a directory of frames, not a video file, because
the reel is drawn frame by frame in PIL and a storyboard can only paste a
bitmap at time `t` -- the same reason the repo B-roll is one tall screenshot
rather than a screen recording (see app/render/reposhot.py).
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class VisualsError(RuntimeError):
    """Generation failed for a reason worth showing the user."""


@dataclass
class StillResult:
    path: Path
    width: int
    height: int
    seed: int
    prompt: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClipResult:
    #: directory of `%05d.jpg` frames at the reel's own size and frame rate
    frames_dir: Path
    fps: int
    frames: int
    seconds: float
    seed: int
    prompt: str
    #: the encoded clip as the generator produced it, kept for the bundle
    source: Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class AudioResult:
    #: 48 kHz stereo 16-bit wav, what the mixer reads
    path: Path
    seconds: float
    seed: int
    prompt: str
    #: the file as the generator produced it (mp3, flac...), kept for the bundle
    source: Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class VisualsProvider(abc.ABC):
    name = "base"
    #: whether `clip()` does anything. A stills-only setup is a normal one.
    supports_clips = False
    #: whether `audio()` does anything: music beds and sound-design one-shots.
    #: Not speech -- a text-to-audio model has no words; narration stays with
    #: the TTS providers.
    supports_audio = False

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings

    @abc.abstractmethod
    def still(self, prompt: str, out: Path, *, width: int, height: int,
              seed: int, negative: str = "", query: str = "") -> StillResult:
        """Generate one image and write it to `out` as PNG.

        `query` is the same picture in two or three words. A generator has no
        use for it and ignores it; an adapter that searches a stock library
        needs it, because `prompt` is written for a diffusion model and is far
        too long to match anything.
        """

    def clip(self, prompt: str, out_dir: Path, *, seconds: float, fps: int,
             width: int, height: int, seed: int, negative: str = "",
             query: str = "") -> ClipResult:
        """Generate one clip and extract its frames into `out_dir`."""
        raise VisualsError(f"the {self.name} adapter does not generate clips")

    def audio(self, prompt: str, out: Path, *, seconds: float, seed: int,
              category: str = "Music", negative: str = "", progress=None) -> AudioResult:
        """Generate music or a sound effect and write it to `out` as 48k wav."""
        raise VisualsError(f"the {self.name} adapter does not generate audio")

    def release(self) -> bool:
        """Let go of whatever the backend holds (VRAM). No-op by default."""
        return True

    @abc.abstractmethod
    def health(self) -> dict[str, Any]:
        """Reachability and what the server offers. Never generates anything."""
