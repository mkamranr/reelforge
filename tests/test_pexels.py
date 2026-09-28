"""The Pexels adapter against a mocked API.

Search, pick by seed, download, and hand the stage what it expects: a PNG for
a still, a directory of frames for a clip. The API is respx; ffmpeg is real,
because trimming and exploding a clip is the one step with a tool in it.

The seed tests are the load-bearing ones. The stage keeps an asset whose
prompt and seed are unchanged rather than fetching it again, and that promise
is only true if the same seed lands on the same photograph.
"""
from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

respx = pytest.importorskip("respx")

BASE = "https://api.pexels.com"
CDN = "https://images.pexels.com"
VIDEO_CDN = "https://videos.pexels.com"


def _jpeg_bytes(colour=(180, 90, 40), size=(64, 96)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "JPEG")
    return buf.getvalue()


def _photo(pid: int) -> dict:
    return {"id": pid, "width": 2000, "height": 3000,
            "url": f"https://www.pexels.com/photo/{pid}/",
            "photographer": f"Photographer {pid}",
            "photographer_url": f"https://www.pexels.com/@p{pid}",
            "src": {"portrait": f"{CDN}/photos/{pid}/portrait.jpg",
                    "large2x": f"{CDN}/photos/{pid}/large2x.jpg",
                    "original": f"{CDN}/photos/{pid}/original.jpg"}}


def _video(vid: int, files: list[dict] | None = None, duration: int = 20) -> dict:
    return {"id": vid, "duration": duration,
            "url": f"https://www.pexels.com/video/{vid}/",
            "user": {"name": f"Filmmaker {vid}", "url": f"https://www.pexels.com/@f{vid}"},
            "video_files": files if files is not None else [
                {"id": 1, "quality": "sd", "width": 640, "height": 1138,
                 "fps": 30.0, "link": f"{VIDEO_CDN}/{vid}/sd.mp4"},
                {"id": 2, "quality": "hd", "width": 1080, "height": 1920,
                 "fps": 30.0, "link": f"{VIDEO_CDN}/{vid}/hd.mp4"},
                {"id": 3, "quality": "hd", "width": 2160, "height": 3840,
                 "fps": 30.0, "link": f"{VIDEO_CDN}/{vid}/uhd.mp4"},
            ]}


@pytest.fixture(autouse=True)
def _isolated_secrets(tmp_path, monkeypatch):
    """Point the settings store at an empty directory for every test here.

    `secret()` reads the environment first and falls back to
    `data/secrets.json`. Without this, a machine that has a real Pexels key
    saved through the Settings page fails the "no key configured" test --
    which is exactly what happened the first time one was added.
    """
    monkeypatch.setenv("REELFORGE_PATHS__DATA_DIR", str(tmp_path / "data"))


def _provider(monkeypatch, key="test-key-123", **extra):
    from app.providers.visuals.pexels import PexelsProvider

    monkeypatch.setenv("PEXELS_API_KEY", key) if key else \
        monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    settings = {"api_key_env": "PEXELS_API_KEY", "pool": 15, "request_timeout": 10}
    settings.update(extra)
    return PexelsProvider(settings)


# ------------------------------------------------------------- stills ---
@respx.mock
def test_a_still_is_searched_downloaded_and_saved_as_png(tmp_path, monkeypatch):
    seen = {}

    def on_search(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"photos": [_photo(11), _photo(22)]})

    respx.get(f"{BASE}/v1/search").mock(side_effect=on_search)
    respx.get(url__startswith=CDN).mock(
        return_value=httpx.Response(200, content=_jpeg_bytes()))

    out = tmp_path / "still-1.png"
    result = _provider(monkeypatch).still(
        "copper wires meeting at a glowing junction, macro. Vertical composition",
        out, width=1152, height=1536, seed=0, query="copper wire macro")

    assert out.exists() and result.width == 64 and result.height == 96
    assert result.seed == 0
    # the key travels in a bare Authorization header, not as a Bearer token
    assert seen["auth"] == "test-key-123"
    assert "query=copper+wire+macro" in seen["url"]
    assert "orientation=portrait" in seen["url"]
    assert result.meta["photographer"] == "Photographer 11"
    assert result.meta["url"] == "https://www.pexels.com/photo/11/"
    assert result.meta["source"] == "pexels"
    # the intermediate download is cleaned up
    assert not list(tmp_path.glob("*.raw*"))


