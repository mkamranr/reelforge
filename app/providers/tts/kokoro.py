"""Kokoro: self-hosted narration, free, no key, no GPU required.

Two servers speak this API and one adapter drives both:

- **Kokoro-TTS-OpenAPI** (github.com/mkamranr/Kokoro-TTS-OpenAPI) -- runs
  CPU-native on macOS with no container at all, 28 English voices, port 8080,
  voice list at `/voices`.
- **Kokoro-FastAPI** (`ghcr.io/remsky/kokoro-fastapi-cpu`, the `local-tts`
  compose profile) -- a container, more voices and more languages, port 8880,
  voice list at `/v1/audio/voices`.

Synthesis is identical: `POST /v1/audio/speech` with `{model, input, voice,
response_format, speed}`, OpenAI-shaped, returning audio bytes. That is why
the generic `local` adapter can already drive either. This one exists for the
two things generic cannot do:

- **It lists its voices.** The picker in Settings is filled from whichever
  server is configured, instead of a text box you have to type `af_heart`
  into from memory. Where the server describes its voices properly -- name,
  accent, gender, quality grade -- the grade is carried through, because an A
  voice and a C voice are audibly different and narration is the whole
  soundtrack. `local` stays generic for Piper and anything without a voice
  endpoint.
- **It checks the voice before a job spends twenty minutes.** A voice id that
  does not exist is not rejected at configuration time; it fails on the first
  `speak()` call, which is a dozen calls into a narration. `health()` compares
  the configured voice against the served list and says so up front.

Voices are named `<language><gender>_<name>`: `af_heart` is American female,
`bm_george` British male. Both servers also accept a weighted blend in the
same field -- `af_bella(2)+af_heart(1)` -- so the picker suggests rather than
constrains, and anything typed is passed through untouched.

Measured on this repo's development machine, CPU synthesis runs about 4x
slower than real time, so a 40 s narration is a few minutes spread over a
dozen per-phrase calls. Hence the generous default timeout.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.providers.tts.base import TTSError, TTSProvider

#: Seconds a reachability probe may take. These run when a form opens, so a
#: dead endpoint must fail fast rather than hold the page.
PROBE_TIMEOUT = 4.0
#: Kokoro-TTS-OpenAPI's default. Kokoro-FastAPI uses 8880 instead, so a
#: profile pointing at that one sets `base_url` accordingly.
DEFAULT_PORT = 8080
#: Where each server keeps its voice list. Kokoro-TTS-OpenAPI serves the
#: first, Kokoro-FastAPI the second; trying both is cheaper than making the
#: user tell us which flavour they run.
VOICE_PATHS = ("/voices", "/v1/audio/voices")
#: CPU synthesis measured at ~4x slower than real time, and a phrase is a
#: few seconds of audio, so a single call is tens of seconds.
DEFAULT_TIMEOUT = 300.0


class KokoroProvider(TTSProvider):
    name = "kokoro"
    output_mime = "audio/wav"

    def __init__(self, settings: dict[str, Any]):
        super().__init__(settings)
        self.base_url = str(settings.get("base_url")
                            or f"http://localhost:{DEFAULT_PORT}").rstrip("/")
        self.voice = str(settings.get("voice") or "af_heart")
        self.model = str(settings.get("model") or "kokoro")
        self.response_format = str(settings.get("response_format") or "wav")
        self.speed = float(settings.get("speed") or 1.0)
        self.language = str(settings.get("language") or "")
        self.timeout = float(settings.get("request_timeout") or DEFAULT_TIMEOUT)
        self.output_mime = f"audio/{'mpeg' if self.response_format == 'mp3' else self.response_format}"

        # Self-hosted and usually wide open, but Kokoro-TTS-OpenAPI will
        # demand a bearer token if KOKORO_API_KEY is set on the server. Named
        # here the same way every other provider names a key: by variable.
        headers = {}
        self.key_env = str(settings.get("api_key_env") or "")
        if self.key_env:
            from app.config import secret

            key = secret(self.key_env)
            if key:
                headers["Authorization"] = f"Bearer {key}"
        self._client = httpx.Client(base_url=self.base_url, headers=headers,
                                    timeout=self.timeout)

    # ------------------------------------------------------------ speak ---
    def speak(self, text: str, *, hints: dict[str, int] | None = None) -> bytes:
        payload: dict[str, Any] = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": self.response_format,
            "speed": self.speed,
        }
        if self.language:
            payload["lang_code"] = self.language
        try:
            response = self._client.post("/v1/audio/speech", json=payload)
        except httpx.HTTPError as exc:
            raise TTSError(f"kokoro at {self.base_url} unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise TTSError(f"kokoro {response.status_code}: {response.text[:300]}")
        if not response.content:
            raise TTSError("kokoro returned an empty body")
        return response.content

    # ----------------------------------------------------------- voices ---
    def voices(self) -> list[dict[str, str]]:
        """The served voice list, normalised.

        Two servers, two paths: Kokoro-TTS-OpenAPI serves `/voices` and
        Kokoro-FastAPI serves `/v1/audio/voices`, so both are tried. The
        payload has been three shapes besides -- a bare list of strings, the
        same wrapped in `{"voices": ...}`, and a list of objects -- and all of
        them are flattened here rather than in the UI, which only ever sees
        `{"id", "name"}`.
        """
        for path in VOICE_PATHS:
            try:
                response = self._client.get(path, timeout=PROBE_TIMEOUT)
                if response.status_code >= 400:
                    continue
                body = response.json()
            except Exception:
                continue
            raw = body.get("voices", []) if isinstance(body, dict) else body
            if not isinstance(raw, list) or not raw:
                continue
            out: list[dict[str, str]] = []
            for item in raw:
                if isinstance(item, str):
                    out.append({"id": item, "name": _pretty(item)})
                elif isinstance(item, dict):
                    vid = item.get("id") or item.get("voice") or item.get("name")
                    if vid:
                        out.append({"id": str(vid), "name": _label(item, str(vid))})
            if out:
                return out
        return []

    # ----------------------------------------------------------- health ---
    def health(self) -> dict[str, Any]:
        out: dict[str, Any] = {"provider": self.name, "base_url": self.base_url,
                               "voice": self.voice, "model": self.model,
                               "speed": self.speed}
        reachable = False
        for path in ("/health", *VOICE_PATHS, "/"):
            try:
                response = self._client.get(path, timeout=PROBE_TIMEOUT)
            except httpx.HTTPError:
                continue
            if response.status_code < 500:
                reachable = True
                out["probe"] = path
                break
        if not reachable:
            out |= {"reachable": False,
                    "error": f"{self.base_url} unreachable. Start Kokoro-TTS-OpenAPI "
                             f"(python -m app, port 8080), or the container: docker "
                             f"compose -f docker/docker-compose.yml --profile local-tts "
                             f"up -d tts (port 8880)."}
            return out

        out["reachable"] = True
        voices = self.voices()
        out["voices"] = voices
        if voices:
            ids = {v["id"] for v in voices}
            out["voice_count"] = len(ids)
            # A blend ("af_bella(2)+af_heart(1)") is not a voice id and cannot
            # be checked against the list, so only a plain id is verified.
            if _is_plain_voice(self.voice) and self.voice not in ids:
                near = sorted(v for v in ids if v[:3] == self.voice[:3])[:4]
                out["error"] = (
                    f"the server does not have a voice called {self.voice!r}. "
                    + (f"Close by: {', '.join(near)}." if near else
                       f"It offers {len(ids)} voices; pick one from the list.")
                )
            else:
                out["voice_name"] = next((v["name"] for v in voices
                                          if v["id"] == self.voice), self.voice)
        return out


def _is_plain_voice(voice: str) -> bool:
    """False for a weighted blend, which the server resolves itself."""
    return bool(voice) and "+" not in voice and "(" not in voice


def _label(item: dict[str, Any], voice_id: str) -> str:
    """A picker label out of whatever the server chose to tell us.

    Kokoro-TTS-OpenAPI describes each voice properly -- name, gender, accent
    and a quality grade -- and the grade is the useful part: an A voice and a
    C voice are audibly different, and narration is the whole soundtrack. Any
    server that returns less falls back to parsing the id.
    """
    name = str(item.get("name") or "").strip()
    if not name:
        return _pretty(voice_id)
    who = " ".join(x for x in (str(item.get("accent") or "").strip(),
                               str(item.get("gender") or "").strip()) if x)
    grade = str(item.get("grade") or "").strip()
    out = f"{name} -- {who}" if who else name
    return f"{out} ({grade})" if grade else out


def _pretty(voice_id: str) -> str:
    """`af_heart` -> `Heart (American female)`, for the picker's label."""
    prefix, _, name = voice_id.partition("_")
    language = {"a": "American", "b": "British", "e": "Spanish", "f": "French",
                "i": "Italian", "p": "Portuguese", "h": "Hindi", "j": "Japanese",
                "z": "Mandarin"}.get(prefix[:1], "")
    gender = {"f": "female", "m": "male"}.get(prefix[1:2], "")
    label = " ".join(x for x in (language, gender) if x)
    return f"{name.replace('_', ' ').title()} ({label})" if label and name else voice_id
