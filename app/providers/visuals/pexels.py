"""Pexels: stock photography and footage, in place of a generator.

Every other visuals adapter *makes* a picture from a prompt. This one *finds*
one, which changes three things and nothing else:

- **The prompt is not the request.** A diffusion model wants 25-45 words of
  cinematic description; a search engine wants two or three nouns. The stage
  passes both -- `query` for the search, `prompt` kept for the record -- and
  the adapter falls back to squeezing a query out of the prompt only when it
  was given none.
- **The seed picks rather than generates.** A search returns a page of
  results and the seed chooses among them, so a re-run with an unchanged
  direction reproduces the same photograph, exactly as an unchanged prompt
  reproduces the same generation.
- **The result has an author.** Pexels asks for a link back and a credit, so
  every result carries the photographer and the page URL in its `meta`, and
  the package stage turns those into the delivery notes' credits.

There is no `audio()`: Pexels has no sound library, so a reel on this profile
keeps the synthesized cut sounds and gets no music bed.

The API is two endpoints -- `/v1/search` for photos, `/videos/search` for
clips -- and the key goes in a bare `Authorization` header, with no `Bearer`.
Free accounts get 200 requests an hour, which is roughly forty reels.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx

from app.providers.visuals.base import ClipResult, StillResult, VisualsError, VisualsProvider
from app.providers.visuals.media import extract_frames

PROBE_TIMEOUT = 5.0
API_BASE = "https://api.pexels.com"
#: A 4K thirty-second shot is a few hundred megabytes and five seconds of it
#: end up on screen. Anything larger is the wrong file, not a big one.
MAX_CLIP_BYTES = 120 * 1024 * 1024
#: Read in chunks rather than buffering: `response.content` on a video is how
#: a worker runs out of memory.
DOWNLOAD_CHUNK = 1 << 20
#: The reel is 1080 wide; a narrower source would be upscaled.
MIN_CLIP_WIDTH = 1080


def squeeze_query(text: str, *, words: int = 3) -> str:
    """A search term out of whatever we were given.

    The deterministic fallback for when the art director did not supply a
    `query` -- the LLM call is optional and can fail. The prompt's tail is
    boilerplate the stage appends to every one of them (framing, palette
    words, the style suffix), so only the leading clause is worth reading.
    """
    head = re.split(r"[.,]", (text or "").strip(), maxsplit=1)[0]
    found = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'-]+", head)
             if w.lower() not in _STOPWORDS]
    return " ".join(found[:words])


_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "into", "with",
    "for", "from", "by", "over", "under", "across", "through", "between",
    "is", "are", "was", "were", "be", "being", "been", "its", "it", "one",
    "single", "clean", "slow", "smooth", "cinematic", "photoreal", "vertical",
    "shot", "shallow", "soft", "bright", "dark", "well-lit", "empty", "plenty",
    "around", "camera", "movement", "move", "continuous", "framing", "frame",
    "composition", "middle", "kept", "calm", "depth", "field", "light",
    "lighting", "background", "foreground", "no", "not",
}


class PexelsProvider(VisualsProvider):
    name = "pexels"
    supports_clips = True
    #: Pexels is pictures. Narration stays with the TTS providers and the cut
    #: sounds stay synthesized.
    supports_audio = False

    def __init__(self, settings: dict[str, Any]):
        super().__init__(settings)
        self.base_url = str(settings.get("base_url") or API_BASE).rstrip("/")
        self.key_env = str(settings.get("api_key_env") or "PEXELS_API_KEY")
        self.orientation = str(settings.get("orientation") or "portrait")
        self.photo_size = str(settings.get("photo_size") or "large")
        self.video_quality = str(settings.get("video_quality") or "hd")
        self.pool = max(1, min(80, int(settings.get("pool") or 15)))
        self.clip_start = max(0.0, float(settings.get("clip_start") or 0.0))
        self.timeout = float(settings.get("request_timeout") or 60)
        #: ids already used this run, so two scenes never get the same picture
        self._used: set[int] = set()

        from app.config import secret

        self._key = secret(self.key_env) or ""
        headers = {"Authorization": self._key} if self._key else {}
        self._client = httpx.Client(base_url=self.base_url, headers=headers,
                                    timeout=httpx.Timeout(self.timeout, connect=10.0))
        # A SECOND client, with no Authorization header, for the CDN. The
        # files live on images.pexels.com and videos.pexels.com, and httpx
        # sends a client's default headers to an absolute URL too -- sharing
        # one client would hand the key to a host that never asked for it.
        self._cdn = httpx.Client(timeout=httpx.Timeout(self.timeout, connect=10.0),
                                 follow_redirects=True)

    # ------------------------------------------------------------ http ---
    def _search(self, path: str, query: str, **params) -> list[dict[str, Any]]:
        if not self._key:
            raise VisualsError(
                f"no Pexels API key: set {self.key_env} in the environment or on the "
                f"Settings page. A free key comes from https://www.pexels.com/api/"
            )
        try:
            response = self._client.get(path, params={"query": query, **params})
        except httpx.HTTPError as exc:
            raise VisualsError(f"{self.base_url} unreachable: {exc}") from exc
        if response.status_code == 401:
            raise VisualsError(f"Pexels rejected the key in {self.key_env} (HTTP 401)")
        if response.status_code == 429:
            raise VisualsError("Pexels rate limit reached (HTTP 429); a free key allows "
                               "200 requests an hour")
        if response.status_code >= 400:
            raise VisualsError(f"GET {path} -> HTTP {response.status_code}: "
                               f"{response.text[:200]}")
        body = response.json()
        return list(body.get("photos") or body.get("videos") or [])

    def _pick(self, results: list[dict[str, Any]], seed: int) -> dict[str, Any]:
        """The seed chooses, skipping anything this run already used.

        Deterministic on purpose: the stage's reuse check keeps an asset whose
        prompt and seed are unchanged, and that promise only holds if the same
        seed lands on the same photograph.
        """
        pool = [r for r in results if r.get("id") not in self._used] or results
        chosen = pool[seed % len(pool)]
        self._used.add(chosen.get("id"))
        return chosen

    def _find(self, path: str, query: str, prompt: str, seed: int,
              **params) -> tuple[dict[str, Any], str]:
        """Search, widening the query once before giving up.

        A three-word query can be too specific for a stock library. Dropping
        to the first two words finds something related far more often than it
        finds nothing, and an empty-handed asset is a hole in the reel.
        """
        term = (query or "").strip() or squeeze_query(prompt)
        if not term:
            raise VisualsError("no search term: the scene direction yielded no keywords")
        results = self._search(path, term, **params)
        if not results:
            widened = " ".join(term.split()[:2])
            if widened and widened != term:
                results = self._search(path, widened, **params)
                if results:
                    term = widened
        if not results:
            raise VisualsError(f"Pexels has nothing for {term!r}")
        return self._pick(results, seed), term

    def _download(self, url: str, target: Path, *, limit: int | None = None) -> int:
        """Stream a CDN file to disk. Returns the bytes written."""
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with self._cdn.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise VisualsError(f"download failed: HTTP {response.status_code}")
                declared = int(response.headers.get("content-length") or 0)
                if limit and declared > limit:
                    raise VisualsError(f"the file is {declared // 2**20} MB, over the "
                                       f"{limit // 2**20} MB ceiling")
                with target.open("wb") as fh:
                    for chunk in response.iter_bytes(DOWNLOAD_CHUNK):
                        written += len(chunk)
                        if limit and written > limit:
                            raise VisualsError(f"the file is larger than the "
                                               f"{limit // 2**20} MB ceiling")
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            target.unlink(missing_ok=True)
            raise VisualsError(f"download failed: {exc}") from exc
        except VisualsError:
            target.unlink(missing_ok=True)
            raise
        return written

    # ------------------------------------------------------- generation ---
    def still(self, prompt: str, out: Path, *, width: int, height: int,
              seed: int, negative: str = "", query: str = "",
              progress=None) -> StillResult:
        photo, term = self._find("/v1/search", query, prompt, seed,
                                 orientation=self.orientation, size=self.photo_size,
                                 per_page=self.pool)
        if progress:
            progress(f"pexels: {term!r} -> photo {photo.get('id')} "
                     f"by {photo.get('photographer')}")
        src = photo.get("src") or {}
        url = _sized(src, width, height)
        if not url:
            raise VisualsError(f"photo {photo.get('id')} carries no usable size")
        raw = out.with_suffix(".raw.jpg")
        self._download(url, raw)
        from PIL import Image

        with Image.open(raw) as image:
            image = image.convert("RGB")
            out.parent.mkdir(parents=True, exist_ok=True)
            image.save(out, "PNG")
            size = image.size
        raw.unlink(missing_ok=True)
        return StillResult(path=out, width=size[0], height=size[1], seed=seed, prompt=prompt,
                           meta=_credit(photo, term, kind="photo"))

    def clip(self, prompt: str, out_dir: Path, *, seconds: float, fps: int,
             width: int, height: int, seed: int, negative: str = "", query: str = "",
             progress=None) -> ClipResult:
        video, term = self._find("/videos/search", query, prompt, seed,
                                 orientation=self.orientation, size="medium",
                                 per_page=self.pool)
        chosen = _best_file(video.get("video_files") or [], self.video_quality)
        if not chosen:
            raise VisualsError(f"video {video.get('id')} has no file at least "
                               f"{MIN_CLIP_WIDTH} px wide")
        if progress:
            progress(f"pexels: {term!r} -> video {video.get('id')} by "
                     f"{video.get('user', {}).get('name')}, "
                     f"{chosen.get('width')}x{chosen.get('height')}")
        out_dir.mkdir(parents=True, exist_ok=True)
        source = out_dir.parent / f"{out_dir.name}.mp4"
        self._download(chosen["link"], source, limit=MAX_CLIP_BYTES)

        # Start a little way in: stock clips routinely open on a fade or a
        # settling camera, and the reel cuts to this frame with no run-up.
        available = float(video.get("duration") or 0) or seconds
        start = self.clip_start if available - self.clip_start >= seconds else 0.0
        count = extract_frames(source, out_dir, fps=fps, width=width, height=height,
                               seconds=seconds, start=start)
        meta = _credit(video, term, kind="video")
        meta |= {"source_width": chosen.get("width"), "source_height": chosen.get("height"),
                 "source_fps": chosen.get("fps"), "start": start,
                 "source_seconds": video.get("duration")}
        return ClipResult(frames_dir=out_dir, fps=fps, frames=count, seconds=count / fps,
                          seed=seed, prompt=prompt, source=source, meta=meta)

    def release(self) -> bool:
        """Nothing to hand back.

        False rather than the inherited True on purpose: the stage announces
        "models unloaded, VRAM released" when this returns True, and there is
        no GPU anywhere in this adapter to say that about.
        """
        return False

    # ----------------------------------------------------------- health ---
    def health(self) -> dict[str, Any]:
        """Reachable and authenticated are different questions.

        A missing key is not a network problem, but on a hosted API it is
        still fatal -- every search would come back 401 -- so it is reported
        as unreachable with the reason, the way the LLM adapters do it.
        """
        out: dict[str, Any] = {"provider": "pexels", "base_url": self.base_url,
                               "api_key_env": self.key_env,
                               "authenticated": bool(self._key),
                               "supports_clips": True, "supports_audio": False}
        if not self._key:
            out |= {"reachable": False,
                    "error": f"no key is set for {self.key_env}, so every search would "
                             f"fail with 401. A free key comes from "
                             f"https://www.pexels.com/api/"}
            return out
        try:
            response = self._client.get("/v1/search",
                                        params={"query": "nature", "per_page": 1},
                                        timeout=PROBE_TIMEOUT)
        except Exception as exc:
            out |= {"reachable": False,
                    "error": f"{self.base_url} unreachable: {str(exc)[:160]}"}
            return out
        out["reachable"] = True
        if response.status_code == 401:
            out |= {"authenticated": False,
                    "error": f"Pexels rejected the key in {self.key_env} (HTTP 401)"}
            return out
        if response.status_code >= 400:
            out["error"] = f"HTTP {response.status_code}: {response.text[:160]}"
            return out
        remaining = response.headers.get("X-Ratelimit-Remaining")
        if remaining is not None:
            out["quota_remaining"] = _int_or_none(remaining)
            out["quota_limit"] = _int_or_none(response.headers.get("X-Ratelimit-Limit"))
            out["quota_reset"] = _int_or_none(response.headers.get("X-Ratelimit-Reset"))
        out["note"] = ("Stock footage, not generated imagery: the search term matters "
                       "more than the prompt, and no music bed or sound effects come "
                       "from this profile.")
        return out


def _sized(src: dict[str, Any], width: int, height: int) -> str:
    """The CDN URL for a photo at exactly the size the frame wants.

    None of the named sizes is big enough for a 1080-wide reel: `portrait` is
    800x1200 and `large2x` 867x1300, so both would be upscaled and visibly
    soft. `original` is the full picture -- 4128x6192 and 3.5 MB in the case
    that found this -- which is the opposite problem.

    Pexels' CDN resizes on request, the same way the named sizes are built, so
    asking `original` for the exact frame gives a sharp, already-cropped
    picture at about 150 KB. The named sizes stay as the fallback for a
    response shaped differently than expected.
    """
    original = src.get("original")
    if original:
        join = "&" if "?" in original else "?"
        return (f"{original}{join}auto=compress&cs=tinysrgb"
                f"&fit=crop&w={int(width)}&h={int(height)}")
    return src.get("portrait") or src.get("large2x") or src.get("large") or ""


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _best_file(files: list[dict[str, Any]], quality: str) -> dict[str, Any] | None:
    """The smallest file that still fills the frame.

    Sorted by width ascending, so a 1080-wide clip wins over the 4K version of
    the same shot: both are centre-cropped to 1080x1920 and only one of them
    is a 300 MB download.
    """
    usable = [f for f in files
              if f.get("link") and int(f.get("width") or 0) >= MIN_CLIP_WIDTH]
    if not usable:
        # nothing wide enough; take the widest there is rather than nothing
        usable = [f for f in files if f.get("link")]
    if not usable:
        return None
    preferred = [f for f in usable if f.get("quality") == quality] or usable
    return sorted(preferred, key=lambda f: int(f.get("width") or 0))[0]


def _credit(item: dict[str, Any], query: str, *, kind: str) -> dict[str, Any]:
    """What the licence asks us to keep: who made it and where it lives.

    A photo names its photographer at the top level; a video nests the same
    person under `user`.
    """
    user = item.get("user") or {}
    return {
        "source": "pexels",
        "kind": kind,
        "id": item.get("id"),
        "photographer": item.get("photographer") or user.get("name") or "",
        "photographer_url": item.get("photographer_url") or user.get("url") or "",
        "url": item.get("url") or "",
        "query": query,
    }