@respx.mock
def test_the_photo_is_requested_at_the_frames_exact_size(tmp_path, monkeypatch):
    """None of Pexels' named sizes is wide enough for a 1080 reel -- `portrait`
    is 800x1200, `large2x` 867x1300 -- and `original` is 4128x6192 and 3.5 MB.
    The CDN resizes on request, so ask it for the frame."""
    asked = {}

    def on_download(request):
        asked["url"] = str(request.url)
        return httpx.Response(200, content=_jpeg_bytes())

    respx.get(f"{BASE}/v1/search").mock(
        return_value=httpx.Response(200, json={"photos": [_photo(11)]}))
    respx.get(url__startswith=CDN).mock(side_effect=on_download)

    _provider(monkeypatch).still("p", tmp_path / "s.png", width=1152, height=1536,
                                 seed=0, query="wire")
    assert "/original.jpg" in asked["url"]
    assert "w=1152" in asked["url"] and "h=1536" in asked["url"]
    assert "fit=crop" in asked["url"]


def test_a_response_without_an_original_falls_back_to_a_named_size():
    from app.providers.visuals.pexels import _sized

    assert _sized({"portrait": "P", "large": "L"}, 1080, 1920) == "P"
    assert _sized({"large": "L"}, 1080, 1920) == "L"
    assert _sized({}, 1080, 1920) == ""
    # an original that already carries a query string keeps it
    out = _sized({"original": "https://x/y.jpg?v=2"}, 1080, 1920)
    assert out.startswith("https://x/y.jpg?v=2&") and "w=1080" in out


@respx.mock
def test_the_api_key_is_never_sent_to_the_cdn(tmp_path, monkeypatch):
    """A single httpx client sends its default headers to an absolute URL too,
    which would hand the key to a host that never asked for it."""
    downloads = []

    def on_download(request):
        downloads.append(dict(request.headers))
        return httpx.Response(200, content=_jpeg_bytes())

    respx.get(f"{BASE}/v1/search").mock(
        return_value=httpx.Response(200, json={"photos": [_photo(11)]}))
    respx.get(url__startswith=CDN).mock(side_effect=on_download)

    _provider(monkeypatch).still("a prompt", tmp_path / "s.png", width=1152, height=1536,
                                 seed=0, query="copper wire")

    assert downloads, "the CDN was never called"
    for headers in downloads:
        assert "authorization" not in {k.lower() for k in headers}


@respx.mock
def test_the_same_seed_picks_the_same_photo_and_a_different_seed_does_not(tmp_path, monkeypatch):
    respx.get(f"{BASE}/v1/search").mock(
        return_value=httpx.Response(200, json={"photos": [_photo(n) for n in (10, 20, 30, 40)]}))
    respx.get(url__startswith=CDN).mock(
        return_value=httpx.Response(200, content=_jpeg_bytes()))

    def pick(seed):
        # a fresh provider each time: within one run the no-repeat rule applies
        return _provider(monkeypatch).still("p", tmp_path / f"s{seed}.png", width=8, height=8,
                                            seed=seed, query="wire").meta["id"]

    assert pick(2) == pick(2)
    assert pick(2) != pick(3)


@respx.mock
def test_two_assets_in_one_run_never_get_the_same_photo(tmp_path, monkeypatch):
    """Two scenes under one heading showing the identical picture reads as a
    bug, and the same query for adjacent scenes is normal."""
    respx.get(f"{BASE}/v1/search").mock(
        return_value=httpx.Response(200, json={"photos": [_photo(10), _photo(20)]}))
    respx.get(url__startswith=CDN).mock(
        return_value=httpx.Response(200, content=_jpeg_bytes()))

    provider = _provider(monkeypatch)
    first = provider.still("p", tmp_path / "a.png", width=8, height=8, seed=4, query="wire")
    second = provider.still("p", tmp_path / "b.png", width=8, height=8, seed=4, query="wire")
    assert first.meta["id"] != second.meta["id"]


