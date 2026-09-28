"""The settings API: profiles, keys, roles and gates.

These endpoints accept secrets, so the access guard and the "never return a key
value" rule are tested as carefully as the behaviour is.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REELFORGE_PATHS__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REELFORGE_EXECUTOR", "inline")
    monkeypatch.delenv("REELFORGE_ADMIN_TOKEN", raising=False)
    for key in ("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    import app.config
    import app.runner

    app.config.get_config.cache_clear()
    app.runner.mode.cache_clear()
    from app.main import app as fastapi_app

    return TestClient(fastapi_app)


# ---------------------------------------------------------------- read ----
def test_settings_reports_profiles_and_presets(client):
    body = client.get("/api/settings").json()
    assert body["llm"]["active"]
    assert {"all-local", "hybrid", "all-hosted"} <= set(body["llm"]["presets"])
    assert body["approval"]["stages"][0] == "ingest"


def test_legacy_config_migrates_into_profiles(client):
    """The committed config.yaml predates profiles and must keep working."""
    names = {p["name"] for p in client.get("/api/settings").json()["llm"]["profiles"]}
    assert {"anthropic", "openai", "ollama"} <= names


# ------------------------------------------------------------ profiles ----
def test_add_and_activate_a_profile(client):
    response = client.put("/api/settings/llm/profiles/groq", json={
        "adapter": "openai", "label": "Groq",
        "model": "llama-3.3-70b", "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY", "settings": {},
    })
    assert response.status_code == 200
    names = {p["name"] for p in response.json()["llm"]["profiles"]}
    assert "groq" in names

    activated = client.post("/api/settings/llm/active", json={"name": "groq"}).json()
    assert activated["llm"]["active"] == "groq"


def test_adapter_cannot_be_changed_on_an_existing_profile(client):
    """Settings mean different things per adapter; reinterpreting a base_url
    silently is worse than making the user create a new profile."""
    client.put("/api/settings/llm/profiles/mine", json={
        "adapter": "openai", "model": "x", "settings": {}})
    response = client.put("/api/settings/llm/profiles/mine", json={
        "adapter": "ollama", "model": "y", "settings": {}})
    assert response.status_code == 409
    assert "cannot be changed" in response.json()["detail"]


def test_the_profile_in_use_cannot_be_deleted(client):
    active = client.get("/api/settings").json()["llm"]["active"]
    response = client.delete(f"/api/settings/llm/profiles/{active}")
    assert response.status_code == 409
    assert "in use" in response.json()["detail"]


def test_upload_profile_cannot_be_removed(client):
    """A job must always be able to fall back to narration you supply."""
    client.post("/api/settings/tts/active", json={"name": "say"})
    response = client.delete("/api/settings/tts/profiles/upload")
    assert response.status_code == 409


def test_unknown_adapter_is_rejected(client):
    response = client.put("/api/settings/llm/profiles/x", json={
        "adapter": "not-a-thing", "settings": {}})
    assert response.status_code == 422


# --------------------------------------------------------------- roles ----
def test_hybrid_preset_routes_writing_to_the_hosted_profile(client):
    body = client.post("/api/settings/llm/roles/preset/hybrid", json={}).json()
    roles = body["llm"]["roles"]
    assert roles["content"]["profile"] == "anthropic"
    assert roles["storyboard"]["profile"] == "anthropic"
    # the cheap, guarded stage stays local
    assert roles["review"]["profile"] == "ollama"


def test_all_local_preset_clears_routing(client):
    client.post("/api/settings/llm/roles/preset/hybrid", json={})
    body = client.post("/api/settings/llm/roles/preset/all-local", json={}).json()
    assert all(not binding for binding in body["llm"]["roles"].values())


def test_preset_refuses_when_no_hosted_profile_exists(client):
    client.post("/api/settings/llm/active", json={"name": "ollama"})
    client.delete("/api/settings/llm/profiles/anthropic")
    client.delete("/api/settings/llm/profiles/openai")
    response = client.post("/api/settings/llm/roles/preset/hybrid", json={})
    assert response.status_code == 409
    assert "no hosted profile" in response.json()["detail"]


def test_a_role_cannot_name_a_missing_profile(client):
    response = client.put("/api/settings/llm/roles",
                          json={"content": {"profile": "nope"}})
    assert response.status_code == 404


# ------------------------------------------------------------ approval ----
def test_approval_gates_are_stored_in_pipeline_order(client):
    body = client.put("/api/settings/approval",
                      json={"manual_stages": ["render", "content"]}).json()
    assert body["approval"]["manual_stages"] == ["content", "render"]


def test_approval_presets_are_reported(client):
    assert client.put("/api/settings/approval", json={"manual_stages": []}
                      ).json()["approval"]["preset"] == "all-auto"
    every = client.get("/api/settings").json()["approval"]["stages"]
    assert client.put("/api/settings/approval", json={"manual_stages": every}
                      ).json()["approval"]["preset"] == "all-manual"


def test_unknown_stage_is_rejected(client):
    response = client.put("/api/settings/approval",
                          json={"manual_stages": ["not-a-stage"]})
    assert response.status_code == 422


def test_a_new_job_inherits_the_saved_gates(client):
    client.put("/api/settings/approval", json={"manual_stages": ["content"]})
    job = client.post("/api/jobs", json={
        "url": "https://github.com/ruvnet/ruflo", "autostart": False}).json()
    assert job["manual_stages"] == ["content"]


# ------------------------------------------------------------- secrets ----
def test_a_stored_key_is_never_returned(client):
    client.put("/api/settings/secrets",
               json={"name": "ELEVENLABS_API_KEY", "value": "sk_supersecretvalue"})
    body = client.get("/api/settings/secrets").text
    assert "supersecretvalue" not in body
    assert "sk_s...alue" in body
    assert "supersecretvalue" not in client.get("/api/settings").text


def test_secret_file_is_owner_only(client, tmp_path):
    import stat

    client.put("/api/settings/secrets", json={"name": "K", "value": "v"})
    mode = stat.S_IMODE((tmp_path / "secrets.json").stat().st_mode)
    assert mode == 0o600


def test_environment_shadows_a_stored_key(client, monkeypatch):
    """A key set in the UI has no effect while the env var exists, and the UI
    has to say so or the user loses an hour to it."""
    client.put("/api/settings/secrets",
               json={"name": "ELEVENLABS_API_KEY", "value": "from-ui"})
    monkeypatch.setenv("ELEVENLABS_API_KEY", "from-env")
    import app.config

    app.config.get_config.cache_clear()
    status = next(s for s in client.get("/api/settings/secrets").json()["secrets"]
                  if s["name"] == "ELEVENLABS_API_KEY")
    assert status["source"] == "environment"
    assert status["shadowed"] is True
    assert app.config.secret("ELEVENLABS_API_KEY") == "from-env"


def test_deleting_a_key_unsets_it(client):
    client.put("/api/settings/secrets", json={"name": "K", "value": "v"})
    assert client.delete("/api/settings/secrets/K").json()["state"] == "unset"


# -------------------------------------------------------------- access ----
def test_remote_requests_are_refused_without_a_token(monkeypatch):
    from fastapi import HTTPException

    from app.api.auth import require_admin

    monkeypatch.delenv("REELFORGE_ADMIN_TOKEN", raising=False)

    class Remote:
        client = type("C", (), {"host": "203.0.113.7"})()
        headers: dict = {}

    with pytest.raises(HTTPException) as raised:
        require_admin(Remote())
    assert raised.value.status_code == 403


def test_a_token_admits_a_remote_request(monkeypatch):
    from app.api.auth import require_admin

    monkeypatch.setenv("REELFORGE_ADMIN_TOKEN", "letmein")

    class Remote:
        client = type("C", (), {"host": "203.0.113.7"})()
        headers = {"authorization": "Bearer letmein"}

    require_admin(Remote())  # does not raise


def test_a_wrong_token_is_refused(monkeypatch):
    from fastapi import HTTPException

    from app.api.auth import require_admin

    monkeypatch.setenv("REELFORGE_ADMIN_TOKEN", "letmein")

    class Remote:
        client = type("C", (), {"host": "127.0.0.1"})()
        headers = {"authorization": "Bearer wrong"}

    with pytest.raises(HTTPException) as raised:
        require_admin(Remote())
    assert raised.value.status_code == 401


# ------------------------------------------- keys reach the providers -----
def test_a_key_saved_in_the_ui_reaches_the_provider(client, monkeypatch):
    """The bug this covers: the key was stored correctly and reported as set,
    but the ElevenLabs adapter read os.environ directly through its key ring and
    never saw it, so the profile tested as unreachable with a key in place."""
    from app.providers.keyring import KeyRing

    client.put("/api/settings/secrets",
               json={"name": "ELEVENLABS_API_KEY", "value": "sk_storedinui1234"})
    import app.config

    app.config.get_config.cache_clear()

    ring = KeyRing.from_env(["ELEVENLABS_API_KEY"])
    assert len(ring) == 1
    assert ring.current.value == "sk_storedinui1234"


def test_the_environment_still_wins_for_the_key_ring(client, monkeypatch):
    from app.providers.keyring import KeyRing

    client.put("/api/settings/secrets",
               json={"name": "ELEVENLABS_API_KEY", "value": "from-ui"})
    monkeypatch.setenv("ELEVENLABS_API_KEY", "from-env")
    import app.config

    app.config.get_config.cache_clear()
    assert KeyRing.from_env(["ELEVENLABS_API_KEY"]).current.value == "from-env"


def test_comma_separated_keys_still_build_a_rotation_pool(client):
    from app.providers.keyring import KeyRing

    client.put("/api/settings/secrets",
               json={"name": "ELEVENLABS_API_KEY", "value": "sk_one111,sk_two222"})
    import app.config

    app.config.get_config.cache_clear()
    assert len(KeyRing.from_env(["ELEVENLABS_API_KEY"])) == 2


# ------------------------------------------------- editing, not replacing --
def test_editing_a_profile_keeps_the_rest_of_it(client):
    """Updating one field must not require deleting and re-creating the
    profile, and must not silently drop the settings you did not mention."""
    client.put("/api/settings/tts/profiles/elevenlabs", json={
        "adapter": "elevenlabs", "model": "eleven_multilingual_v2",
        "api_key_env": "ELEVENLABS_API_KEY",
        "settings": {"voice_id": "voice-one", "stability": 0.5,
                     "max_characters_per_job": 4000},
    })
    # change only the voice
    body = client.put("/api/settings/tts/profiles/elevenlabs", json={
        "adapter": "elevenlabs", "settings": {"voice_id": "voice-two"},
    }).json()
    profile = next(p for p in body["tts"]["profiles"] if p["name"] == "elevenlabs")
    assert profile["settings"]["voice_id"] == "voice-two"
    assert profile["settings"]["stability"] == 0.5          # untouched
    assert profile["settings"]["max_characters_per_job"] == 4000
    assert profile["api_key_env"] == "ELEVENLABS_API_KEY"   # key still linked


def test_each_adapter_reports_its_own_editable_fields(client):
    body = client.get("/api/settings").json()
    fields = {p["name"]: {f["key"] for f in p["fields"]}
              for p in body["tts"]["profiles"] + body["llm"]["profiles"]}
    assert "voice_id" in fields["elevenlabs"]
    assert "keep_alive" in fields["ollama"]
    assert "json_mode" in fields["openai"]
    # the voice field is typed so the UI renders a picker, not a text box
    voice = next(f for p in body["tts"]["profiles"] if p["name"] == "elevenlabs"
                 for f in p["fields"] if f["key"] == "voice_id")
    assert voice["type"] == "voice"


def test_a_pasted_key_is_moved_into_the_secrets_store(client, tmp_path):
    """The field wants a variable name. Pasting the key itself put it in
    settings.yaml, which is not permission-restricted, and left the profile
    with no usable key."""
    body = client.put("/api/settings/llm/profiles/openrouter", json={
        "adapter": "openai", "base_url": "https://openrouter.ai/api/v1",
        "model": "anthropic/claude-sonnet-4.5",
        "api_key_env": "sk-or-v1-abcdef0123456789", "settings": {},
    }).json()
    assert "notice" in body
    profile = next(p for p in body["llm"]["profiles"] if p["name"] == "openrouter")
    assert profile["api_key_env"] == "OPENROUTER_API_KEY"
    assert "sk-or-v1" not in (tmp_path / "settings.yaml").read_text()
    from app import settings_store

    assert settings_store.read_secrets()["OPENROUTER_API_KEY"] == "sk-or-v1-abcdef0123456789"


def test_a_per_job_voice_reaches_the_engine(client):
    """Which voice narrates is a decision about one reel, not a permanent
    setting, so the job's choice must override the profile's."""
    from app.config import load_config
    from app.providers.tts import build_tts

    client.put("/api/settings/tts/profiles/elevenlabs", json={
        "adapter": "elevenlabs", "api_key_env": "ELEVENLABS_API_KEY",
        "settings": {"voice_id": "profile-default"}})
    client.put("/api/settings/secrets",
               json={"name": "ELEVENLABS_API_KEY", "value": "sk_test1234"})
    import app.config

    app.config.get_config.cache_clear()
    cfg = load_config()
    assert build_tts(cfg, {"provider": "elevenlabs"}).voice_id == "profile-default"
    assert build_tts(cfg, {"provider": "elevenlabs",
                           "voice": "job-choice"}).voice_id == "job-choice"


def test_every_catalogued_service_can_be_added_as_a_profile(client):
    """The catalogue is what the Add-profile picker offers, so each entry has to
    produce a profile the factory can actually build."""
    from app.config import OPENAI_COMPATIBLE_ENDPOINTS

    for name, entry in OPENAI_COMPATIBLE_ENDPOINTS.items():
        body = client.put(f"/api/settings/llm/profiles/{name}", json={
            "adapter": "openai", "label": entry["label"],
            "model": entry.get("model", ""), "base_url": entry["base_url"],
            "api_key_env": entry.get("api_key_env"),
            "settings": {"json_mode": entry.get("json_mode", "json_schema")},
        }).json()
        created = next(p for p in body["llm"]["profiles"] if p["name"] == name)
        assert created["base_url"] == entry["base_url"]

    from app.config import load_config
    from app.providers.llm import build_llm

    cfg = load_config()
    for name in OPENAI_COMPATIBLE_ENDPOINTS:
        assert build_llm("content", cfg, {"profile": name}).base_url


def test_a_pexels_profile_is_addable_and_its_key_shows_up_masked(client, tmp_path, monkeypatch):
    """The settings page is generated from the adapter's field list, so a new
    visuals adapter needs no UI work -- but only if the fields, the defaults
    and the key discovery all line up."""
    import json

    # the environment wins over the settings store, so a real key on this
    # machine would answer for the stored one and the test would prove nothing
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    body = client.put("/api/settings/visuals/profiles/stock", json={
        "adapter": "pexels", "label": "Pexels",
        "api_key_env": "PEXELS_API_KEY",
        "settings": {"orientation": "portrait", "pool": 20},
    }).json()
    profile = next(p for p in body["visuals"]["profiles"] if p["name"] == "stock")
    assert profile["adapter"] == "pexels"
    assert {"api_key_env", "orientation", "pool", "clip_start"} <= {
        f["key"] for f in profile["fields"]}
    assert "pexels" in body["visuals"]["adapters"]

    client.put("/api/settings/secrets",
               json={"name": "PEXELS_API_KEY", "value": "563492ad-secret-value"})
    secrets = client.get("/api/settings/secrets").json()["secrets"]
    entry = next(s for s in secrets if s["name"] == "PEXELS_API_KEY")
    assert entry["state"] == "set" and "563492ad-secret-value" not in json.dumps(secrets)
    assert "563492ad-secret-value" not in json.dumps(client.get("/api/settings").json())
    assert "563492ad-secret-value" not in (tmp_path / "settings.yaml").read_text()

    # and the key actually reaches the provider the factory builds
    from app.config import load_config
    from app.providers.visuals import build_visuals

    import app.config
    app.config.get_config.cache_clear()
    provider = build_visuals(load_config(), {"profile": "stock"})
    assert provider.name == "pexels" and provider._key == "563492ad-secret-value"
    assert provider.pool == 20


def test_a_kokoro_profile_gets_a_voice_picker_not_a_text_box(client):
    """The voice field's *type* is what turns the Settings control into a
    picker fed by the server's own list. `local` keeps its plain text box,
    because Piper has no voices endpoint and an always-empty picker is worse."""
    body = client.put("/api/settings/tts/profiles/kokoro", json={
        "adapter": "kokoro",
        "settings": {"base_url": "http://localhost:8880", "voice": "af_heart",
                     "speed": 1.0},
    }).json()
    profile = next(p for p in body["tts"]["profiles"] if p["name"] == "kokoro")
    assert profile["adapter"] == "kokoro"
    assert profile["api_key_env"] is None          # self-hosted: no key at all
    fields = {f["key"]: f["type"] for f in profile["fields"]}
    assert fields["voice"] == "voice"
    assert {"base_url", "speed", "response_format"} <= set(fields)
    assert "kokoro" in body["tts"]["adapters"]

    other = next(p for p in body["tts"]["profiles"] if p["name"] == "local")
    assert {f["key"]: f["type"] for f in other["fields"]}["voice"] == "text"


def test_a_transient_server_error_is_not_a_mode_failure():
    """Google returned 503 "high demand" and all three structured-output modes
    were consumed in under a second, reporting the cause as unsupported
    structured output. A 5xx means retry, not change the request."""
    from app.providers.llm.base import TransientError
    from app.providers.llm.openai_compat import OpenAICompatProvider

    assert 503 in OpenAICompatProvider.RETRY_STATUS
    assert 429 in OpenAICompatProvider.RETRY_STATUS
    assert 400 not in OpenAICompatProvider.RETRY_STATUS   # that IS a bad request
    assert issubclass(TransientError, Exception)


def test_a_self_hosted_endpoint_needs_no_key_to_be_reachable(client, monkeypatch):
    """A local vLLM takes no credential. Inheriting `api_key_env` from the
    OpenAI defaults made a working endpoint report unreachable for want of a key
    it never asked for -- conflating "reachable" with "authenticated"."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from app.config import ProviderProfile, is_hosted

    local = ProviderProfile(adapter="openai", base_url="http://192.168.1.10:8000/v1")
    remote = ProviderProfile(adapter="openai", base_url="https://api.openai.com/v1")
    assert not is_hosted(local)
    assert is_hosted(remote)

    from app.config import OPENAI_COMPATIBLE_ENDPOINTS

    # the self-hosted entries carry no key variable at all
    assert OPENAI_COMPATIBLE_ENDPOINTS["vllm"]["api_key_env"] is None
    assert OPENAI_COMPATIBLE_ENDPOINTS["lmstudio"]["api_key_env"] is None
    assert OPENAI_COMPATIBLE_ENDPOINTS["openai"]["api_key_env"] == "OPENAI_API_KEY"
