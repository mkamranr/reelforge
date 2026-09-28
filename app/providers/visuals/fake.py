"""A generator that draws placeholders instantly. For tests, and for seeing
the stage plumbing work without a GPU anywhere."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.providers.visuals.base import AudioResult, ClipResult, StillResult, VisualsProvider


def _colour(seed: int) -> tuple[int, int, int]:
    """Bright enough to count as a picture: the smoke check calls a frame
    sparse below luminance 150, and a placeholder must not trip that."""
    digest = hashlib.sha1(str(seed).encode()).digest()
    return 120 + digest[0] % 120, 120 + digest[1] % 120, 140 + digest[2] % 110


class FakeProvider(VisualsProvider):
    name = "fake"
    supports_clips = True
    supports_audio = True

    def __init__(self, settings: dict[str, Any]):
        super().__init__(settings)
        self.calls: list[dict[str, Any]] = []

    def still(self, prompt: str, out: Path, *, width: int, height: int,
              seed: int, negative: str = "", query: str = "",
              progress=None) -> StillResult:
        from PIL import Image, ImageDraw

        self.calls.append({"kind": "still", "prompt": prompt, "seed": seed})
        # contrast on purpose: a real photograph has darks and lights, and the
        # smoke check's sparse rung measures exactly that
        image = Image.new("RGB", (width, height), _colour(seed))
        draw = ImageDraw.Draw(image)
        for i in range(0, height, 64):
            tone = 20 if (i // 64) % 2 else 235
            draw.line((0, i, width, i + width // 3), fill=(tone, tone, tone), width=9)
        draw.ellipse((width // 4, height // 3, 3 * width // 4, 2 * height // 3),
                     fill=(250, 250, 255), outline=(10, 10, 14), width=12)
        out.parent.mkdir(parents=True, exist_ok=True)
        image.save(out, "PNG")
        return StillResult(path=out, width=width, height=height, seed=seed, prompt=prompt)

    def clip(self, prompt: str, out_dir: Path, *, seconds: float, fps: int,
             width: int, height: int, seed: int, negative: str = "", query: str = "",
             progress=None) -> ClipResult:
        from PIL import Image, ImageDraw

        self.calls.append({"kind": "clip", "prompt": prompt, "seed": seed})
        out_dir.mkdir(parents=True, exist_ok=True)
        count = max(1, int(round(seconds * fps)))
        base = _colour(seed)
        for index in range(count):
            # a picture, not a flat card: real clips carry contrast, and the
            # smoke check's sparse rung is right to flag a frame without any
            image = Image.new("RGB", (width, height), base)
            draw = ImageDraw.Draw(image)
            for band in range(0, height, height // 8):
                tone = 30 + (band * 7 + seed * 13) % 200
                draw.rectangle((0, band, width, band + height // 16),
                               fill=(tone, tone, min(255, tone + 30)))
            y = int(height * index / count)
            draw.rectangle((0, y, width, y + 40), fill=(245, 245, 250))
            image.save(out_dir / f"{index + 1:05d}.jpg", "JPEG", quality=80)
        return ClipResult(frames_dir=out_dir, fps=fps, frames=count, seconds=count / fps,
                          seed=seed, prompt=prompt)

    def audio(self, prompt: str, out: Path, *, seconds: float, seed: int,
              category: str = "Music", negative: str = "", progress=None) -> AudioResult:
        """A soft two-tone pad (music) or a short decaying blip (one-shot),
        48 kHz stereo wav -- enough to hear the mixer working."""
        import wave

        import numpy as np

        self.calls.append({"kind": "audio", "prompt": prompt, "seed": seed, "category": category})
        sr = 48000
        n = int(seconds * sr)
        t = np.arange(n) / sr
        rng = np.random.RandomState(seed % (2 ** 32))
        if category == "Music":
            f = 110.0 * (1 + rng.randint(0, 4) / 4)
            sig = 0.25 * np.sin(2 * np.pi * f * t) + 0.15 * np.sin(2 * np.pi * f * 1.5 * t + 0.3)
            sig *= 0.5 + 0.5 * np.sin(2 * np.pi * 0.25 * t) ** 2
        else:
            sig = np.sin(2 * np.pi * 880.0 * t) * np.exp(-t / 0.06)
        out.parent.mkdir(parents=True, exist_ok=True)
        frames = (np.clip(np.stack([sig, sig], 1), -1, 1) * 32767).astype(np.int16)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(frames.tobytes())
        return AudioResult(path=out, seconds=seconds, seed=seed, prompt=prompt)

    def health(self) -> dict[str, Any]:
        return {"provider": "fake", "reachable": True}
