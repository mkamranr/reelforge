"""Render generated content into the file formats this repo already uses.

The script `.txt`, the caption block and the `-reel-notes.md` were written by
hand for the first six reels and have a consistent shape. Reproducing it exactly
matters for more than tidiness: the script is the human-readable record of what
was claimed and when it was checked, and the notes file is what gets copied into
the upload forms.

Layout rules taken from the existing files: 58-column rules, ASCII body text
with `--` for dashes and `->` for arrows, a 12-character label column in the
fact sheet, and timecodes right-aligned to column 58.
"""
from __future__ import annotations

import textwrap

from app.models.content import ReelContent
from app.models.facts import FactsBundle
from app.models.platform import PlatformBundle

WIDTH = 58
RULE = "=" * WIDTH
THIN = "-" * WIDTH


def _ascii(text: str) -> str:
    """The scripts are ASCII in the body so the TTS reads punctuation predictably."""
    return (text.replace("—", "--").replace("–", "-")
                .replace("→", "->").replace("·", "-")
                .replace("‘", "'").replace("’", "'")
                .replace("“", '"').replace("”", '"')
                .replace("…", "..."))


def _wrap(text: str, indent: str = "  ", width: int = WIDTH) -> str:
    return "\n".join(
        textwrap.fill(line, width=width, initial_indent=indent,
                      subsequent_indent=indent) or indent.rstrip()
        for line in _ascii(text).splitlines() or [""]
    )


