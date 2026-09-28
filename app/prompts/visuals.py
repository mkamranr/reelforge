"""Art direction for the generated pictures: the scene plan -> text-free shots.

A scene's `on_screen` line is written for the renderer, and it is full of
things only the renderer should draw -- code, labels, numbers, the word that
types itself in. An image model given that draws a screen full of glyphs it
cannot spell. So the directions are rewritten here into what a camera could
photograph, and the words the scene needs come back separately as a short
heading the renderer paints over the picture afterwards.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.content import ReelContent

SYSTEM = """\
You are the art director for a short vertical video about a software project.
A diffusion model will draw one still image per scene and a video model will
draw one short clip. Neither can render text. Your job is to describe, for each
scene, a picture that carries the idea WITHOUT any text in it, and separately
the few words that should be painted over it afterwards.

Rules for `still` and `clip`:
- Describe a physical, photographable subject: objects, materials, light,
  space, motion. Metaphor is good: a cookie jar on a shelf for "session",
  copper wire and glowing junctions for "connection pooling", a single clean
  key turning for "one method call".
- NEVER describe screens, monitors, terminals, code, user interfaces, windows,
  buttons, labels, captions, diagrams with words, documents, signs, logos, or
  anything with letters or digits on it. Do not mention the project's name.
- One subject, one idea, one camera move for the clip (slow push-in, dolly,
  orbit, tilt). Vertical 9:16 framing. Keep the middle third of the frame
  calm, because the heading is painted there.
- 25-45 words each. Concrete nouns, materials, light. No adjectives piled up.

Rules for `heading`:
- 2-5 words that the viewer should read over the picture: the scene's claim,
  in the narration's own words where possible. Title case. No full stop.

Rules for `query`:
- 2-4 words for searching a stock photography library for the same subject,
  used when the pictures are found rather than drawn.
- Plain concrete nouns and at most one adjective: "copper wire macro", "brass
  key slate", "glass office night". No style words, no camera words, no
  colours, no project name, no punctuation.
- Describe the SUBJECT of your `still`, not the mood of it. A library has
  millions of photographs of a thing and none of an atmosphere.

Return every scene you are given, in order.
"""


class SceneDirection(BaseModel):
    scene_index: int = Field(ge=1)
    still: str = Field(min_length=20, max_length=420)
    clip: str = Field(min_length=20, max_length=420)
    heading: str = Field(min_length=2, max_length=40)
    #: Optional: only a search-based visuals adapter reads it, and the stage
    #: derives one by rule when it is missing.
    query: str = Field(default="", max_length=60)


class ArtDirection(BaseModel):
    scenes: list[SceneDirection] = Field(min_length=1, max_length=8)


def build_art_direction_prompt(content: ReelContent, scene_indexes: list[int],
                               palette: str) -> tuple[str, str]:
    scenes = [s for s in content.scenes if s.index in scene_indexes]
    plan = "\n".join(
        f"Scene {s.index} -- {s.title}\n"
        f"  narration : {s.you_say}\n"
        f"  direction : {s.on_screen}\n"
        + (f"  b-roll    : {s.b_roll}\n" if s.b_roll.strip() else "")
        for s in scenes
    )
    user = f"""\
The project: {content.display_name} -- {content.tagline}
Audience: {content.audience}
Palette words the pictures should sit in: {palette}

SCENES
{plan}

For each scene above, give `still`, `clip`, `heading` and `query` as described.
Remember: nothing with text in the pictures -- the heading is painted on
afterwards.
"""
    return SYSTEM, user