@respx.mock
def test_an_empty_result_widens_the_query_once_before_giving_up(tmp_path, monkeypatch):
    from app.providers.visuals.base import VisualsError

    terms = []

    def on_search(request):
        term = httpx.QueryParams(request.url.query).get("query")
        terms.append(term)
        photos = [_photo(9)] if term == "copper wire" else []
        return httpx.Response(200, json={"photos": photos})

    respx.get(f"{BASE}/v1/search").mock(side_effect=on_search)
    respx.get(url__startswith=CDN).mock(
        return_value=httpx.Response(200, content=_jpeg_bytes()))

    result = _provider(monkeypatch).still("p", tmp_path / "s.png", width=8, height=8,
                                          seed=0, query="copper wire junction")
    assert terms == ["copper wire junction", "copper wire"]
    assert result.meta["id"] == 9

    respx.get(f"{BASE}/v1/search").mock(return_value=httpx.Response(200, json={"photos": []}))
    with pytest.raises(VisualsError, match="nothing for"):
        _provider(monkeypatch).still("p", tmp_path / "t.png", width=8, height=8,
                                     seed=0, query="nonsense term")


@respx.mock
def test_without_a_query_the_search_term_is_squeezed_out_of_the_prompt(tmp_path, monkeypatch):
    """The art director's `query` is optional -- the call can be switched off
    or fail, and the reel still has to get pictures."""
    seen = {}

    def on_search(request):
        seen["query"] = httpx.QueryParams(request.url.query).get("query")
        return httpx.Response(200, json={"photos": [_photo(1)]})

    respx.get(f"{BASE}/v1/search").mock(side_effect=on_search)
    respx.get(url__startswith=CDN).mock(
        return_value=httpx.Response(200, content=_jpeg_bytes()))

    _provider(monkeypatch).still(
        "Copper wires meeting at a glowing junction inside a dark housing, macro. "
        "Vertical composition, the middle of the frame kept calm, indigo accent light, "
        "cinematic, photoreal, shallow depth of field",
        tmp_path / "s.png", width=8, height=8, seed=0)

    assert seen["query"] == "copper wires meeting"


# -------------------------------------------------------------- clips ---
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not on PATH")
@respx.mock
def test_a_clip_is_downloaded_trimmed_and_exploded_into_reel_frames(tmp_path, monkeypatch):
    # a real 4 s 24 fps landscape clip, so both the trim and the crop are exercised
    source = tmp_path / "src.mp4"
    subprocess.run([shutil.which("ffmpeg"), "-y", "-v", "error", "-f", "lavfi",
                    "-i", "testsrc=size=1280x720:rate=24:duration=4", "-pix_fmt", "yuv420p",
                    str(source)], check=True)

    respx.get(f"{BASE}/videos/search").mock(
        return_value=httpx.Response(200, json={"videos": [_video(77, duration=4)]}))
    respx.get(url__startswith=VIDEO_CDN).mock(
        return_value=httpx.Response(200, content=source.read_bytes()))

    out_dir = tmp_path / "clip-1"
    result = _provider(monkeypatch, clip_start=0.5).clip(
        "slow dolly along a bundle of wires", out_dir, seconds=1.0, fps=30,
        width=1080, height=1920, seed=0, query="copper wire")

    frames = sorted(out_dir.glob("*.jpg"))
    assert result.frames == len(frames) and 28 <= len(frames) <= 31
    from PIL import Image

    with Image.open(frames[0]) as image:
        assert image.size == (1080, 1920)
    assert result.source and result.source.exists()
    assert result.meta["photographer"] == "Filmmaker 77"
    assert result.meta["start"] == 0.5


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not on PATH")
@respx.mock
def test_the_smallest_file_that_still_fills_the_frame_is_chosen(tmp_path, monkeypatch):
    """Both the 1080 and the 4K version crop to the same 1080x1920, and only
    one of them is a 300 MB download."""
    asked = {}

    def on_download(request):
        asked["url"] = str(request.url)
        return httpx.Response(200, content=b"not really an mp4")

    respx.get(f"{BASE}/videos/search").mock(
        return_value=httpx.Response(200, json={"videos": [_video(77)]}))
    respx.get(url__startswith=VIDEO_CDN).mock(side_effect=on_download)

    from app.providers.visuals.base import VisualsError

    with pytest.raises(VisualsError):          # ffmpeg rejects the fake mp4
        _provider(monkeypatch).clip("p", tmp_path / "c", seconds=1.0, fps=30,
                                    width=1080, height=1920, seed=0, query="wire")
    assert asked["url"].endswith("/hd.mp4")    # not sd.mp4 (too narrow), not uhd.mp4


