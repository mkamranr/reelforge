"""The Kokoro voice adapter against a mocked server.

Three things carry their weight here. Two different servers speak this API --
Kokoro-TTS-OpenAPI and Kokoro-FastAPI -- and they keep their voice lists at
different paths, so the adapter has to find either. The payload itself has
been three shapes besides, and all of them must reach the picker looking the
same. And a voice id that does not exist is only rejected when synthesis runs,
a dozen calls into a narration, so `health()` has to catch it at configuration
time instead.
"""
from __future__ import annotations

import httpx
import pytest

respx = pytest.importorskip("respx")

BASE = "http://kokoro.test:8880"

VOICE_IDS = ["af_heart", "af_bella", "am_michael", "bf_emma", "bm_george", "jf_alpha"]


def _provider(**extra):
    from app.providers.tts.kokoro import KokoroProvider

    settings = {"base_url": BASE, "voice": "af_heart", "request_timeout": 10}
    settings.update(extra)
    return KokoroProvider(settings)


# -------------------------------------------------------------- speak ---
@respx.mock
def test_a_phrase_is_posted_in_the_openai_shape_and_returns_audio():
    sent = {}

    def on_speech(request):
        import json as _json
        sent.update(_json.loads(request.content))
        return httpx.Response(200, content=b"RIFF....WAVEfake")

    respx.post(f"{BASE}/v1/audio/speech").mock(side_effect=on_speech)

    audio = _provider(speed=1.15).speak("One connection, reused.")

    assert audio == b"RIFF....WAVEfake"
    assert sent == {"model": "kokoro", "input": "One connection, reused.",
                    "voice": "af_heart", "response_format": "wav", "speed": 1.15}
    # no lang_code unless one was configured -- the voice decides by default
    assert "lang_code" not in sent


@respx.mock
def test_a_weighted_blend_is_passed_through_untouched():
    """The server resolves `af_bella(2)+af_heart(1)` itself. The adapter must
    not try to validate or normalise it."""
    sent = {}
    respx.post(f"{BASE}/v1/audio/speech").mock(
        side_effect=lambda r: (sent.update(__import__("json").loads(r.content))
                               or httpx.Response(200, content=b"wav")))

    _provider(voice="af_bella(2)+af_heart(1)").speak("hello")
    assert sent["voice"] == "af_bella(2)+af_heart(1)"


@respx.mock
def test_an_error_and_an_empty_body_both_become_tts_errors():
    from app.providers.tts.base import TTSError

    respx.post(f"{BASE}/v1/audio/speech").mock(
        return_value=httpx.Response(400, text="voice 'nope' not found"))
    with pytest.raises(TTSError, match="not found"):
        _provider().speak("hello")

    respx.post(f"{BASE}/v1/audio/speech").mock(return_value=httpx.Response(200, content=b""))
    with pytest.raises(TTSError, match="empty body"):
        _provider().speak("hello")


def test_an_unreachable_server_is_a_clean_error_not_a_traceback():
    from app.providers.tts.base import TTSError

    with pytest.raises(TTSError, match="unreachable"):
        _provider(base_url="http://127.0.0.1:9").speak("hello")


# ------------------------------------------------------------- voices ---
@pytest.mark.parametrize("payload", [
    VOICE_IDS,                                             # a bare list
    {"voices": VOICE_IDS},                                 # wrapped
    {"voices": [{"id": v} for v in VOICE_IDS]},            # objects
])
@respx.mock
def test_every_shape_the_voices_endpoint_has_had_normalises_the_same(payload):
    respx.get(f"{BASE}/v1/audio/voices").mock(return_value=httpx.Response(200, json=payload))
    voices = _provider().voices()
    assert [v["id"] for v in voices] == VOICE_IDS
    assert all(v["name"] for v in voices)


@respx.mock
def test_a_voice_id_is_labelled_for_a_human_reading_the_picker():
    respx.get(f"{BASE}/v1/audio/voices").mock(
        return_value=httpx.Response(200, json={"voices": ["af_heart", "bm_george"]}))
    names = {v["id"]: v["name"] for v in _provider().voices()}
    assert names["af_heart"] == "Heart (American female)"
    assert names["bm_george"] == "George (British male)"


@respx.mock
def test_an_unreachable_voices_endpoint_yields_no_voices_rather_than_raising():
    """The picker degrades to a text box; it must not break the form."""
    respx.get(f"{BASE}/v1/audio/voices").mock(return_value=httpx.Response(500))
    assert _provider().voices() == []


# ------------------------------------------------------------- health ---
@respx.mock
def test_health_reports_the_served_voices_and_the_one_selected():
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx.get(f"{BASE}/v1/audio/voices").mock(
        return_value=httpx.Response(200, json={"voices": VOICE_IDS}))

    out = _provider().health()
    assert out["reachable"] is True
    assert out["voice_count"] == len(VOICE_IDS)
    assert out["voice_name"] == "Heart (American female)"
    assert "error" not in out


@respx.mock
def test_a_voice_the_server_does_not_have_is_caught_before_a_job_runs():
    """Otherwise it surfaces as a 400 on the first phrase, minutes in."""
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{BASE}/v1/audio/voices").mock(
        return_value=httpx.Response(200, json={"voices": VOICE_IDS}))

    out = _provider(voice="af_heartt").health()
    assert out["reachable"] is True
    assert "af_heartt" in out["error"]
    assert "af_heart" in out["error"]          # suggests the near miss