def _timecode(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def script_txt(content: ReelContent, facts: FactsBundle) -> str:
    """The `<slug>.txt` script: header, scenes, read-through, audio notes,
    fact sheet, accuracy notes."""
    out: list[str] = []
    title = f'VIDEO SCRIPT -- "{content.display_name}"'
    out += [RULE, title, f"({content.repo_url})", RULE]

    runtime = content.estimated_seconds()
    out += [
        f"Target runtime : {runtime:.0f} seconds",
        f"Narration      : {content.word_count} words in "
        f"{len(content.phrases)} phrases",
        f"Audience       : {_ascii(content.audience)}",
        "Voice          : measured, level, spoken to camera",
        f"Facts checked  : {content.facts_checked_at.strftime('%d %B %Y')}",
        "",
    ]

    for scene in content.scenes:
        head = f"SCENE {scene.index} -- {_ascii(scene.title).upper()}"
        stamp = f"({_timecode(scene.t_start)}-{_timecode(scene.t_end)})"
        out += [THIN, f"{head}{' ' * max(1, WIDTH - len(head) - len(stamp))}{stamp}", THIN]
        out += ["YOU SAY:", _wrap(f'"{scene.you_say}"', "  "), ""]
        out += ["ON SCREEN:", _wrap(scene.on_screen, "  "), ""]
        if scene.b_roll:
            out += ["B-ROLL:", _wrap(scene.b_roll, "  "), ""]

    out += [
        RULE,
        "READ-THROUGH  (narration only -- THIS is the text to feed",
        "               the audio model)",
        RULE,
    ]
    for paragraph in content.narration.split("\n\n"):
        out += [_wrap(paragraph, ""), ""]

    out += [RULE, "NOTES FOR THE AUDIO MODEL", RULE,
            "Read the READ-THROUGH only. Nothing else in this file.", ""]
    delivery = content.audio.delivery or [
        "Measured, level delivery. No hype, no upspeak.",
        "LEAVE A CLEAR PAUSE -- about one second -- at every blank line. "
        "Those gaps are how the video gets cut; without them the captions "
        "cannot be synced.",
        "No background music. Voice only.",
    ]
    for note in delivery:
        out.append(_wrap(note, "  ").replace("  ", "  · ", 1))
    for word, syllables in sorted(content.audio.pronunciations.items()):
        out.append(f"  · Read \"{word}\" as {syllables} syllables.")
    out += [f"  · Target {content.audio.target_seconds_min:.0f}-"
            f"{content.audio.target_seconds_max:.0f} seconds.", ""]

    out += [RULE, "FACT SHEET (for on-screen text / description box)", RULE]
    for row in content.fact_sheet:
        label = f"{row.label:<12}"
        wrapped = textwrap.fill(_ascii(row.value), width=WIDTH,
                                initial_indent="", subsequent_indent=" " * 15)
        out.append(f"{label}: {wrapped}")
    out.append("")

    if content.accuracy_notes:
        out += [RULE, "ACCURACY NOTES", RULE]
        for note in content.accuracy_notes:
            out.append(_wrap(note, "  ").replace("  ", "  · ", 1))
        if facts.github:
            out += ["", "  Re-check the volatile figures before recording:",
                    f"    gh api repos/{facts.github.full_name} --jq .stargazers_count"]
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def caption_block(content: ReelContent, platforms: PlatformBundle, number: int = 1,
                  credit: str = "") -> str:
    """One `captions.txt`-style block: heading, then the post-ready body.

    `credit` is the stock library's attribution line, appended to the body
    that gets pasted into the post. Empty for a reel that used no stock
    footage, which leaves the block byte-identical to what it always was.
    """
    heading = f"REEL {number} -- {content.display_name.upper()}  ({content.slug}-reel.png)"
    body = platforms.instagram.render_text()
    if credit:
        body = body.rstrip() + f"\n\n{credit}\n"
    return "\n".join([RULE, heading, RULE, "", body])


def notes_md(
    content: ReelContent,
    platforms: PlatformBundle,
    *,
    spec: dict,
    facts: FactsBundle,
    credits: list[dict] | None = None,
) -> str:
    """The `-reel-notes.md` delivery notes.

    Every figure in the spec paragraph comes from ffprobe on the real encode --
    never from what the storyboard intended.
    """
    slug = content.slug
    duration = spec.get("duration_seconds")
    frames = spec.get("frames")
    lufs = spec.get("integrated_lufs")

    lines = [
        f"# {slug}-reel.mp4 -- delivery notes",
        "",
        f"1080x1920 - {spec.get('fps', 30)} fps - **{duration:.2f} s** "
        f"({frames} frames) - H.264 High @ 4.1 - yuv420p - bt709 - "
        f"AAC-LC 192 kbps 48 kHz - **{lufs} LUFS** - +faststart"
        if duration is not None else "(spec pending -- render not yet verified)",
        "",
        f"Rebuild: `reelforge run {slug}` "
        f"(or `cd video && ./make.sh {slug} ../{slug}-reel.mp4`)",
        "",
        "## Instagram",
        "",
        "Caption: see the `REEL` block in `captions.txt`, or "
        "`metadata/instagram.txt`.",
        f"Cover: `{slug}-reel.png`",
        "",
        f"Alt text: {platforms.instagram.alt_text}",
        "",
        "Hashtags go in the caption, not the first comment.",
    ]
    if platforms.instagram.pinned_comment:
        lines += ["", f"Pinned comment: {platforms.instagram.pinned_comment}"]

    lines += [
        "",
        "## YouTube Shorts",
        "",
        "**Title**",
        "",
        "```",
        platforms.youtube.title,
        "```",
        "",
        "**Description**",
        "",
        "```",
        platforms.youtube.render_description().rstrip(),
        "```",
        "",
        "## Facebook",
        "",
        "```",
        platforms.facebook.render_text().rstrip(),
        "```",
        "",
        "## LinkedIn",
        "",
        "```",
        platforms.linkedin.render_text().rstrip(),
        "```",
    ]
    if platforms.linkedin.first_comment:
        lines += ["", f"First comment: `{platforms.linkedin.first_comment}`"]

    lines += ["", "## What the video does", ""]
    for scene in content.scenes:
        lines.append(f"- **{scene.title}** -- {scene.on_screen.splitlines()[0]}")

    if credits:
        # Pexels asks for a prominent link back and a credit to the
        # photographer. This is where it is paid, for every picture that
        # actually made it into the reel.
        library = "Pexels" if credits[0].get("source") == "pexels" else credits[0]["source"]
        home = "https://www.pexels.com" if credits[0].get("source") == "pexels" else ""
        lines += ["", "## Credits", "",
                  f"Stills and footage from **[{library}]({home})**."
                  if home else f"Stills and footage from **{library}**.", ""]
        for credit in credits:
            who = credit.get("photographer") or "an uncredited photographer"
            who = f"[{who}]({credit['photographer_url']})" if credit.get("photographer_url") else who
            where = f" -- {credit['url']}" if credit.get("url") else ""
            lines.append(f"- {credit['asset']}: {who}{where}")

    lines += ["", "## Accuracy", "",
              f"Facts checked {content.facts_checked_at.strftime('%d %B %Y')} "
              f"against {facts.primary_url}.", ""]
    for note in content.accuracy_notes:
        lines.append(f"- {note}")
    return "\n".join(lines).rstrip() + "\n"


def phrases_txt(content: ReelContent) -> str:
    """`video/phrases/<slug>.txt`: one line per voiced phrase, count is load-bearing."""
    header = (
        f"# {content.slug}.mp3 -- {len(content.phrases)} voiced phrases, "
        "one per line. align.py refuses to build unless this count matches "
        "what it detects."
    )
    return "\n".join([header, *content.phrase_lines()]) + "\n"


def metadata_files(content: ReelContent, platforms: PlatformBundle,
                   credit: str = "") -> dict[str, str]:
    """Per-platform bundle: JSON for machines, text for pasting into a form.

    `credit` is appended to every rendered body, because the attribution has
    to travel with the copy wherever it is pasted -- not only to the notes.
    """
    import json

    def _with_credit(text: str) -> str:
        return text.rstrip() + f"\n\n{credit}\n" if credit else text

    out: dict[str, str] = {}
    for post in platforms.all():
        name = post.platform
        payload = post.model_dump(mode="json")
        payload["rendered"] = _with_credit(post.render_text())
        if name == "youtube":
            payload["description"] = _with_credit(post.render_description())
        if credit:
            payload["credit"] = credit
        out[f"{name}.json"] = json.dumps(payload, indent=2, ensure_ascii=False)
        out[f"{name}.txt"] = _with_credit(post.render_text())
    out["hashtags.txt"] = " ".join(content.hashtags) + "\n"
    return out
