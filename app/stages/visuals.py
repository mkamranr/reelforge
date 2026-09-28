"""Stage 4: generated imagery.

Turns the script's art direction into pictures. Each scene already carries an
`on_screen` line (what is drawn) and a `b_roll` line (a shot description the
content prompt asks for and nothing read until now). A still is made from the
first, a clip from the second, and both get the template's palette words and
one style suffix so a reel's assets share a look.

What this writes, all under `<job>/visuals/`:

    visuals.json          the plan and what came of it -- every prompt, seed,
                          file and failure, so a still can be regenerated
                          with the prompt edited
    still-<n>.png         as the generator produced it
    clip-<n>.mp4          as the generator produced it
    clip-<n>/00001.jpg    frames at the reel's size and frame rate

and into `<job>/images/` the prepared `gen-still-<n>.png`, sized the way an
uploaded screenshot is so the screen catalogue treats it the same way.

Per-asset failure is recorded and the rest continue; the stage fails only
when everything it was asked for failed. A reel with two of three stills is
a reel; a stage that refuses to finish over one bad seed is not.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.images import Crop, FRAME_H, FRAME_W, prepare
from app.models.content import ReelContent, Scene
from app.providers.visuals import VisualsError, VisualsProvider

Progress = Callable[[str], None]

#: the reel's own frame rate; clip frames are extracted at this
REEL_FPS = 30


#: The storyboards' sound-design kinds (see video/sfx.py), with a description
#: each for the one-shot generator. Three sets, one per design family; the
#: descriptions keep the registers apart the way the synthesized ones do --
#: Bloom owns the low end, Ledger is dry and mid-high, Slab is mid and musical.
SFX_KINDS: dict[str, str] = {
    # Bloom
    "thump": "deep soft low-frequency cinematic impact hit, short, dry, sub bass, no tail",
    "swish": "short airy whoosh transition swipe, soft, fast, filtered noise",
    "tick": "tiny user interface blip, soft high click, extremely short",
    "sweep": "two second rising filtered noise riser, cinematic build-up, tension",
    # Ledger
    "click": "dry damped mechanical click, single, very short, no bass, like a relay",
    "rule": "quick pen stroke drawn on paper, short dry scratch",
    "latch": "small metallic latch seating, two-tone mechanical clack, short",
    "shift": "short upward pitched slide blip, small interface sound, no bass",
    # Slab
    "slam": "tight mid-range impact, soft book slam on a desk, short, no sub bass",
    "paper": "single sheet of paper page turn, quick flip, short",
    "chime": "soft two-note bell chime, gentle reveal, short decay",
    "riser": "very short mid-frequency whoosh riser, half a second",
}
#: seconds asked of the generator per one-shot; trimmed to the event's `dur`
SFX_SECONDS = 2.0
#: the bed is generated once, long enough for the longest reel, and placed
MUSIC_SECONDS = 60.0


@dataclass
class Asset:
    kind: str                  # still | clip | music
    index: int                 # 1-based within its kind
    scene_index: int
    scene_title: str
    prompt: str
    seed: int
    #: a few keywords for an adapter that searches a library rather than
    #: drawing; ignored by a generator, which reads `prompt`
    query: str = ""
    #: the words the renderer paints over the picture afterwards
    heading: str = ""
    ok: bool = False
    error: str = ""
    #: still: the raw generated PNG; clip: the mp4
    source: str = ""
    #: still: the prepared PNG under <job>/images; clip: the frames directory
    file: str = ""
    width: int = 0
    height: int = 0
    fit: str = "full"
    fps: int = REEL_FPS
    frames: int = 0
    seconds: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------- prompts ---
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]+")

#: Phrases in an `on_screen` line that describe the renderer's own typography
#: rather than a picture. A still of "the word requests types itself in
#: indigo" is a picture of text, which diffusion models draw badly and which
#: the storyboard draws itself anyway. Whole sentences go when they are about
#: text; inside a sentence, the code and the UI furniture go.
_TYPOGRAPHY = re.compile(
    r"(the (word|text|label|headline|title|number|counter)s? [^.]*?\.)|"
    r"([^.]*\b(types? itself|appears in \w+ at the bottom|in indigo|in teal|in amber"
    r"|strikes? through|a list of|the text|reads)\b[^.]*\.)",
    re.IGNORECASE,
)
#: Anything a camera could not photograph without letters on it.
_TEXTY = re.compile(
    r"`[^`]*`|\b[\w.]+\([^)]*\)|[\w./-]+\.(py|js|ts|json|yaml|yml|md|txt|sh)\b"
    r"|\b(code|terminal|console|shell|prompt|editor|IDE|screenshot|screen|monitor|window"
    r"|dashboard|UI|interface|button|menu|label|caption|subtitle|headline|title|text|font"
    r"|typography|logo|wordmark|diagram|chart|graph|table|list|bullet|checklist|document"
    r"|README|markdown|command|CLI|URL|link|query string|form data|JSON|API|HTTP|TCP|GET|POST"
    r"|split screen|left side|right side|shows?|showing|displays?|typing|types)\b"
    r"|'[^']{1,40}'|\"[^\"]{1,40}\"|\b\d[\d,.]*\b",
    re.IGNORECASE,
)


def palette_words(theme) -> str:
    """A few colour words from the theme, so the assets sit on the reel."""
    def name(rgb) -> str:
        r, g, b = rgb
        mx, mn = max(rgb), min(rgb)
        if mx - mn < 28:
            return "charcoal" if mx < 90 else ("grey" if mx < 190 else "white")
        if b >= r and b >= g:
            return "indigo" if r > g else ("teal" if g > 0.7 * b else "deep blue")
        if r >= g and r >= b:
            return "amber" if g > 0.5 * r else ("rose" if b > 0.5 * r else "red")
        return "green" if b < 0.7 * g else "teal"

    return f"{name(theme.accent)} accent light, {name(theme.support)} highlights, {name(theme.bg)} ground"


def clean_direction(text: str) -> str:
    """The deterministic fallback for the art director.

    A direction that mentions anything text-bearing is discarded whole, not
    trimmed: what survives trimming ("the resulting request with
    automatically appended") still names things the model spells out. Only a
    direction that was photographable to begin with passes.
    """
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    if _TYPOGRAPHY.search(text + ".") or _TEXTY.search(text):
        return ""
    words = re.findall(r"[A-Za-z][A-Za-z'-]+", text)
    if len(words) < 4:
        return ""
    return text.rstrip(".")


#: Appended to the workflows' own negative prompts for stills and clips, so
#: the model is told twice that letters are not wanted.
TEXT_NEGATIVE = ("text, letters, words, typography, captions, subtitles, watermark, logo, "
                 "signage, user interface, screen, monitor, code, numbers, diagram")


#: Stand-ins by scene role, for when a direction is all code and labels and
#: no art director answered. Dull on purpose: a plain object under good light
#: with nothing written on it beats a busy picture with gibberish in it.
_METAPHORS = {
    "hook": "a single brass key resting on dark slate, one hard beam of light across it",
    "what": "a clean machined metal block with one glowing seam, on a dark surface",
    "mechanism": "copper wires meeting at a glowing junction inside a dark housing, macro",
    "close": "a row of identical matte boxes with one lit from inside, dark studio",
}


def metaphor_for(scene: Scene, content: ReelContent) -> str:
    """When nothing photographable survives the cleaning."""
    title = scene.title.lower()
    for key, shot in _METAPHORS.items():
        if key in title:
            return shot
    return ("a single clean physical object standing for the idea, on a dark surface, "
            "dramatic side light, plenty of empty space around it")


def still_prompt(scene: Scene, content: ReelContent, style: str,
                 direction: str | None = None) -> str:
    """`direction` is the art director's text-free shot; without one, the
    scene's own direction is cleaned, and a metaphor stands in when nothing
    photographable is left. The project is named nowhere -- a name is text."""
    direction = direction or clean_direction(scene.b_roll) or clean_direction(scene.on_screen) \
        or metaphor_for(scene, content)
    return (f"{direction}. Vertical composition, the middle of the frame kept calm, "
            f"{palette_words(content.theme)}, {style}")


def clip_prompt(scene: Scene, content: ReelContent, style: str,
                direction: str | None = None) -> str:
    direction = direction or clean_direction(scene.b_roll) or clean_direction(scene.on_screen) \
        or ("slow cinematic camera move over " + metaphor_for(scene, content))
    return (f"{direction}. Slow, smooth camera movement, one continuous shot, "
            f"vertical 9:16 framing, {palette_words(content.theme)}, {style}")


def art_direct(content: ReelContent, scene_indexes: list[int], llm, *,
               progress: Progress | None = None) -> dict[int, dict]:
    """Ask the model for text-free shots and headings; {} when it cannot.

    One call for every scene that gets a picture. A failure here is never a
    failure of the stage: the deterministic cleaning takes over, and the
    reel still gets pictures, only with less judgement behind the prompts.
    """
    from app.prompts.visuals import ArtDirection, build_art_direction_prompt

    if not scene_indexes:
        return {}
    system, user = build_art_direction_prompt(content, scene_indexes, palette_words(content.theme))
    try:
        result = llm.complete(system=system, user=user, schema=ArtDirection,
                              max_tokens=2000, temperature=0.5)
    except Exception as exc:
        if progress:
            progress(f"art direction skipped ({type(exc).__name__}: {str(exc)[:120]}); "
                     "prompts cleaned by rule instead")
        return {}
    out = {}
    for item in result.parsed.scenes:
        if item.scene_index in scene_indexes:
            out[item.scene_index] = {"still": item.still.strip(), "clip": item.clip.strip(),
                                     "heading": item.heading.strip().rstrip("."),
                                     "query": item.query.strip().strip(".,").lower()}
    if progress:
        progress(f"art direction: {len(out)} scene(s) described as text-free shots")
    return out


def search_query(scene: Scene, content: ReelContent, direction: str | None = None) -> str:
    """A few keywords for a stock library, when nothing better was supplied.

    The art director writes a `query` per scene and it is much the better
    term; this is what happens when that call is switched off or fails. The
    shot description is already text-free and photographable, so the job is
    only to cut it down to nouns a search engine can match.
    """
    from app.providers.visuals.pexels import squeeze_query

    source = (direction or clean_direction(scene.b_roll) or clean_direction(scene.on_screen)
              or metaphor_for(scene, content))
    return squeeze_query(source)


def cover_query(content: ReelContent) -> str:
    """The backdrop's search term: the cover's motif, in plain nouns."""
    motif = {"plugins": "modular blocks abstract", "artboards": "translucent layers abstract",
             "flow": "light ribbons abstract", "swarm": "glowing particles abstract"}
    return motif.get(content.cover.motif, "abstract dark texture")


def cover_prompt(content: ReelContent, style: str) -> str:
    cover = content.cover
    motif = {"plugins": "a lattice of glowing modular blocks", "artboards": "layered translucent panes",
             "flow": "flowing ribbons of light", "swarm": "a swarm of luminous particles"}
    return (f"Abstract background art: {motif.get(cover.motif, 'soft abstract light')}, "
            f"evoking {content.tagline.lower()}. Large empty dark areas in the middle for "
            f"text overlay, subject matter kept to the edges, vertical 9:16, "
            f"{palette_words(content.theme)}, {style}")


def music_prompt(content: ReelContent, hint: str, seconds: float = MUSIC_SECONDS) -> str:
    return (f"{hint}, evoking {content.tagline.lower()}. Instrumental, no vocals, no lyrics, "
            f"steady tempo, smooth and consistent dynamics, background music under a "
            f"spoken voiceover, subtle, no sudden changes, no drum fills. BPM: 88. "
            f"Length: {int(seconds)} seconds")


def seed_for(job_id: str, kind: str, index: int, prompt: str) -> int:
    """Stable per asset, so a re-run with the same prompt reproduces the same
    picture and an edited prompt gets a fresh one."""
    digest = hashlib.sha256(f"{job_id}|{kind}|{index}|{prompt}".encode()).digest()
    return int.from_bytes(digest[:6], "big")


# ---------------------------------------------------------------- plan ---
def body_scenes(content: ReelContent) -> list[Scene]:
    """Scenes worth a picture: everything but the hook and the close.

    The opener is the storyboard's own typography at frame 0 by rule, and
    the closer is the end card. With only three scenes the middle one is it.
    """
    scenes = list(content.scenes)
    if len(scenes) <= 2:
        return scenes
    return scenes[1:-1]


def plan(content: ReelContent, *, job_id: str, stills: int, clips: int, style: str,
         still_fit: str, music: str | None = None,
         directions: dict[int, dict] | None = None) -> list[Asset]:
    """Which assets to make, for which scenes, with which prompts.

    `music` is the template's hint; given, one bed is planned for the reel.
    `directions` is the art director's output per scene index; a scene
    without one falls back to its cleaned direction."""
    directions = directions or {}
    assets: list[Asset] = []
    if music:
        prompt = music_prompt(content, music)
        assets.append(Asset(kind="music", index=1, scene_index=0, scene_title="the whole reel",
                            prompt=prompt, seed=seed_for(job_id, "music", 1, prompt),
                            seconds=MUSIC_SECONDS))
    candidates = body_scenes(content)
    if not candidates:
        return assets

    # clips go to the scenes with a written b_roll shot first, then the
    # longest ones -- a mechanism scene has more to show than a definition
    ranked = sorted(candidates, key=lambda s: (not s.b_roll.strip(), -(s.t_end - s.t_start)))
    clip_scenes = ranked[:max(0, clips)]
    for n, scene in enumerate(clip_scenes, 1):
        d = directions.get(scene.index) or {}
        prompt = clip_prompt(scene, content, style, d.get("clip"))
        assets.append(Asset(kind="clip", index=n, scene_index=scene.index, scene_title=scene.title,
                            prompt=prompt, seed=seed_for(job_id, "clip", n, prompt),
                            query=d.get("query") or search_query(scene, content, d.get("clip")),
                            width=FRAME_W, height=FRAME_H,
                            heading=d.get("heading") or scene.title))

    # stills for the remaining scenes in script order, so the first body
    # scene gets one before the third does
    # stills take only the scenes no clip covers: a still beside the same
    # scene's clip is the same idea on screen twice under the same heading
    taken = {s.index for s in clip_scenes}
    still_scenes = [s for s in candidates if s.index not in taken][:max(0, stills)]
    for n, scene in enumerate(still_scenes, 1):
        d = directions.get(scene.index) or {}
        prompt = still_prompt(scene, content, style, d.get("still"))
        assets.append(Asset(kind="still", index=n, scene_index=scene.index, scene_title=scene.title,
                            prompt=prompt, seed=seed_for(job_id, "still", n, prompt), fit=still_fit,
                            query=d.get("query") or search_query(scene, content, d.get("still")),
                            heading=d.get("heading") or scene.title))
    return assets


# ------------------------------------------------------------ running ---
def generate(assets: list[Asset], provider: VisualsProvider, *, job_root: Path,
             visuals_dir: Path, prepared_dir: Path, still_size: tuple[int, int],
             clip_seconds: float, negative: str = "", progress: Progress | None = None,
             reuse: dict[str, Asset] | None = None) -> list[Asset]:
    """Run the plan. `reuse` carries earlier results keyed by `kind-index`:
    an asset whose prompt and seed are unchanged and whose files still exist
    is kept rather than regenerated, which makes a retry after one failure
    cost one generation rather than all of them."""
    visuals_dir.mkdir(parents=True, exist_ok=True)
    prepared_dir.mkdir(parents=True, exist_ok=True)
    note = progress or (lambda _m: None)

    for asset in assets:
        key = f"{asset.kind}-{asset.index}"
        earlier = (reuse or {}).get(key)
        if earlier and earlier.ok and earlier.prompt == asset.prompt and earlier.seed == asset.seed \
                and (job_root / earlier.file).exists():
            note(f"{key}: unchanged, keeping the earlier result")
            asset.__dict__.update({k: v for k, v in asdict(earlier).items()})
            continue
        try:
            if asset.kind == "still":
                _make_still(asset, provider, job_root, visuals_dir, prepared_dir, still_size, negative, note)
            elif asset.kind == "music":
                _make_music(asset, provider, job_root, visuals_dir, note)
            else:
                _make_clip(asset, provider, job_root, visuals_dir, clip_seconds, negative, note)
            asset.ok = True
        except VisualsError as exc:
            asset.ok = False
            asset.error = str(exc)[:400]
            note(f"{key} failed: {asset.error}")
        except Exception as exc:  # a PIL or ffmpeg surprise is still one asset's problem
            asset.ok = False
            asset.error = f"{type(exc).__name__}: {str(exc)[:300]}"
            note(f"{key} failed: {asset.error}")
    return assets


def _make_still(asset, provider, job_root, visuals_dir, prepared_dir, size, negative, note):
    note(f"still {asset.index} for scene {asset.scene_index} ({asset.scene_title}): generating")
    raw = visuals_dir / f"still-{asset.index}.png"
    result = provider.still(asset.prompt, raw, width=size[0], height=size[1], seed=asset.seed,
                            negative=negative, query=asset.query, progress=note)
    prepared = prepared_dir / f"gen-still-{asset.index}.png"
    width, height = prepare(raw, prepared, fit=asset.fit, crop=Crop())
    asset.source = str(raw.relative_to(job_root)).replace("\\", "/")
    asset.file = str(prepared.relative_to(job_root)).replace("\\", "/")
    asset.width, asset.height = width, height
    asset.meta = dict(result.meta)


def _make_clip(asset, provider, job_root, visuals_dir, seconds, negative, note):
    note(f"clip {asset.index} for scene {asset.scene_index} ({asset.scene_title}): generating, "
         f"this is the slow one")
    frames_dir = visuals_dir / f"clip-{asset.index}"
    if frames_dir.exists():
        shutil.rmtree(frames_dir, ignore_errors=True)
    result = provider.clip(asset.prompt, frames_dir, seconds=seconds, fps=REEL_FPS,
                           width=FRAME_W, height=FRAME_H, seed=asset.seed,
                           negative=negative, query=asset.query, progress=note)
    asset.file = str(result.frames_dir.relative_to(job_root)).replace("\\", "/")
    asset.source = (str(result.source.relative_to(job_root)).replace("\\", "/")
                    if result.source else "")
    asset.fps, asset.frames, asset.seconds = result.fps, result.frames, round(result.seconds, 2)
    asset.width, asset.height = FRAME_W, FRAME_H
    asset.meta = dict(result.meta)


def _make_music(asset, provider, job_root, visuals_dir, note):
    note(f"music bed: generating {int(asset.seconds)} s")
    out = visuals_dir / "music-bed.wav"
    result = provider.audio(asset.prompt, out, seconds=asset.seconds, seed=asset.seed,
                            category="Music", progress=note)
    asset.file = str(result.path.relative_to(job_root)).replace("\\", "/")
    asset.source = (str(result.source.relative_to(job_root)).replace("\\", "/")
                    if result.source else "")
    asset.seconds = round(result.seconds, 2)
    asset.meta = dict(result.meta)


def ensure_sfx_library(provider: VisualsProvider, directory: Path, *,
                       kinds: dict[str, str] | None = None,
                       progress: Progress | None = None) -> dict[str, str]:
    """Generate any missing `<kind>.wav` one-shot into `directory`.

    A library, not a per-reel asset: the cut sounds are the same for every
    reel of a family, so they are made once and reused. Returns the kinds
    that failed, with the reason, so a caller can say so without stopping.
    """
    note = progress or (lambda _m: None)
    directory.mkdir(parents=True, exist_ok=True)
    failed: dict[str, str] = {}
    for kind, description in (kinds or SFX_KINDS).items():
        target = directory / f"{kind}.wav"
        if target.exists() and target.stat().st_size > 1000:
            continue
        note(f"sfx library: generating {kind}")
        try:
            provider.audio(f"{description}, isolated sound effect, clean, no music, no reverb tail",
                           target, seconds=SFX_SECONDS,
                           seed=seed_for("sfx-library", kind, 0, description),
                           category="One-shot", progress=note)
        except Exception as exc:
            failed[kind] = str(exc)[:200]
            note(f"sfx library: {kind} failed: {failed[kind]}")
            target.unlink(missing_ok=True)
    return failed


def music_for_render(assets: list[Asset], job_root: Path) -> Path | None:
    for asset in assets:
        if asset.kind == "music" and asset.ok and asset.file:
            path = job_root / asset.file
            if path.exists():
                return path
    return None


# ------------------------------------------------------------ reading ---
def read_assets(visuals_json: Path) -> list[Asset]:
    if not visuals_json.exists():
        return []
    try:
        data = json.loads(visuals_json.read_text(encoding="utf-8"))
    except ValueError:
        return []
    out = []
    for raw in data.get("assets") or []:
        try:
            out.append(Asset(**{k: v for k, v in raw.items() if k in Asset.__dataclass_fields__}))
        except TypeError:
            continue
    return out


def write_assets(visuals_json: Path, assets: list[Asset], *, provider: str, extra: dict | None = None) -> None:
    visuals_json.parent.mkdir(parents=True, exist_ok=True)
    payload = {"provider": provider, "assets": [asdict(a) for a in assets]}
    payload.update(extra or {})
    visuals_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def credits_from(assets: list[Asset], cover_meta: dict | None = None) -> list[dict]:
    """Who to credit for the pictures that actually shipped.

    A generated asset has nobody to credit and contributes nothing; a stock
    one carries its photographer and page URL in `meta`, put there by the
    adapter. The cover backdrop is made by the cover stage, which writes no
    visuals.json, so its credit arrives separately out of that stage's meta.
    """
    out: list[dict] = []
    seen: set[tuple] = set()
    entries = [(f"{a.kind} {a.index}", a.meta) for a in assets if a.ok]
    if cover_meta:
        entries.insert(0, ("cover backdrop", cover_meta))
    for label, meta in entries:
        if not meta or not meta.get("source"):
            continue
        key = (meta.get("source"), meta.get("id"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"asset": label, "source": meta["source"],
                    "photographer": meta.get("photographer") or "",
                    "photographer_url": meta.get("photographer_url") or "",
                    "url": meta.get("url") or ""})
    return out


def credit_line(credits: list[dict]) -> str:
    """One line for a caption: the library, and who to thank."""
    if not credits:
        return ""
    names: list[str] = []
    for credit in credits:
        name = credit.get("photographer")
        if name and name not in names:
            names.append(name)
    library = "Pexels" if credits[0].get("source") == "pexels" else credits[0].get("source", "")
    return f"Footage: {library}" + (f" ({', '.join(names)})" if names else "")


def stills_for_storyboard(assets: list[Asset], job_root: Path) -> list[dict]:
    """The same dicts `stage_images` builds for uploads, for generated stills."""
    out = []
    for asset in assets:
        if asset.kind != "still" or not asset.ok or not asset.file:
            continue
        path = job_root / asset.file
        if not path.exists():
            continue
        out.append({
            "file": path.name, "fit": asset.fit, "role": "generated",
            "position": "centre",
            "eyebrow": asset.scene_title.upper()[:34] or "A LOOK",
            "caption": "", "width": asset.width, "height": asset.height,
            "scene_index": asset.scene_index, "prompt": asset.prompt,
            "heading": asset.heading or asset.scene_title,
            "path": path,
        })
    return out


def clips_for_storyboard(assets: list[Asset], job_root: Path) -> list[dict]:
    out = []
    for asset in assets:
        if asset.kind != "clip" or not asset.ok or not asset.file:
            continue
        frames_dir = job_root / asset.file
        if not frames_dir.is_dir() or not any(frames_dir.glob("*.jpg")):
            continue
        out.append({
            "dir": frames_dir.name, "fps": asset.fps, "frames": asset.frames,
            "seconds": asset.seconds, "label": (asset.heading or asset.scene_title)[:40],
            "scene_index": asset.scene_index, "prompt": asset.prompt,
            "path": frames_dir,
        })
    return out
