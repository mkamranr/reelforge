"""TTS provider factory."""
from __future__ import annotations

from typing import Any

from app.config import Config, get_config
from app.providers.tts.base import (
    SpokenPhrase, TTSError, TTSProvider, TTSResult, UploadRequired,
)
from app.providers.tts.joiner import join

__all__ = [
    "SpokenPhrase", "TTSError", "TTSProvider", "TTSResult", "UploadRequired",
    "join", "build_tts", "list_voices", "voice_field", "PROVIDERS",
]

PROVIDERS = ("upload", "elevenlabs", "openai", "kokoro", "local", "say", "fish")


#: Each engine names the voice differently. A caller -- the UI, a job override,
#: an env var -- should be able to say "voice" and have it land in the right
#: field, without knowing which engine is selected.
VOICE_KEY = {
    "elevenlabs": "voice_id",
    "openai": "voice",
    "kokoro": "voice",
    "local": "voice",
    "say": "voice",
    "fish": "reference_id",
}


def voice_field(provider: str) -> str:
    return VOICE_KEY.get(provider, "voice")


def build_tts(
    cfg: Config | None = None, overrides: dict[str, Any] | None = None
) -> TTSProvider:
    """Resolve the TTS engine.

    Voice selection is configurable at three levels, each overriding the one
    before: `config.yaml`, an env var
    (`REELFORGE_TTS__ELEVENLABS__VOICE_ID=...`), and a per-job override sent
    with the request. A generic `voice` override is translated to whatever the
    selected engine calls that field.
    """
    cfg = cfg or get_config()
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    requested = overrides.pop("provider", None) or overrides.pop("profile", None)

    if requested and requested in cfg.tts.profiles:
        name, settings = cfg.tts.for_profile(requested)
    elif requested:
        from app.config import TTS_DEFAULTS

        match = next((p for p in cfg.tts.profiles.values() if p.adapter == requested), None)
        name = requested
        settings = match.settings() if match else dict(TTS_DEFAULTS.get(requested, {}))
    else:
        name, settings = cfg.tts.for_profile()

    # a generic "voice" lands in whatever field this engine calls it
    generic_voice = overrides.pop("voice", None)
    if generic_voice is not None:
        settings[voice_field(name)] = generic_voice
    settings.update(overrides)

    from app.providers.tts import providers as impl

    if name == "fish":
        from app.providers.tts.fish import FishSpeechProvider

        return FishSpeechProvider(settings)
    if name == "kokoro":
        from app.providers.tts.kokoro import KokoroProvider

        return KokoroProvider(settings)
    return {
        "upload": impl.UploadProvider,
        "elevenlabs": impl.ElevenLabsProvider,
        "openai": impl.OpenAITTSProvider,
        "local": impl.LocalHTTPProvider,
        "say": impl.SayProvider,
    }.get(name, impl.UploadProvider)(settings)


def list_voices(cfg: Config | None = None, provider: str | None = None) -> dict[str, Any]:
    """What the UI's voice picker shows. Costs no credits on any engine."""
    cfg = cfg or get_config()
    name = provider or cfg.tts.active
    try:
        engine = build_tts(cfg, {"provider": name})
    except TTSError as exc:
        return {"provider": name, "reachable": False, "error": str(exc)}
    health = engine.health()
    return {
        "provider": name,
        "voice_field": voice_field(name),
        "selected": getattr(engine, voice_field(name), None) or engine.voice,
        "reachable": health.get("reachable", False),
        "voices": health.get("voices", []),
        # per-key balances; `credits` kept for any caller still reading it
        "credits": health.get("credits"),
        "key_balances": health.get("key_balances"),
        "keys": health.get("keys"),
        "voice_name": health.get("voice_name"),
        "error": health.get("error"),
    }