@respx.mock
def test_a_blend_is_not_reported_as_a_missing_voice():
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{BASE}/v1/audio/voices").mock(
        return_value=httpx.Response(200, json={"voices": VOICE_IDS}))

    out = _provider(voice="af_bella(2)+af_heart(1)").health()
    assert "error" not in out


@respx.mock
def test_health_falls_back_when_there_is_no_health_route():
    """Older builds have no /health. Reachability must not depend on it."""
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/v1/audio/voices").mock(
        return_value=httpx.Response(200, json={"voices": VOICE_IDS}))

    out = _provider().health()
    assert out["reachable"] is True and out["probe"] == "/health"


def test_an_unreachable_server_health_says_how_to_start_one():
    out = _provider(base_url="http://127.0.0.1:9").health()
    assert out["reachable"] is False
    assert "local-tts" in out["error"]


# ------------------------------------------- two servers, one adapter ---
@respx.mock
def test_the_openapi_servers_voices_path_is_tried_first():
    """Kokoro-TTS-OpenAPI serves /voices; Kokoro-FastAPI serves
    /v1/audio/voices. Asking both beats making the user say which they run."""
    rich = respx.get(f"{BASE}/voices").mock(return_value=httpx.Response(200, json={
        "voices": [
            {"id": "af_heart", "name": "Heart", "gender": "female",
             "accent": "American", "lang": "a", "grade": "A", "default": True},
            {"id": "bm_george", "name": "George", "gender": "male",
             "accent": "British", "lang": "b", "grade": "C"},
        ]}))
    fastapi = respx.get(f"{BASE}/v1/audio/voices").mock(
        return_value=httpx.Response(200, json={"voices": VOICE_IDS}))

    voices = _provider().voices()

    assert rich.call_count == 1 and fastapi.call_count == 0
    # the server's own description wins, and the quality grade survives --
    # an A voice and a C voice are audibly different
    assert voices[0] == {"id": "af_heart", "name": "Heart -- American female (A)"}
    assert voices[1]["name"] == "George -- British male (C)"


@respx.mock
def test_a_server_without_the_openapi_path_falls_through_to_the_other():
    respx.get(f"{BASE}/voices").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/v1/audio/voices").mock(
        return_value=httpx.Response(200, json={"voices": VOICE_IDS}))

    assert [v["id"] for v in _provider().voices()] == VOICE_IDS


@respx.mock
def test_an_empty_list_from_one_path_does_not_stop_the_other_being_tried():
    """A server can answer 200 with nothing rather than 404."""
    respx.get(f"{BASE}/voices").mock(return_value=httpx.Response(200, json={"voices": []}))
    respx.get(f"{BASE}/v1/audio/voices").mock(
        return_value=httpx.Response(200, json={"voices": VOICE_IDS}))

    assert [v["id"] for v in _provider().voices()] == VOICE_IDS


@respx.mock
def test_a_bearer_token_is_sent_only_when_a_key_variable_is_named(monkeypatch):
    """Kokoro-TTS-OpenAPI demands one when KOKORO_API_KEY is set on the
    server side; most self-hosted setups have none at all."""
    monkeypatch.setenv("REELFORGE_PATHS__DATA_DIR", "/nonexistent-for-this-test")
    monkeypatch.setenv("KOKORO_API_KEY", "tok-123")
    seen = {}
    respx.post(f"{BASE}/v1/audio/speech").mock(side_effect=lambda r: (
        seen.update(auth=r.headers.get("Authorization")) or httpx.Response(200, content=b"w")))

    _provider(api_key_env="KOKORO_API_KEY").speak("hi")
    assert seen["auth"] == "Bearer tok-123"

    seen.clear()
    _provider().speak("hi")                       # no api_key_env configured
    assert seen["auth"] is None


# ------------------------------------------------------- registration ---
def test_the_profile_builds_through_the_factory_with_the_right_defaults():
    from app.providers.tts import PROVIDERS, build_tts, voice_field

    assert "kokoro" in PROVIDERS
    assert voice_field("kokoro") == "voice"

    engine = build_tts(None, {"provider": "kokoro"})
    assert engine.name == "kokoro"
    # Kokoro-TTS-OpenAPI's port; the Kokoro-FastAPI container is 8880 and a
    # profile pointing there sets base_url itself
    assert engine.base_url.endswith(":8080")
    assert engine.voice == "af_heart" and engine.output_mime == "audio/wav"


def test_a_per_job_voice_override_reaches_the_engine():
    """Which voice narrates is a decision about one reel, not a setting."""
    from app.providers.tts import build_tts

    assert build_tts(None, {"provider": "kokoro", "voice": "bm_george"}).voice == "bm_george"


def test_the_config_file_block_is_migrated_into_a_profile():
    """`_migrate_legacy` walks a hardcoded tuple of adapter names -- a block it
    does not list is silently dropped, which is exactly how a new adapter goes
    missing."""
    from app.config import load_config

    cfg = load_config()
    assert cfg.tts.profiles["kokoro"].adapter == "kokoro"
    assert cfg.tts.profiles["local"].adapter == "local"      # still generic
