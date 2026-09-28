"""ComfyUI over HTTP.

The server's API is small: POST /prompt queues a workflow and returns an id,
GET /history/<id> reports its outputs once it has run, GET /view streams a
produced file. Progress is available over a websocket, but polling /history
every couple of seconds is enough for something that takes minutes and needs
no per-step feedback.

A clip comes back as an mp4 and is turned into frames with ffmpeg, scaled to
the reel's frame and resampled to its frame rate, so the storyboard pastes
frame `int((t - t0) * FPS)` and never decodes video. That conversion, and the
one to the mixer's wav, live in `media.py` and are re-exported here: they were
this module's own until a second adapter needed them.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from app.providers.visuals import workflow as wf
from app.providers.visuals.base import AudioResult, ClipResult, StillResult, VisualsError, VisualsProvider
from app.providers.visuals.media import extract_frames, to_wav48

PROBE_TIMEOUT = 5.0
POLL_SECONDS = 2.0
#: ComfyUI's own default; kept as a named constant so the health report can say it
DEFAULT_PORT = 8188


def _repo_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    from app.config import REPO_ROOT

    return REPO_ROOT / path


class ComfyUIProvider(VisualsProvider):
    name = "comfyui"

    def __init__(self, settings: dict[str, Any]):
        super().__init__(settings)
        self.base_url = str(settings.get("base_url") or f"http://localhost:{DEFAULT_PORT}").rstrip("/")
        self.image_workflow = str(settings.get("image_workflow") or "")
        self.video_workflow = str(settings.get("video_workflow") or "")
        self.image_nodes = dict(settings.get("image_nodes") or {})
        self.video_nodes = dict(settings.get("video_nodes") or {})
        self.lightning = settings.get("lightning")
        self.prompt_enhancer = settings.get("prompt_enhancer")
        self.timeout = float(settings.get("request_timeout") or 1800)
        self.client_id = f"reelforge-{uuid.uuid4().hex[:12]}"
        self.audio_workflow = str(settings.get("audio_workflow") or "")
        self.audio_nodes = dict(settings.get("audio_nodes") or {})
        self.audio_enhancer = settings.get("audio_enhancer")
        self.supports_clips = bool(self.video_workflow)
        self.supports_audio = bool(self.audio_workflow)

        headers = {}
        key_env = settings.get("api_key_env")
        if key_env:
            from app.config import secret

            key = secret(key_env)
            if key:
                headers["Authorization"] = f"Bearer {key}"
        self._client = httpx.Client(base_url=self.base_url, headers=headers,
                                    timeout=httpx.Timeout(60.0, connect=10.0))

    # ------------------------------------------------------------ http ---
    def _get(self, path: str, **params) -> httpx.Response:
        try:
            response = self._client.get(path, params=params or None)
        except httpx.HTTPError as exc:
            raise VisualsError(f"{self.base_url} unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise VisualsError(f"GET {path} -> HTTP {response.status_code}: {response.text[:200]}")
        return response

    def submit(self, nodes: dict[str, Any]) -> str:
        try:
            response = self._client.post("/prompt", json={"prompt": nodes, "client_id": self.client_id})
        except httpx.HTTPError as exc:
            raise VisualsError(f"{self.base_url} unreachable: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text[:600]
            try:
                body = response.json()
                errors = body.get("node_errors") or {}
                if errors:
                    first = next(iter(errors.values()))
                    messages = [e.get("message", "") for e in first.get("errors", [])]
                    detail = "; ".join(m for m in messages if m) or detail
                elif body.get("error"):
                    err = body["error"]
                    detail = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            except ValueError:
                pass
            raise VisualsError(f"ComfyUI rejected the workflow: {detail}")
        body = response.json()
        prompt_id = body.get("prompt_id")
        if not prompt_id:
            raise VisualsError(f"ComfyUI returned no prompt_id: {json.dumps(body)[:200]}")
        return prompt_id

    def wait(self, prompt_id: str, *, progress=None) -> dict[str, Any]:
        """Poll until the prompt has outputs, or fail with the server's reason."""
        deadline = time.monotonic() + self.timeout
        started = time.monotonic()
        last_note = 0.0
        while True:
            history = self._get(f"/history/{prompt_id}").json()
            entry = history.get(prompt_id)
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    raise VisualsError("ComfyUI reported an error: " + _status_message(status))
                outputs = entry.get("outputs") or {}
                if outputs:
                    return outputs
                if status.get("completed"):
                    raise VisualsError("the workflow finished without producing an output file "
                                       "-- does it end in a Save node?")
            now = time.monotonic()
            if now > deadline:
                raise VisualsError(f"ComfyUI did not finish in {int(self.timeout)} s")
            if progress and now - last_note > 20:
                last_note = now
                progress(f"generating ... {int(now - started)} s")
            time.sleep(POLL_SECONDS)

    def outputs_of(self, outputs: dict[str, Any], kind: str) -> list[dict[str, Any]]:
        """Every saved file of the wanted kind, in node order."""
        wanted = wf.EXT_FOR[kind]
        files: list[dict[str, Any]] = []
        for node_out in outputs.values():
            if not isinstance(node_out, dict):
                continue
            for entries in node_out.values():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if isinstance(entry, dict) and str(entry.get("filename", "")).lower().endswith(wanted) \
                            and entry.get("type", "output") != "temp":
                        files.append(entry)
        return files

    def download(self, entry: dict[str, Any], target: Path) -> Path:
        response = self._get("/view", filename=entry["filename"],
                             subfolder=entry.get("subfolder", ""),
                             type=entry.get("type", "output"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.content)
        return target

    # ------------------------------------------------------- generation ---
    def still(self, prompt: str, out: Path, *, width: int, height: int,
              seed: int, negative: str = "", query: str = "",
              progress=None) -> StillResult:
        if not self.image_workflow:
            raise VisualsError("no image workflow configured on this profile")
        nodes = wf.load(_repo_path(self.image_workflow))
        patched = wf.patch_image(
            nodes, self.image_nodes, prompt=prompt, negative=negative or None,
            width=width, height=height, seed=seed,
            lightning=None if self.lightning is None else bool(self.lightning),
            prefix=f"reelforge/{out.stem}",
        )
        prompt_id = self.submit(patched)
        outputs = self.wait(prompt_id, progress=progress)
        files = self.outputs_of(outputs, "image")
        if not files:
            raise VisualsError("the image workflow produced no image file")
        raw = self.download(files[0], out.with_suffix(".raw" + Path(files[0]["filename"]).suffix))
        from PIL import Image

        with Image.open(raw) as image:
            image = image.convert("RGB")
            image.save(out, "PNG")
            size = image.size
        raw.unlink(missing_ok=True)
        return StillResult(path=out, width=size[0], height=size[1], seed=seed, prompt=prompt,
                           meta={"prompt_id": prompt_id, "file": files[0]["filename"]})

    def clip(self, prompt: str, out_dir: Path, *, seconds: float, fps: int,
             width: int, height: int, seed: int, negative: str = "", query: str = "",
             progress=None) -> ClipResult:
        if not self.video_workflow:
            raise VisualsError("no video workflow configured on this profile")
        nodes = wf.load(_repo_path(self.video_workflow))
        native_fps = wf.workflow_fps(nodes, self.video_nodes) or 24
        patched = wf.patch_video(
            nodes, self.video_nodes, prompt=prompt, negative=negative or None,
            seconds=seconds, fps=native_fps, seed=seed,
            enhancer=None if self.prompt_enhancer is None else bool(self.prompt_enhancer),
            prefix=f"reelforge/{out_dir.name}",
        )
        prompt_id = self.submit(patched)
        outputs = self.wait(prompt_id, progress=progress)
        files = self.outputs_of(outputs, "video")
        if not files:
            raise VisualsError("the video workflow produced no video file")
        out_dir.mkdir(parents=True, exist_ok=True)
        source = self.download(files[0], out_dir.parent / f"{out_dir.name}{Path(files[0]['filename']).suffix}")
        count = extract_frames(source, out_dir, fps=fps, width=width, height=height)
        return ClipResult(frames_dir=out_dir, fps=fps, frames=count, seconds=count / fps,
                          seed=seed, prompt=prompt, source=source,
                          meta={"prompt_id": prompt_id, "file": files[0]["filename"],
                                "native_fps": native_fps})

    def audio(self, prompt: str, out: Path, *, seconds: float, seed: int,
              category: str = "Music", negative: str = "", progress=None) -> AudioResult:
        if not self.audio_workflow:
            raise VisualsError("no audio workflow configured on this profile")
        nodes = wf.load(_repo_path(self.audio_workflow))
        patched = wf.patch_audio(
            nodes, self.audio_nodes, prompt=prompt, negative=negative or None,
            seconds=seconds, seed=seed, category=category,
            enhancer=None if self.audio_enhancer is None else bool(self.audio_enhancer),
            prefix=f"reelforge/{out.stem}",
        )
        prompt_id = self.submit(patched)
        outputs = self.wait(prompt_id, progress=progress)
        files = self.outputs_of(outputs, "audio")
        if not files:
            raise VisualsError("the audio workflow produced no audio file")
        source = self.download(files[0], out.with_suffix(Path(files[0]["filename"]).suffix))
        length = to_wav48(source, out)
        return AudioResult(path=out, seconds=length, seed=seed, prompt=prompt, source=source,
                           meta={"prompt_id": prompt_id, "file": files[0]["filename"],
                                 "category": category})

    def release(self) -> bool:
        """Ask the server to unload its models and return the VRAM.

        Best effort: an older server without /free simply keeps them, which
        is only the status quo. The next generation pays the model-load time
        again -- seconds, against the out-of-memory it prevents in whatever
        shares the card (the TTS crashed at 159 MB free with LTX resident).
        """
        try:
            response = self._client.post("/free", json={"unload_models": True,
                                                        "free_memory": True},
                                         timeout=PROBE_TIMEOUT)
            return response.status_code < 400
        except Exception:
            return False

    # ----------------------------------------------------------- health ---
    def health(self) -> dict[str, Any]:
        out: dict[str, Any] = {"provider": "comfyui", "base_url": self.base_url,
                               "image_workflow": self.image_workflow,
                               "video_workflow": self.video_workflow}
        try:
            stats = self._client.get("/system_stats", timeout=PROBE_TIMEOUT)
            stats.raise_for_status()
            body = stats.json()
        except Exception as exc:
            out |= {"reachable": False, "error": f"{self.base_url} unreachable: {str(exc)[:160]}"}
            return out
        out["reachable"] = True
        system = body.get("system") or {}
        out["comfyui_version"] = system.get("comfyui_version") or system.get("version")
        devices = []
        for dev in body.get("devices") or []:
            total = dev.get("vram_total") or 0
            devices.append({"name": dev.get("name"), "vram_gb": round(total / 2**30, 1) if total else None})
        out["devices"] = devices
        try:
            queue = self._client.get("/queue", timeout=PROBE_TIMEOUT).json()
            out["queue"] = {"running": len(queue.get("queue_running") or []),
                            "pending": len(queue.get("queue_pending") or [])}
        except Exception:
            pass

        problems = []
        out["audio_workflow"] = self.audio_workflow
        for kind, path, node_map in (("image", self.image_workflow, self.image_nodes),
                                     ("video", self.video_workflow, self.video_nodes),
                                     ("audio", self.audio_workflow, self.audio_nodes)):
            if not path:
                continue
            try:
                nodes = wf.load(_repo_path(path))
                found = wf.detect(nodes, kind)
                found.update({k: v for k, v in node_map.items() if v})
                out[f"{kind}_nodes"] = wf.describe(nodes, found)
                if "prompt" not in found:
                    problems.append(f"{kind} workflow: no prompt node found")
                missing = self._missing_models(nodes)
                if missing:
                    problems.append(f"{kind} workflow needs models the server does not list: "
                                    + ", ".join(missing[:4]))
            except wf.WorkflowError as exc:
                problems.append(str(exc))
        if problems:
            out["error"] = "; ".join(problems)
        return out

    def _missing_models(self, nodes: dict[str, Any]) -> list[str]:
        """Checkpoint names the server's loaders do not offer. Best effort:
        a server without /object_info simply reports nothing missing."""
        loaders = {k: n for k, n in nodes.items()
                   if str(n.get("class_type", "")).endswith("Loader")}
        if not loaders:
            return []
        missing = []
        for key, node in loaders.items():
            cls = node["class_type"]
            try:
                response = self._client.get(f"/object_info/{cls}", timeout=PROBE_TIMEOUT)
                if response.status_code != 200:
                    continue    # a custom node this server cannot describe
                info = response.json()
            except Exception:
                continue
            spec = (info.get(cls) or {}).get("input", {}).get("required", {})
            for name, value in (node.get("inputs") or {}).items():
                if not isinstance(value, str) or name not in spec:
                    continue
                options = spec[name][0] if spec[name] else None
                if isinstance(options, list) and value not in options:
                    missing.append(value)
        return missing


def _status_message(status: dict[str, Any]) -> str:
    for message in status.get("messages") or []:
        if isinstance(message, list) and len(message) == 2 and message[0] == "execution_error":
            detail = message[1] or {}
            return f"{detail.get('node_type', '?')}: {detail.get('exception_message', '')}"[:300]
    return status.get("status_str", "error")
