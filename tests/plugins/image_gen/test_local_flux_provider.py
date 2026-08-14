"""Tests for the bundled local_flux image_gen plugin.

The HTTP path is the same OpenAI-compatible shape ``deepinfra`` already
covers, so these tests pin the part that is new and dangerous: the
**exclusive GPU handoff**. Getting the handoff wrong does not produce a bad
image — it takes the host's chat model down for every consumer — so each
safety rule from the module docstring gets a test that fails when the rule
is removed.
"""

from __future__ import annotations

import base64
import threading
import time

import pytest

import plugins.image_gen.local_flux as local_flux


# 1×1 transparent PNG — valid bytes for save_b64_image()
_PNG_HEX = (
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6300010000000500010d0a2db40000000049454e44"
    "ae426082"
)


def _b64_png() -> str:
    return base64.b64encode(bytes.fromhex(_PNG_HEX)).decode()


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    """Keep saved images and the GPU lock inside tmp_path."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in (
        "LOCAL_FLUX_ENDPOINT",
        "LOCAL_FLUX_MODEL",
        "LOCAL_FLUX_QUALITY",
        "LOCAL_FLUX_READY_URL",
        "LOCAL_FLUX_LOCK_PATH",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOCAL_FLUX_LOCK_PATH", str(tmp_path / "gpu.lock"))
    yield


def _config(tmp_path, **overrides):
    """A config with the handoff wired to recorder commands."""
    cfg = {
        "endpoint": "http://gpu-host:8100/v1",
        "quality": "high",
        "handoff": {
            "acquire": "/bin/true graphics",
            "release": "/bin/true llm",
            "ready_url": "http://gpu-host:8100/health",
            "lock_path": str(tmp_path / "gpu.lock"),
        },
    }
    cfg.update(overrides)
    return cfg


class _Recorder:
    """Stands in for ``_run``, logging calls and returning scripted codes."""

    def __init__(self, *, acquire=(0, ""), release=(0, "")):
        self.calls = []
        self._acquire = acquire
        self._release = release

    def __call__(self, cmd, timeout):
        argv = list(cmd)
        self.calls.append(argv)
        return self._release if "llm" in argv else self._acquire

    @property
    def kinds(self):
        return ["release" if "llm" in c else "acquire" for c in self.calls]


def _install(monkeypatch, tmp_path, *, recorder, ready=False, post=None, config=None):
    monkeypatch.setattr(local_flux, "_load_config", lambda: config or _config(tmp_path))
    monkeypatch.setattr(local_flux, "_run", recorder)
    monkeypatch.setattr(local_flux, "_is_ready", lambda url, timeout: ready)
    monkeypatch.setattr(local_flux, "RELEASE_RETRY_DELAY", 0.0)
    if post is not None:
        import requests

        monkeypatch.setattr(requests, "post", post)


def _ok_post(*_a, **_kw):
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"b64_json": _b64_png()}]}

    return _Resp()


def _boom_post(*_a, **_kw):
    raise RuntimeError("connection reset mid-generation")


# ---------------------------------------------------------------------------
# Rule 1 — release always runs
# ---------------------------------------------------------------------------


def test_release_runs_even_when_generation_fails(monkeypatch, tmp_path):
    """A failed generation must still hand the GPU back.

    This is the invariant that matters most: without it, one network blip
    leaves the chat model unloaded for every consumer on the host.
    """
    rec = _Recorder()
    _install(monkeypatch, tmp_path, recorder=rec, post=_boom_post)

    result = local_flux.LocalFluxImageGenProvider().generate("a cat")

    assert result["success"] is False
    assert result["error_type"] == "api_error"
    assert rec.kinds == ["acquire", "release"], "release must run after a failed generation"


def test_release_runs_after_a_successful_generation(monkeypatch, tmp_path):
    rec = _Recorder()
    _install(monkeypatch, tmp_path, recorder=rec, post=_ok_post)

    result = local_flux.LocalFluxImageGenProvider().generate("a cat")

    assert result["success"] is True
    assert rec.kinds == ["acquire", "release"]
    assert result["provider"] == "local_flux"
    assert result["quality"] == "high"
    assert result["size"] == local_flux.DEFAULT_SIZES["landscape"]
    assert "warning" not in result


# ---------------------------------------------------------------------------
# Rule 2 — only release what we acquired
# ---------------------------------------------------------------------------


def test_already_serving_skips_both_commands(monkeypatch, tmp_path):
    """If the endpoint is already up we displaced nothing, so leave it be."""
    rec = _Recorder()
    _install(monkeypatch, tmp_path, recorder=rec, ready=True, post=_ok_post)

    result = local_flux.LocalFluxImageGenProvider().generate("a cat")

    assert result["success"] is True
    assert rec.calls == [], "must not acquire or release a GPU that was already serving"


# ---------------------------------------------------------------------------
# Rule 3 — a refusal is surfaced, never retried
# ---------------------------------------------------------------------------


def test_acquire_refusal_is_surfaced_and_not_retried(monkeypatch, tmp_path):
    """A protected window / reservation refusal must stop the call dead."""
    rec = _Recorder(acquire=(3, "REFUSED: protected window 'nightly' is active"))
    _install(monkeypatch, tmp_path, recorder=rec, post=_ok_post)

    result = local_flux.LocalFluxImageGenProvider().generate("a cat")

    assert result["success"] is False
    assert result["error_type"] == "handoff_refused"
    assert "protected window" in result["error"]
    assert rec.kinds == ["acquire"], "a refusal must not be retried, and must not release"


# ---------------------------------------------------------------------------
# Release retry + warning
# ---------------------------------------------------------------------------


def test_release_retried_once_then_warns(monkeypatch, tmp_path):
    """Release is worth one retry; a double failure must reach the caller."""
    rec = _Recorder(release=(1, "ssh: connect to host gpu-host port 22: timed out"))
    _install(monkeypatch, tmp_path, recorder=rec, post=_ok_post)

    result = local_flux.LocalFluxImageGenProvider().generate("a cat")

    assert result["success"] is True, "the image was produced; the warning rides along"
    assert rec.kinds == ["acquire", "release", "release"], "release gets exactly one retry"
    assert "NOT switched back" in result["warning"]


def test_release_retry_that_succeeds_leaves_no_warning(monkeypatch, tmp_path):
    codes = iter([(1, "transient"), (0, "")])

    class _Flaky(_Recorder):
        def __call__(self, cmd, timeout):
            argv = list(cmd)
            self.calls.append(argv)
            return next(codes) if "llm" in argv else (0, "")

    rec = _Flaky()
    _install(monkeypatch, tmp_path, recorder=rec, post=_ok_post)

    result = local_flux.LocalFluxImageGenProvider().generate("a cat")

    assert result["success"] is True
    assert "warning" not in result
    assert rec.kinds == ["acquire", "release", "release"]


# ---------------------------------------------------------------------------
# Rule 4 — one handoff at a time
# ---------------------------------------------------------------------------


def test_lock_serialises_concurrent_handoffs(monkeypatch, tmp_path):
    """Two agents asking at once must not both flip the GPU."""
    overlap = {"peak": 0, "now": 0}
    guard = threading.Lock()

    def _counting_post(*_a, **_kw):
        with guard:
            overlap["now"] += 1
            overlap["peak"] = max(overlap["peak"], overlap["now"])
        try:
            time.sleep(0.15)
            return _ok_post()
        finally:
            with guard:
                overlap["now"] -= 1

    rec = _Recorder()
    _install(monkeypatch, tmp_path, recorder=rec, post=_counting_post)

    provider = local_flux.LocalFluxImageGenProvider()
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(provider.generate("a cat")))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(results) == 2
    assert all(r["success"] for r in results)
    assert overlap["peak"] == 1, "the GPU lock must serialise generations"


def test_gpu_busy_is_reported_when_the_lock_cannot_be_taken(monkeypatch, tmp_path):
    cfg = _config(tmp_path)
    cfg["handoff"]["lock_timeout"] = 0.2
    rec = _Recorder()
    _install(monkeypatch, tmp_path, recorder=rec, post=_ok_post, config=cfg)

    provider = local_flux.LocalFluxImageGenProvider()
    held = local_flux._GpuLock(cfg["handoff"]["lock_path"], 5.0)
    held.__enter__()
    try:
        result = provider.generate("a cat")
    finally:
        held.__exit__()

    assert result["success"] is False
    assert result["error_type"] == "gpu_busy"
    assert rec.calls == [], "never touch the GPU without the lock"


# ---------------------------------------------------------------------------
# Availability + surface
# ---------------------------------------------------------------------------


def test_is_available_requires_explicit_config(monkeypatch):
    """No config section means not available.

    This provider needs no API key, so an unconditional True would let the
    registry's single-available-provider fallback silently route a user's
    images at a server they never configured.
    """
    provider = local_flux.LocalFluxImageGenProvider()

    monkeypatch.setattr(local_flux, "_load_config", dict)
    assert provider.is_available() is False

    monkeypatch.setattr(local_flux, "_load_config", lambda: {"endpoint": "http://x:8100/v1"})
    assert provider.is_available() is True


def test_no_handoff_configured_runs_no_commands(monkeypatch, tmp_path):
    """Pointed at an always-on server, this is a plain HTTP client."""
    rec = _Recorder()
    _install(
        monkeypatch,
        tmp_path,
        recorder=rec,
        post=_ok_post,
        config={"endpoint": "http://always-on:8100/v1"},
    )

    result = local_flux.LocalFluxImageGenProvider().generate("a cat")

    assert result["success"] is True
    assert rec.calls == []


def test_rejects_image_to_image(monkeypatch, tmp_path):
    rec = _Recorder()
    _install(monkeypatch, tmp_path, recorder=rec, post=_ok_post)

    result = local_flux.LocalFluxImageGenProvider().generate(
        "a cat", image_url="/workspace/uploads/cat.png"
    )

    assert result["success"] is False
    assert result["error_type"] == "modality_unsupported"
    assert rec.calls == [], "an unsupported request must not touch the GPU"


def test_blank_prompt_is_rejected_before_the_gpu(monkeypatch, tmp_path):
    rec = _Recorder()
    _install(monkeypatch, tmp_path, recorder=rec, post=_ok_post)

    result = local_flux.LocalFluxImageGenProvider().generate("   ")

    assert result["success"] is False
    assert result["error_type"] == "invalid_argument"
    assert rec.calls == []


def test_aspect_ratio_selects_size(monkeypatch, tmp_path):
    seen = {}

    def _capture(url, json=None, timeout=None):  # noqa: A002
        seen["size"] = json["size"]
        seen["url"] = url
        return _ok_post()

    rec = _Recorder()
    _install(monkeypatch, tmp_path, recorder=rec, ready=True, post=_capture)

    local_flux.LocalFluxImageGenProvider().generate("a cat", aspect_ratio="portrait")

    assert seen["size"] == local_flux.DEFAULT_SIZES["portrait"]
    assert seen["url"] == "http://gpu-host:8100/v1/images/generations"


def test_command_parsing_accepts_string_and_list():
    assert local_flux._command("/bin/gpu-mode graphics") == ["/bin/gpu-mode", "graphics"]
    assert local_flux._command(["/bin/gpu-mode", "llm"]) == ["/bin/gpu-mode", "llm"]
    assert local_flux._command("") is None
    assert local_flux._command(None) is None
