"""Visuals provider factory: generated stills, clips and cover backdrops."""
from __future__ import annotations

from typing import Any

from app.config import Config, get_config
from app.providers.visuals.base import AudioResult, ClipResult, StillResult, VisualsError, VisualsProvider

__all__ = ["AudioResult", "ClipResult", "StillResult", "VisualsError", "VisualsProvider",
           "build_visuals", "probe", "PROVIDERS"]

PROVIDERS = ("none", "comfyui", "pexels", "fake")


class NoneProvider(VisualsProvider):
    """The off switch. Never called by a stage -- `VisualsCfg.enabled` gates
    that -- but a profile has to build into something."""

    name = "none"

    def still(self, *args, **kwargs):  # pragma: no cover - guarded upstream
        raise VisualsError("generated visuals are switched off")

    def health(self) -> dict[str, Any]:
        return {"provider": "none", "reachable": True,
                "note": "Pictures are off. Pick a ComfyUI profile to generate them, "
                        "or a Pexels profile to search for them."}


def build_visuals(cfg: Config | None = None, overrides: dict[str, Any] | None = None) -> VisualsProvider:
    cfg = cfg or get_config()
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    requested = overrides.pop("profile", None) or overrides.pop("provider", None)
    if requested and requested in cfg.visuals.profiles:
        name, settings = cfg.visuals.for_profile(requested)
    elif requested:
        from app.config import VISUALS_DEFAULTS

        match = next((p for p in cfg.visuals.profiles.values() if p.adapter == requested), None)
        name = requested
        settings = match.settings() if match else dict(VISUALS_DEFAULTS.get(requested, {}))
    else:
        name, settings = cfg.visuals.for_profile()
    settings.update(overrides)

    if name == "comfyui":
        from app.providers.visuals.comfyui import ComfyUIProvider

        return ComfyUIProvider(settings)
    if name == "pexels":
        from app.providers.visuals.pexels import PexelsProvider

        return PexelsProvider(settings)
    if name == "fake":
        from app.providers.visuals.fake import FakeProvider

        return FakeProvider(settings)
    return NoneProvider(settings)


def probe(cfg: Config | None = None, profile: str | None = None) -> dict[str, Any]:
    """Reachability for the settings page. Never generates anything."""
    try:
        return build_visuals(cfg, {"profile": profile}).health()
    except Exception as exc:
        return {"reachable": False, "error": str(exc)[:300]}