@respx.mock
def test_an_oversize_video_is_refused_rather_than_downloaded(tmp_path, monkeypatch):
    from app.providers.visuals.base import VisualsError

    respx.get(f"{BASE}/videos/search").mock(
        return_value=httpx.Response(200, json={"videos": [_video(77)]}))
    respx.get(url__startswith=VIDEO_CDN).mock(return_value=httpx.Response(
        200, content=b"x" * 64, headers={"content-length": str(900 * 1024 * 1024)}))

    with pytest.raises(VisualsError, match="ceiling"):
        _provider(monkeypatch).clip("p", tmp_path / "c", seconds=1.0, fps=30,
                                    width=1080, height=1920, seed=0, query="wire")
    assert not (tmp_path / "c.mp4").exists()


@respx.mock
def test_a_video_with_no_wide_enough_file_still_yields_the_widest_there_is(tmp_path, monkeypatch):
    from app.providers.visuals.pexels import _best_file

    narrow = [{"id": 1, "quality": "sd", "width": 640, "height": 1138, "link": "a"},
              {"id": 2, "quality": "sd", "width": 960, "height": 1706, "link": "b"}]
    assert _best_file(narrow, "hd")["width"] == 640
    assert _best_file([], "hd") is None


# ------------------------------------------------------------- health ---
def test_health_without_a_key_says_so_and_is_not_reachable(monkeypatch):
    out = _provider(monkeypatch, key="").health()
    assert out["reachable"] is False and out["authenticated"] is False
    assert "PEXELS_API_KEY" in out["error"]


@respx.mock
def test_health_reports_a_rejected_key_as_reachable_but_unauthenticated(monkeypatch):
    respx.get(f"{BASE}/v1/search").mock(return_value=httpx.Response(401, json={}))
    out = _provider(monkeypatch).health()
    assert out["reachable"] is True and out["authenticated"] is False
    assert "401" in out["error"]


@respx.mock
def test_a_good_probe_surfaces_what_is_left_of_the_quota(monkeypatch):
    respx.get(f"{BASE}/v1/search").mock(return_value=httpx.Response(
        200, json={"photos": [_photo(1)]},
        headers={"X-Ratelimit-Limit": "20000", "X-Ratelimit-Remaining": "19873",
                 "X-Ratelimit-Reset": "1700000000"}))
    out = _provider(monkeypatch).health()
    assert out["reachable"] is True and out["authenticated"] is True
    assert out["quota_remaining"] == 19873 and out["quota_limit"] == 20000
    assert "error" not in out


def test_an_unreachable_host_is_a_clean_health_result(monkeypatch):
    out = _provider(monkeypatch, base_url="http://127.0.0.1:9").health()
    assert out["reachable"] is False and "unreachable" in out["error"]


@respx.mock
def test_a_rate_limited_search_says_what_the_limit_is(tmp_path, monkeypatch):
    from app.providers.visuals.base import VisualsError

    respx.get(f"{BASE}/v1/search").mock(return_value=httpx.Response(429, text="slow down"))
    with pytest.raises(VisualsError, match="200 requests an hour"):
        _provider(monkeypatch).still("p", tmp_path / "s.png", width=8, height=8,
                                     seed=0, query="wire")


# -------------------------------------------------------- the factory ---
def test_the_profile_builds_through_the_factory_and_offers_no_audio(monkeypatch):
    from app.providers.visuals import build_visuals

    provider = build_visuals(None, {"profile": "pexels"})
    assert provider.name == "pexels"
    assert provider.supports_clips is True
    assert provider.supports_audio is False


def test_squeeze_query_keeps_nouns_and_drops_the_boilerplate():
    from app.providers.visuals.pexels import squeeze_query

    assert squeeze_query("A single brass key resting on dark slate, one hard beam") \
        == "brass key resting"
    assert squeeze_query("") == ""
    assert squeeze_query("the a of in on") == ""
