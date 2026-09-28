"""Configuration: config.yaml + environment overrides.

One provider-switching surface. Nothing in here holds a secret -- API keys are
named by env var (`api_key_env`) and resolved at call time, so a config file can
be committed and shared safely.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PREFIX = "REELFORGE_"


# ---------------------------------------------------------------- LLM ------
class ProviderProfile(BaseModel):
    """One named way to reach a model.

    `adapter` selects the code that talks to it; everything else is that
    adapter's own settings. Extra keys are allowed and passed straight through,
    so an adapter-specific option needs no schema change here.

    Several profiles may share an adapter -- "groq", "openrouter" and a local
    vLLM box are all `openai` -- which is the point: the OpenAI-compatible
    adapter reaches most of the ecosystem, and what differs between those
    services is a base_url and a key, not code.
    """

    model_config = ConfigDict(extra="allow")

    adapter: Literal["anthropic", "openai", "ollama", "fake"] = "openai"
    label: str = ""
    model: str = ""
    base_url: str | None = None
    max_tokens: int = 8192
    temperature: float = 0.7
    api_key_env: str | None = None
    #: Seconds one generation may take. Ten minutes was the hardcoded default
    #: and is far too long to wait before concluding something is wrong -- a
    #: stalled request holds the whole stage with no signal. Raise it
    #: deliberately for a slow free-tier model rather than inheriting it.
    request_timeout: float = 180.0

    def settings(self) -> dict[str, Any]:
        """The dict an adapter constructor expects."""
        out = self.model_dump(exclude_none=True)
        out.pop("adapter", None)
        out.pop("label", None)
        return out

    def display(self) -> str:
        return self.label or self.model or self.adapter


class RoleOverride(BaseModel):
    """Which profile a pipeline stage uses, and any tweaks to it.

    Empty means "use the active profile". The point of per-role routing is that
    the stages are not the same kind of work: writing the script and writing the
    storyboard reward a strong model, while extracting a fact sheet or choosing
    a palette are structured tasks with validators behind them.
    """

    model_config = ConfigDict(extra="allow")

    profile: str | None = None
    #: accepted as an alias for `profile`, since the pre-profiles config used it
    provider: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None

    def target(self) -> str | None:
        return self.profile or self.provider


#: Stages that can be routed independently.
LLM_ROLES = ("content", "storyboard", "review")

#: Ready-made routings offered in the UI.
#:
#: "hybrid" is the one worth explaining: measured on a local
#: qwen2.5:7b-instruct, per-scene generation reaches the target length but the
#: prose is weak -- fact dumps, filler, banned marketing words the validators
#: then reject. Writing the script and writing a storyboard module are the two
#: tasks where model quality shows most; fact-sheet extraction and cover-spec
#: choices are structured and guarded, and a small local model handles them.
ROLE_PRESETS: dict[str, dict[str, Any]] = {
    "all-local": {
        "label": "All local",
        "description": "Everything through the local model. Free and offline; "
                       "the script will read like a 7B model wrote it.",
        "roles": {},
    },
    "hybrid": {
        "label": "Hybrid (recommended)",
        "description": "Hosted model for the script and the storyboard, local "
                       "for everything else. Pennies per reel, and the two "
                       "roles that reward quality get it.",
        "roles": {"content": "__hosted__", "storyboard": "__hosted__",
                  "review": "__local__"},
    },
    "all-hosted": {
        "label": "All hosted",
        "description": "Every stage through the hosted model. Best results, "
                       "still only cents per reel.",
        "roles": {"content": "__hosted__", "storyboard": "__hosted__",
                  "review": "__hosted__"},
    },
}


class LLMCfg(BaseModel):
    active: str = "default"
    profiles: dict[str, ProviderProfile] = Field(default_factory=dict)
    roles: dict[str, RoleOverride] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, data: Any) -> Any:
        """Accept the pre-profiles shape.

        The original config had `provider: ollama` beside fixed `anthropic:`,
        `openai:` and `ollama:` blocks. Rewriting every user's file is worse than
        reading both shapes, so a config without `profiles` is converted here and
        keeps working untouched.
        """
        if not isinstance(data, dict) or "profiles" in data:
            return data
        data = dict(data)
        profiles: dict[str, Any] = {}
        for adapter in ("anthropic", "openai", "ollama"):
            block = data.pop(adapter, None)
            if isinstance(block, dict):
                profiles[adapter] = {**block, "adapter": adapter}
        if profiles:
            data["profiles"] = profiles
        if "provider" in data:
            data.setdefault("active", data.pop("provider"))
        return data

    @model_validator(mode="after")
    def _at_least_one_profile(self) -> "LLMCfg":
        if not self.profiles:
            self.profiles = {"default": ProviderProfile(adapter="fake", model="fake-1")}
        if self.active not in self.profiles:
            self.active = next(iter(self.profiles))
        return self

    def profile_for(self, role: str) -> tuple[str, ProviderProfile, RoleOverride]:
        override = self.roles.get(role) or RoleOverride()
        name = override.target() or self.active
        if name not in self.profiles:
            name = self.active
        return name, self.profiles[name], override

    def for_role(self, role: str) -> tuple[str, dict[str, Any]]:
        """(adapter name, settings) for a pipeline stage."""
        _, profile, override = self.profile_for(role)
        settings = profile.settings()
        for key in ("model", "temperature", "max_tokens"):
            value = getattr(override, key, None)
            if value is not None:
                settings[key] = value
        return profile.adapter, settings


class TTSProfile(BaseModel):
    """One named voice source. Same idea as ProviderProfile, for narration."""

    model_config = ConfigDict(extra="allow")

    adapter: Literal["upload", "elevenlabs", "openai", "kokoro", "local", "say", "fish",
                     "fake"] = "upload"
    label: str = ""

    def settings(self) -> dict[str, Any]:
        out = self.model_dump(exclude_none=True)
        out.pop("adapter", None)
        out.pop("label", None)
        return out

    def display(self) -> str:
        return self.label or self.adapter


#: What each adapter calls its voice field, so a caller can just say "voice".
VOICE_FIELD = {"elevenlabs": "voice_id", "fish": "reference_id"}

#: Defaults for a freshly added profile of each adapter, so the UI can offer
#: something that works rather than an empty form.
TTS_DEFAULTS: dict[str, dict[str, Any]] = {
    "upload": {},
    "elevenlabs": {
        "voice_id": "JBFqnCBsd6RMkjVDRZzb",
        "model": "eleven_multilingual_v2",
        "output_format": "mp3_44100_128",
        "stability": 0.5, "similarity_boost": 0.75, "style": 0.0,
        "use_speaker_boost": True, "max_characters_per_job": 4000,
        "api_key_env": "ELEVENLABS_API_KEY",
        "base_url": "https://api.elevenlabs.io/v1",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini-tts", "voice": "onyx",
        "api_key_env": "OPENAI_API_KEY",
    },
    # Self-hosted Kokoro-FastAPI. No key: the only thing it needs is a URL.
    # Port 8880 is the server's own default and what works outside compose.
    "kokoro": {"base_url": "http://localhost:8080", "voice": "af_heart",
               "model": "kokoro", "response_format": "wav", "speed": 1.0,
               "language": "", "request_timeout": 300},
    "local": {"base_url": "http://tts:8080", "voice": "af_heart", "engine": "kokoro"},
    "say": {"voice": "Daniel", "rate": 165},
    "fish": {"base_url": "http://localhost:7860", "api_name": "/partial",
             "reference_id": "", "reference_audio": "", "reference_text": "",
             "seed": 1, "temperature": 0.8, "top_p": 0.8, "repetition_penalty": 1.1,
             "chunk_length": 300},
}

class VisualsProfile(BaseModel):
    """One image/video generation backend. Same idea as TTSProfile.

    `none` is the always-present null adapter: the reel is drawn entirely by
    the renderer, which is how every reel was made before this existed.
    """

    model_config = ConfigDict(extra="allow")

    adapter: Literal["none", "comfyui", "pexels", "fake"] = "none"
    label: str = ""

    def settings(self) -> dict[str, Any]:
        out = self.model_dump(exclude_none=True)
        out.pop("adapter", None)
        out.pop("label", None)
        return out

    def display(self) -> str:
        return self.label or self.adapter


#: Where the two shipped ComfyUI workflows live. They are API-format exports
#: (what "Save (API format)" writes), which is what POST /prompt takes.
WORKFLOW_DIR = "app/workflows"

VISUALS_DEFAULTS: dict[str, dict[str, Any]] = {
    "none": {},
    "comfyui": {
        "base_url": "http://localhost:8188",
        "image_workflow": f"{WORKFLOW_DIR}/image_qwen_image_2512.json",
        "video_workflow": f"{WORKFLOW_DIR}/video_ltx2_5_t2v.json",
        # Which node carries what. Ids are those of the shipped workflows; an
        # empty map makes the adapter detect them by class_type instead, which
        # works for most simple graphs but not for one with a prompt switch.
        "image_nodes": {"prompt": "238:227", "negative": "238:228",
                        "size": "238:232", "seed": "238:230",
                        "lightning": "238:229", "save": "60"},
        "video_nodes": {"prompt": "405:376", "negative": "405:373",
                        "seconds": "405:362", "fps": "405:361",
                        "enhancer": "405:383", "resolution": "409", "save": "75"},
        "audio_workflow": f"{WORKFLOW_DIR}/audio_stable_audio_3_medium_base.json",
        "audio_nodes": {"prompt": "52:31", "negative": "52:7", "seconds": "52:36",
                        "seed": "52:3", "category": "52:43", "enhancer": "52:35", "save": "19"},
        "lightning": True,
        "prompt_enhancer": True,
        "audio_enhancer": True,
        "image_width": 1152, "image_height": 1536,
        "request_timeout": 1800,
        "api_key_env": "",
    },
    # Stock photography and footage instead of a generator. No GPU, no
    # workflows: a free key and a search term per scene.
    "pexels": {
        "api_key_env": "PEXELS_API_KEY",
        "orientation": "portrait",     # the reel is 1080x1920
        "photo_size": "large",
        "video_quality": "hd",
        "pool": 15,                    # results to choose among; the seed picks
        "clip_start": 0.5,             # skip a fade-in, when the shot is long enough
        "image_width": 1152,           # what the still is prepared to, as with ComfyUI
        "image_height": 1536,
        "request_timeout": 60,
    },
    "fake": {},
}


#: Endpoints the OpenAI-compatible adapter reaches, so the UI can offer them by
#: name. They differ only in a base_url and a key, which is the whole reason one
#: adapter covers them.
OPENAI_COMPATIBLE_ENDPOINTS: dict[str, dict[str, Any]] = {
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1",
               "api_key_env": "OPENAI_API_KEY", "model": "gpt-4o",
               "json_mode": "json_schema", "hosted": True},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
                   "api_key_env": "OPENROUTER_API_KEY",
                   "model": "anthropic/claude-sonnet-4.5",
                   "json_mode": "json_schema", "hosted": True},
    # Google exposes an OpenAI-compatible surface alongside its native API, so
    # it needs no separate adapter -- the native one uses a different request
    # shape and an X-goog-api-key header, which would be a second code path for
    # no benefit.
    "google": {"label": "Google Gemini",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "api_key_env": "GOOGLE_API_KEY", "model": "gemini-flash-latest",
               "json_mode": "json_schema", "hosted": True},
    "xai": {"label": "xAI Grok", "base_url": "https://api.x.ai/v1",
            "api_key_env": "XAI_API_KEY", "model": "grok-4",
            "json_mode": "json_schema", "hosted": True},
    "mistral": {"label": "Mistral", "base_url": "https://api.mistral.ai/v1",
                "api_key_env": "MISTRAL_API_KEY", "model": "mistral-large-latest",
                # Mistral's json_schema support is newer than json_object and
                # varies by model; the adapter falls through, so starting at the
                # widely-supported mode avoids a wasted first call.
                "json_mode": "json_object", "hosted": True},
    "cohere": {"label": "Cohere",
               "base_url": "https://api.cohere.ai/compatibility/v1",
               "api_key_env": "COHERE_API_KEY", "model": "command-a-03-2025",
               "json_mode": "json_object", "hosted": True},
    "groq": {"label": "Groq", "base_url": "https://api.groq.com/openai/v1",
             "api_key_env": "GROQ_API_KEY", "model": "llama-3.3-70b-versatile",
             "json_mode": "json_object", "hosted": True},
    "together": {"label": "Together", "base_url": "https://api.together.xyz/v1",
                 "api_key_env": "TOGETHER_API_KEY",
                 "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
                 "json_mode": "json_schema", "hosted": True},
    # No api_key_env: these take no key, and inheriting one made a working
    # local endpoint report as unreachable for want of a credential it never
    # asked for.
    "vllm": {"label": "vLLM (self-hosted)", "base_url": "http://localhost:8000/v1",
             "model": "", "json_mode": "text", "hosted": False, "api_key_env": None},
    "lmstudio": {"label": "LM Studio", "base_url": "http://localhost:1234/v1",
                 "model": "", "json_mode": "json_schema", "hosted": False,
                 "api_key_env": None},
}

#: Hosts that mean "this model runs on your own hardware".
LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal", "comfyui",
               "ollama", "vllm", "tts")


def is_hosted(profile) -> bool:
    """Whether a profile reaches somebody else's hardware.

    The role presets need this: "hosted" is the class of model worth paying for
    on the writing stages, and it is not the same as "not Ollama". An OpenRouter
    profile and a self-hosted vLLM both use the OpenAI-compatible adapter, and
    only one of them is a frontier model.
    """
    if profile.adapter == "anthropic":
        return True
    if profile.adapter == "ollama":
        return False
    url = (profile.base_url or "").strip()
    if not url:
        return profile.adapter == "openai"
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if host in LOCAL_HOSTS:
        return False
    # a private address is somebody's own box, however it is reached
    try:
        import ipaddress

        return not ipaddress.ip_address(host).is_private
    except ValueError:
        return True


#: The editable settings of each adapter, so the UI renders one form per
#: adapter from data rather than hardcoding a form per provider in JavaScript.
#: `type` drives the control: "voice" becomes a picker populated from the
#: provider's own voice list, so an ElevenLabs voice is chosen rather than a
#: 20-character id pasted from a dashboard.
PROFILE_FIELDS: dict[str, list[dict[str, Any]]] = {
    "anthropic": [
        {"key": "model", "label": "Model", "type": "text"},
        {"key": "api_key_env", "label": "API key variable", "type": "keyname"},
        {"key": "max_tokens", "label": "Max reply tokens", "type": "number",
         "help": "How long an ANSWER may be -- not the context window. The prompt and the reply share the window, so setting this near the window size leaves no room for the prompt and every request fails. Leave it well under: 4000-8000 suits a 16k model."},
        {"key": "temperature", "label": "Temperature", "type": "number", "step": 0.1},
        {"key": "request_timeout", "label": "Request timeout (s)", "type": "number"},
    ],
    "openai": [
        {"key": "model", "label": "Model", "type": "text"},
        {"key": "base_url", "label": "Base URL", "type": "text"},
        {"key": "api_key_env", "label": "API key variable", "type": "keyname"},
        {"key": "json_mode", "label": "Structured output", "type": "select",
         "options": ["json_schema", "json_object", "text"],
         "help": "json_schema is exact and right for an instruct model. A "
                 "reasoning model must use text -- a grammar forces JSON from "
                 "the first token and leaves it no room to think."},
        {"key": "max_tokens", "label": "Max reply tokens", "type": "number",
         "help": "How long an ANSWER may be -- not the context window. The prompt and the reply share the window, so setting this near the window size leaves no room for the prompt and every request fails. Leave it well under: 4000-8000 suits a 16k model."},
        {"key": "temperature", "label": "Temperature", "type": "number", "step": 0.1},
        {"key": "request_timeout", "label": "Request timeout (s)", "type": "number",
         "help": "Raise it for a slow free-tier model."},
        {"key": "disable_thinking", "label": "Disable thinking mode", "type": "bool",
         "help": "For Qwen3 and similar served by vLLM. A reasoning model emits "
                 "a scratchpad before every answer, which makes it slow and "
                 "blocks grammar-constrained decoding. Off, it behaves like an "
                 "instruct model and json_schema works."},
    ],
    "ollama": [
        {"key": "model", "label": "Model", "type": "text"},
        {"key": "base_url", "label": "Base URL", "type": "text"},
        {"key": "keep_alive", "label": "Keep model loaded", "type": "text",
         "help": "How long Ollama holds the model in memory. A cold reload "
                 "between stages costs minutes."},
        {"key": "max_tokens", "label": "Max reply tokens", "type": "number",
         "help": "How long an ANSWER may be -- not the context window. The prompt and the reply share the window, so setting this near the window size leaves no room for the prompt and every request fails. Leave it well under: 4000-8000 suits a 16k model."},
        {"key": "temperature", "label": "Temperature", "type": "number", "step": 0.1},
        {"key": "request_timeout", "label": "Request timeout (s)", "type": "number"},
    ],
    "elevenlabs": [
        {"key": "voice_id", "label": "Voice", "type": "voice",
         "help": "Pick from the voices on your account."},
        {"key": "model", "label": "Model", "type": "select",
         "options": ["eleven_multilingual_v2", "eleven_turbo_v2_5",
                     "eleven_flash_v2_5", "eleven_v3"]},
        {"key": "output_format", "label": "Output format", "type": "select",
         "options": ["mp3_44100_128", "mp3_44100_192", "mp3_22050_32", "pcm_44100"]},
        {"key": "api_key_env", "label": "API key variable", "type": "keyname"},
        {"key": "stability", "label": "Stability", "type": "number", "step": 0.05,
         "help": "Lower is more expressive, higher is more consistent."},
        {"key": "similarity_boost", "label": "Similarity boost", "type": "number",
         "step": 0.05},
        {"key": "style", "label": "Style", "type": "number", "step": 0.05},
        {"key": "use_speaker_boost", "label": "Speaker boost", "type": "bool"},
        {"key": "max_characters_per_job", "label": "Character cap per job",
         "type": "number",
         "help": "Credits are metered per character. A 40 s reel is about "
                 "550-700, so this refuses an overlong job before it spends."},
    ],
    "openai_tts": [
        {"key": "voice", "label": "Voice", "type": "voice"},
        {"key": "model", "label": "Model", "type": "text"},
        {"key": "base_url", "label": "Base URL", "type": "text"},
        {"key": "api_key_env", "label": "API key variable", "type": "keyname"},
    ],
    "kokoro": [
        {"key": "base_url", "label": "Base URL", "type": "text",
         "help": "Kokoro-TTS-OpenAPI runs on 8080; the Kokoro-FastAPI container "
                 "on 8880 (http://tts:8880 inside compose). Either works -- the "
                 "adapter finds whichever voice endpoint the server has."},
        {"key": "voice", "label": "Voice", "type": "voice",
         "help": "Pick from what the server actually has. Named "
                 "<language><gender>_<name>: af_heart is American female, "
                 "bm_george British male. A weighted blend also works if you type "
                 "one: af_bella(2)+af_heart(1)."},
        {"key": "speed", "label": "Speed", "type": "number", "step": 0.05,
         "help": "1.0 is the model's own pace. The reel's timing comes from the "
                 "narration, so this changes how long the video is."},
        {"key": "response_format", "label": "Audio format", "type": "select",
         "options": ["wav", "mp3", "flac", "opus", "pcm"],
         "help": "wav is lossless and the joiner's own format; the others are "
                 "re-encoded on the way in for no gain."},
        {"key": "language", "label": "Language code", "type": "text",
         "help": "Leave empty and the voice decides. Only set it to override "
                 "the pronunciation rules, e.g. 'a' for American English."},
        {"key": "model", "label": "Model", "type": "text"},
        {"key": "request_timeout", "label": "Request timeout (s)", "type": "number",
         "help": "CPU inference runs at roughly real time, so a long phrase takes "
                 "as long as it lasts."},
    ],
    "local": [
        {"key": "base_url", "label": "Base URL", "type": "text"},
        {"key": "voice", "label": "Voice", "type": "text"},
        {"key": "engine", "label": "Engine", "type": "text"},
    ],
    "say": [
        {"key": "voice", "label": "Voice", "type": "text"},
        {"key": "rate", "label": "Words per minute", "type": "number"},
    ],
    "fish": [
        {"key": "base_url", "label": "Gradio app URL", "type": "text",
         "help": "The Fish-Speech / OpenAudio web app, e.g. http://gpu-box:7860."},
        {"key": "reference_audio", "label": "Sample voice", "type": "audio",
         "help": "A 10-30 s recording of the voice to clone, sent with every phrase as "
                 "the reference. Upload one here (wav, mp3, m4a, flac), or leave it "
                 "empty and the adapter fixes a voice itself: one seeded calibration "
                 "line, generated once and reused for every phrase of every reel."},
        {"key": "reference_text", "label": "Reference transcript", "type": "text",
         "help": "What the reference recording says. Improves cloning accuracy."},
        {"key": "reference_id", "label": "Server voice id", "type": "text",
         "help": "A voice the server already holds under references/<id>. Leave "
                 "empty unless you set one up there."},
        {"key": "seed", "label": "Seed", "type": "number",
         "help": "Changing it changes the self-referenced voice."},
        {"key": "temperature", "label": "Temperature", "type": "number", "step": 0.1},
        {"key": "top_p", "label": "Top-P", "type": "number", "step": 0.05},
        {"key": "repetition_penalty", "label": "Repetition penalty", "type": "number", "step": 0.05},
    ],
    "upload": [],
    "none": [],
    "comfyui": [
        {"key": "base_url", "label": "Base URL", "type": "text",
         "help": "The ComfyUI server, e.g. http://gpu-box:8188. Generation is "
                 "slow: a 5 s LTX clip is minutes, so this is a poor fit for a "
                 "hosted tunnel that drops idle connections."},
        {"key": "api_key_env", "label": "API key variable", "type": "keyname",
         "help": "Only for a ComfyUI behind an auth proxy. Sent as a Bearer token."},
        {"key": "image_workflow", "label": "Image workflow (API JSON)", "type": "text"},
        {"key": "video_workflow", "label": "Video workflow (API JSON)", "type": "text",
         "help": "Leave empty to generate stills only."},
        {"key": "audio_workflow", "label": "Audio workflow (API JSON)", "type": "text",
         "help": "A text-to-audio graph (Stable Audio) for the music bed and the "
                 "cut sounds. Not speech: narration stays with the voice profiles. "
                 "Leave empty for no generated audio."},
        {"key": "audio_enhancer", "label": "Stable Audio reprompt", "type": "bool",
         "help": "Lets the workflow's LLM node expand the description into a full "
                 "music prompt before encoding it."},
        {"key": "lightning", "label": "Qwen Lightning (4 steps)", "type": "bool",
         "help": "Flips the workflow's Lightning switch: four steps instead of "
                 "fifty. Much faster, slightly softer."},
        {"key": "prompt_enhancer", "label": "LTX prompt enhancer", "type": "bool",
         "help": "Lets the workflow's Gemma node expand the shot description "
                 "before encoding it."},
        {"key": "image_width", "label": "Still width", "type": "number"},
        {"key": "image_height", "label": "Still height", "type": "number",
         "help": "3:4 portrait by default: it fills the panel band and crops "
                 "cleanly to the full frame."},
        {"key": "request_timeout", "label": "Generation timeout (s)", "type": "number"},
    ],
    "pexels": [
        {"key": "api_key_env", "label": "API key variable", "type": "keyname",
         "help": "A free key from pexels.com/api/. 200 requests an hour, which is "
                 "around forty reels -- one search per picture."},
        {"key": "orientation", "label": "Orientation", "type": "select",
         "options": ["portrait", "square", "landscape"],
         "help": "The reel is 1080x1920. Anything but portrait is centre-cropped "
                 "hard and loses most of its sides."},
        {"key": "photo_size", "label": "Minimum photo size", "type": "select",
         "options": ["large", "medium", "small"]},
        {"key": "video_quality", "label": "Preferred video quality", "type": "select",
         "options": ["hd", "sd"],
         "help": "Of the files offered for a clip, the smallest one at least 1080 "
                 "wide is taken -- both are cropped to the same frame and only one "
                 "of them is a 300 MB download."},
        {"key": "pool", "label": "Results to choose among", "type": "number",
         "help": "One search returns this many; the scene's seed picks one of them. "
                 "A bigger pool varies the picture without costing another request. "
                 "Max 80."},
        {"key": "clip_start", "label": "Skip into the clip (s)", "type": "number",
         "step": 0.1,
         "help": "Stock footage often opens on a fade or a settling camera, and the "
                 "reel cuts to the first frame with no run-up. Ignored when the shot "
                 "is too short to spare it."},
        {"key": "request_timeout", "label": "Request timeout (s)", "type": "number"},
    ],
}


def profile_fields(adapter: str, kind: str = "llm") -> list[dict[str, Any]]:
    """Editable fields for an adapter. TTS and LLM both have an `openai`."""
    if adapter == "openai" and kind == "tts":
        return PROFILE_FIELDS["openai_tts"]
    return PROFILE_FIELDS.get(adapter, [])


LLM_DEFAULTS: dict[str, dict[str, Any]] = {
    "anthropic": {"model": "claude-opus-5", "max_tokens": 16384,
                  "api_key_env": "ANTHROPIC_API_KEY"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o",
               "max_tokens": 16384, "api_key_env": "OPENAI_API_KEY",
               "json_mode": "json_schema"},
    "ollama": {"base_url": "http://ollama:11434", "model": "qwen2.5:7b-instruct-q4_K_M",
               "max_tokens": 8000, "keep_alive": "30m"},
}


class TTSCfg(BaseModel):
    active: str = "upload"
    profiles: dict[str, TTSProfile] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "profiles" in data:
            return data
        data = dict(data)
        profiles: dict[str, Any] = {}
        for adapter in ("elevenlabs", "openai", "kokoro", "local", "say", "fish"):
            block = data.pop(adapter, None)
            if isinstance(block, dict):
                profiles[adapter] = {**block, "adapter": adapter}
        profiles.setdefault("upload", {"adapter": "upload"})
        data["profiles"] = profiles
        if "provider" in data:
            data.setdefault("active", data.pop("provider"))
        return data

    @model_validator(mode="after")
    def _always_offer_upload(self) -> "TTSCfg":
        # a job must always be able to fall back to a file you supply yourself
        self.profiles.setdefault("upload", TTSProfile(adapter="upload"))
        if self.active not in self.profiles:
            self.active = "upload"
        return self

    def for_profile(self, name: str | None = None) -> tuple[str, dict[str, Any]]:
        chosen = name or self.active
        profile = self.profiles.get(chosen) or self.profiles["upload"]
        return profile.adapter, profile.settings()


class VisualsCfg(BaseModel):
    """Generated imagery: stills for scenes, a clip or two, a cover backdrop.

    Off unless a profile other than `none` is active. Every count here is a
    ceiling -- a reel with two body scenes never asks for four stills.
    """

    active: str = "none"
    profiles: dict[str, VisualsProfile] = Field(default_factory=dict)
    #: generated stills per reel. Stills are the overflow, not the point:
    #: clips are assigned to the body scenes first and stills only take the
    #: scenes left over, so with clips at 3 a four-scene script has none.
    stills: int = Field(default=1, ge=0, le=6)
    #: generated video clips per reel -- the primary visuals. A body scene
    #: gets a moving picture, roughly 100 s of generation each on one GPU.
    clips: int = Field(default=3, ge=0, le=3)
    clip_seconds: float = Field(default=5.0, ge=2.0, le=10.0)
    #: paint the cover over a generated backdrop instead of the procedural ground
    cover: bool = True
    #: a generated instrumental bed under the narration. Off by default: the
    #: retention rules in DEVELOPMENT.md say voice only, and a bed anyone
    #: notices is too loud -- so this is a choice, made per reel or here.
    music: bool = False
    music_gain_db: float = Field(default=-22.0, ge=-40.0, le=-6.0)
    music_duck_db: float = Field(default=-9.0, ge=-24.0, le=0.0)
    #: generated one-shots for the storyboards' cut sounds, in place of the
    #: synthesized set. Made once into data/sfx/<profile>/ and reused.
    sfx_samples: bool = False
    #: how a still is placed: `full` bleeds to the frame edge under a scrim,
    #: `panel` sits in a card inside the panel band
    still_fit: Literal["full", "panel"] = "full"
    #: ask the language model to rewrite each scene's direction as a text-free
    #: shot plus a heading, instead of cleaning the direction by rule
    art_director: bool = True
    #: appended to every prompt so the assets share one look. Palette words
    #: are added per template at generation time.
    style: str = ("cinematic, photoreal, shallow depth of field, soft volumetric "
                  "light, a bright well-lit subject on a dark background, no text, no letters, no logos, "
                  "no watermark")
    negative: str = ""

    @model_validator(mode="after")
    def _always_offer_none(self) -> "VisualsCfg":
        self.profiles.setdefault("none", VisualsProfile(adapter="none"))
        if self.active not in self.profiles:
            self.active = "none"
        return self

    @property
    def enabled(self) -> bool:
        return self.profiles[self.active].adapter != "none"

    def for_profile(self, name: str | None = None) -> tuple[str, dict[str, Any]]:
        chosen = name or self.active
        profile = self.profiles.get(chosen) or self.profiles["none"]
        return profile.adapter, profile.settings()


class ContentCfg(BaseModel):
    """How the script is generated.

    whole      one call for the entire script. Best narrative flow, and what a
               frontier model should use.
    per_scene  an outline call, then one call per scene. A model that returns
               40 words when asked for 95 returns 25 when asked for 25, so this
               is what makes a small local model usable.
    auto       try whole once; if it comes back wrong, switch to per_scene for
               the remaining attempts rather than asking the same way again.
    """

    script_mode: Literal["auto", "whole", "per_scene"] = "auto"
    #: Default for new jobs; each job can override it on the form. Burned-in
    #: captions are what carry a muted autoplay, which is how most reels are
    #: first watched -- but Instagram, YouTube and Facebook all auto-caption on
    #: upload now, so the duplication is a fair reason to turn them off.
    burn_captions: bool = True
    #: Spoken as the last line of every reel. It has to be part of the script,
    #: not appended at synthesis time: `align.py build` refuses to run unless the
    #: phrase count matches the segments it detects, so a spoken line with no
    #: phrase behind it breaks alignment outright. Empty disables it.
    outro: str = "Follow for more."
    #: Default for new jobs; each job can override it on the form.
    fact_check: Literal["strict", "warn", "off"] = "strict"
    target_seconds_min: float = 36.0
    target_seconds_max: float = 44.0
    max_attempts: int = 3


#: Stages that pause for review by default. These two are worth reading before
#: the pipeline spends minutes rendering: the script is what the video says, and
#: the storyboard is generated code.
DEFAULT_MANUAL_STAGES = ["content", "storyboard"]


class ApprovalCfg(BaseModel):
    """Which stages wait for a human.

    Replaces a single on/off switch. All-manual is tedious once you trust the
    pipeline, all-auto spends a render on a script nobody read; per stage lets
    you stop where it actually matters.
    """

    manual_stages: list[str] = Field(default_factory=lambda: list(DEFAULT_MANUAL_STAGES))

    def preset(self) -> str:
        from app.models.job import STAGE_ORDER

        names = {s.value for s in STAGE_ORDER}
        chosen = set(self.manual_stages)
        if not chosen:
            return "all-auto"
        if chosen >= names:
            return "all-manual"
        return "custom"


class IngestCfg(BaseModel):
    github_token_env: str = "GITHUB_TOKEN"
    hf_token_env: str = "HUGGINGFACE_TOKEN"
    timeout_seconds: int = 30
    readme_max_chars: int = 60000


class RenderCfg(BaseModel):
    chunk_seconds: float = 4.0
    #: 0 means "decide from the machine". Each chunk worker runs a Python
    #: renderer *and* an ffmpeg encoder, so a fixed default oversubscribes badly:
    #: 8 workers on a 4-core box drove the load average to 25 and made a render
    #: that normally takes 90 seconds take more than ten minutes.
    workers: int = 0
    safe_encode: bool = False
    fps: int = 30
    width: int = 1080
    height: int = 1920
    #: A directory holding the ffmpeg/ffprobe to use, put at the front of PATH
    #: for this process and every child (the renderer's own align.py and
    #: sfx.py find ffmpeg by PATH). For a machine whose PATH carries an old
    #: build first: the encode settings need ffmpeg 4.0 or later, and a 2013
    #: binary rejects `-colorspace bt709` and breaks the frame pipe with a
    #: bare EINVAL. Empty means PATH as it is.
    ffmpeg_dir: str = ""

    def resolved_workers(self) -> int:
        """How many chunks to render at once.

        Sized from *physical* cores, not `os.cpu_count()`. Frame rendering is
        CPU-bound PIL and numpy, which gains almost nothing from hyperthreading,
        and each chunk runs a renderer and an encoder. On a 4-core box that
        reports 8 logical CPUs, sizing from the logical count produced a load
        average of 25 and turned a 90-second render into ten minutes.

        Leaving one core free keeps the machine responsive and, under compose,
        leaves room for the API and the short-stage worker.
        """
        if self.workers > 0:
            return self.workers
        return max(2, min(8, physical_cores() - 1))

    @field_validator("chunk_seconds")
    @classmethod
    def _whole_gops(cls, v: float) -> float:
        # keyint=60 at 30 fps => a GOP is 2.0 s. Chunk boundaries must land on
        # one or the concat demuxer produces a stream that will not seek cleanly.
        if abs(v / 2.0 - round(v / 2.0)) > 1e-6 or v < 2.0:
            raise ValueError("chunk_seconds must be a whole multiple of 2.0 and >= 2.0")
        return v


class FontsCfg(BaseModel):
    display_dir: str = "docker/fonts"
    display_family: str = "Inter"
    mono_family: str = "DejaVuSansMono"

    def dir_path(self) -> Path:
        p = Path(self.display_dir)
        return p if p.is_absolute() else REPO_ROOT / p


class StoryboardCfg(BaseModel):
    max_repair_attempts: int = 3
    vision_review: bool = False
    fallback_to_template: bool = True
    smoke_frames: int = 16
    smoke_timeout_seconds: int = 90
    memory_limit_mb: int = 3072


class PathsCfg(BaseModel):
    video_dir: str = "video"
    data_dir: str = "data"
    out_dir: str = "out"

    def _abs(self, raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def video(self) -> Path:
        return self._abs(self.video_dir)

    @property
    def data(self) -> Path:
        return self._abs(self.data_dir)

    @property
    def out(self) -> Path:
        return self._abs(self.out_dir)

    @property
    def jobs(self) -> Path:
        return self.data / "jobs"


class QueueCfg(BaseModel):
    #: One reel at a time, end to end. Off falls back to the old behaviour of
    #: dispatching straight to the thread pool.
    one_at_a_time: bool = True
    #: How often the scheduler looks when nothing woke it. A poll as well as an
    #: event means a missed wakeup self-heals.
    poll_seconds: float = 3.0
    #: Pause the queue after this many consecutive failures -- that is a broken
    #: model server, not five individually broken reels. 0 disables.
    pause_after_consecutive_failures: int = 2
    #: Start the scheduler thread with the app. Off for tests that drive tick().
    start_scheduler: bool = True

    broker_url: str = "redis://redis:6379/0"
    result_backend: str = "redis://redis:6379/1"
    progress_channel_prefix: str = "reelforge:progress"


class Config(BaseModel):
    llm: LLMCfg = Field(default_factory=LLMCfg)
    tts: TTSCfg = Field(default_factory=TTSCfg)
    visuals: VisualsCfg = Field(default_factory=VisualsCfg)
    content: ContentCfg = Field(default_factory=ContentCfg)
    approval: ApprovalCfg = Field(default_factory=ApprovalCfg)
    ingest: IngestCfg = Field(default_factory=IngestCfg)
    render: RenderCfg = Field(default_factory=RenderCfg)
    fonts: FontsCfg = Field(default_factory=FontsCfg)
    storyboard: StoryboardCfg = Field(default_factory=StoryboardCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)
    queue: QueueCfg = Field(default_factory=QueueCfg)


# ------------------------------------------------------------- loading -----
def _coerce(raw: str) -> Any:
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            pass
    return raw


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """REELFORGE_RENDER__WORKERS=4 -> data["render"]["workers"] = 4"""
    for key, value in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = key[len(ENV_PREFIX) :].lower().split("__")
        node = data
        for part in path[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):  # a scalar sits where a section should
                break
        else:
            node[path[-1]] = _coerce(value)
    return data


def config_path() -> Path:
    raw = os.environ.get("REELFORGE_CONFIG", "").strip()
    return Path(raw) if raw else REPO_ROOT / "config.yaml"


def load_config(path: str | Path | None = None, *, overlay: bool = True) -> Config:
    """Baseline file, then the UI's overlay, then the environment.

    `config.yaml` is the committed baseline and is mounted read-only in the
    container, so anything written from the UI lands in `data/settings.yaml`
    instead and is merged over it here.
    """
    cfg_path = Path(path) if path else config_path()
    data: dict[str, Any] = {}
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if overlay:
        from app.settings_store import merge, read_overlay

        data = merge(data, read_overlay())
    cfg = Config.model_validate(_apply_env_overrides(data))
    _apply_ffmpeg_dir(cfg.render.ffmpeg_dir)
    return cfg


def _apply_ffmpeg_dir(directory: str) -> None:
    """Put `render.ffmpeg_dir` first on PATH, once. Children inherit it."""
    if not directory:
        return
    resolved = str(Path(directory).expanduser().resolve())
    if not Path(resolved).is_dir():
        return
    current = os.environ.get("PATH", "")
    if current.split(os.pathsep)[:1] == [resolved]:
        return
    parts = [p for p in current.split(os.pathsep) if p and p != resolved]
    os.environ["PATH"] = os.pathsep.join([resolved, *parts])
    # shutil.which caches nothing, but a resolver that does must forget
    from app.render import workspace as _ws

    _ws.vendored_ffmpeg_usable.cache_clear()


_cache: tuple[tuple[float, ...], Config] | None = None


def get_config() -> Config:
    """Cached, but invalidated when any configuration file changes.

    The API and each worker are separate processes, so a settings change made
    through the UI has to reach the others. Keying the cache on file mtimes does
    that without a message bus: every process notices on its next call. The cost
    is three `stat()` calls, which is nothing next to the work each stage does.
    """
    global _cache
    from app.settings_store import fingerprint

    stamp = fingerprint()
    if _cache is not None and _cache[0] == stamp:
        return _cache[1]
    config = load_config()
    _cache = (stamp, config)
    return config


def reload_config() -> Config:
    """Drop the cache now, rather than waiting for a file to look different."""
    global _cache
    _cache = None
    return get_config()


def _clear_cache() -> None:
    global _cache
    _cache = None


#: `get_config` was an `lru_cache` before it became mtime-keyed. Keeping the
#: same clearing call means tests and any caller that reached for it still work.
get_config.cache_clear = _clear_cache


@lru_cache(maxsize=1)
def physical_cores() -> int:
    """Physical cores, falling back sensibly when they cannot be determined.

    `os.cpu_count()` reports logical CPUs. Under cgroups (a container with a CPU
    limit) it reports the host's count, which is why the quota is checked first.
    """
    import os
    import subprocess

    # a container's CPU quota is the real ceiling, whatever the host has
    for quota_path, period_path in (
        ("/sys/fs/cgroup/cpu.max", None),
        ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "/sys/fs/cgroup/cpu/cpu.cfs_period_us"),
    ):
        try:
            raw = Path(quota_path).read_text(encoding="utf-8").split()
            quota = raw[0]
            period = raw[1] if period_path is None else Path(period_path).read_text(encoding="utf-8").strip()
            if quota not in ("max", "-1"):
                allowed = int(int(quota) / int(period))
                if allowed >= 1:
                    return allowed
        except (OSError, ValueError, IndexError):
            pass

    try:  # macOS
        out = subprocess.run(["sysctl", "-n", "hw.physicalcpu"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip().isdigit():
            return int(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass

    try:  # Linux: count distinct (physical id, core id) pairs
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8")
        pairs, physical, core = set(), None, None
        for line in text.splitlines():
            if line.startswith("physical id"):
                physical = line.split(":")[1].strip()
            elif line.startswith("core id"):
                core = line.split(":")[1].strip()
                if physical is not None:
                    pairs.add((physical, core))
        if pairs:
            return len(pairs)
    except OSError:
        pass

    logical = os.cpu_count() or 4
    return max(1, logical // 2)


def secret(env_name: str | None) -> str | None:
    """A key's value: the environment first, then what the UI stored.

    Environment wins so an existing `.env` keeps behaving exactly as before and
    a key typed into the UI only fills a gap. `secret_status` reports which
    source a key came from, because "I changed it and nothing happened" is
    otherwise a confusing half hour.
    """
    if not env_name:
        return None
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    from app.settings_store import read_secrets

    return read_secrets().get(env_name) or None
