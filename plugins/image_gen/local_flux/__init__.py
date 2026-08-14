"""Self-hosted image generation backend, with an exclusive GPU handoff.

Talks to any OpenAI-compatible ``/v1/images/generations`` endpoint you run
yourself — a diffusers server, LocalAI, vLLM, a FLUX or SDXL box. The request
path is the same one :mod:`plugins.image_gen.deepinfra` uses; what is new here
is the **handoff**.

The handoff
-----------
One accelerator usually cannot hold a chat model and a diffusion model at the
same time. Generating an image on your own hardware therefore means unloading
the chat model, loading the diffusion model, generating, and putting the chat
model back. That sequence — not the HTTP call — is the hard part, and it is
what this provider manages.

You supply the commands; this plugin only decides *when* to run them::

    image_gen:
      provider: local_flux
      local_flux:
        endpoint: http://gpu-host:8100/v1
        quality: high
        handoff:
          acquire: /path/to/gpu-mode graphics
          release: /path/to/gpu-mode llm
          ready_url: http://gpu-host:8100/health

Omit ``handoff`` entirely and this is a plain client for an always-on image
server.

Four rules make it safe to hand to an agent that is *itself* running on the
chat model it has to displace (which works — no inference happens while a tool
call is in flight):

1. **Release always runs.** It is in a ``finally``, so a failed generation, a
   timeout, or an exception still puts the chat model back. A stranded GPU is
   worse than a missing image, so release is also retried once.
2. **Only release what we acquired.** If the endpoint is already serving when
   the call starts, we generate and leave it running — we displaced nothing.
3. **One handoff at a time.** A host-level advisory lock (keyed by endpoint)
   serialises calls so two agents cannot fight over the GPU.
4. **A refusal is never retried.** A non-zero ``acquire`` — a maintenance
   window, a reservation, a busy queue — is surfaced verbatim.

Readiness is probed at ``ready_url`` rather than parsed out of command output,
so this never depends on the wording of your script.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    save_url_image,
    success_response,
)

logger = logging.getLogger(__name__)


PROVIDER_NAME = "local_flux"

DEFAULT_ENDPOINT = "http://127.0.0.1:8100/v1"

# Standard OpenAI size strings, matching the openai/deepinfra plugins so
# aspect_ratio means the same thing across every image_gen backend.
DEFAULT_SIZES: Dict[str, str] = {
    "landscape": "1536x1024",
    "square": "1024x1024",
    "portrait": "1024x1536",
}

# Local diffusion is slow enough that these are minutes, not seconds. A
# handoff on a cold model load is the long pole, not the sampling.
DEFAULT_GENERATE_TIMEOUT = 900.0
# Acquire has to cover a cold model load, which for a ~32GB diffusion model
# read into VRAM is ~18 minutes measured, not seconds. Sizing this off
# generation time is the mistake that makes a working handoff look broken.
DEFAULT_ACQUIRE_TIMEOUT = 1800.0
DEFAULT_RELEASE_TIMEOUT = 600.0
DEFAULT_LOCK_TIMEOUT = 1800.0
DEFAULT_READY_TIMEOUT = 10.0

# How long to wait before the single release retry. Long enough for a
# transient systemd/ssh hiccup to clear, short enough not to strand the GPU.
RELEASE_RETRY_DELAY = 5.0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _load_config() -> Dict[str, Any]:
    """Read ``image_gen.local_flux`` from config.yaml (``{}`` on any failure)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        mine = section.get(PROVIDER_NAME) if isinstance(section, dict) else None
        return mine if isinstance(mine, dict) else {}
    except Exception as exc:  # noqa: BLE001 — config must never break resolution
        logger.debug("Could not load image_gen.%s config: %s", PROVIDER_NAME, exc)
        return {}


def _str_setting(cfg: Dict[str, Any], key: str, env: str, default: str) -> str:
    """Resolve a string setting: env var wins, then config, then default."""
    raw = os.environ.get(env, "").strip()
    if raw:
        return raw
    value = cfg.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _float_setting(cfg: Dict[str, Any], key: str, default: float) -> float:
    """Resolve a positive float setting, falling back on anything unusable."""
    value = cfg.get(key)
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _endpoint(cfg: Dict[str, Any]) -> str:
    """Base URL ending in ``/v1``, no trailing slash."""
    return _str_setting(cfg, "endpoint", "LOCAL_FLUX_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")


def _sizes(cfg: Dict[str, Any]) -> Dict[str, str]:
    """Aspect-ratio to size map, with per-key overrides from config."""
    sizes = dict(DEFAULT_SIZES)
    override = cfg.get("sizes")
    if isinstance(override, dict):
        for key, value in override.items():
            if key in sizes and isinstance(value, str) and value.strip():
                sizes[key] = value.strip()
    return sizes


def _command(value: Any) -> Optional[List[str]]:
    """Normalise a command from a string or an argv list.

    Strings are split with :func:`shlex.split` — no shell is involved, so
    pipes and redirects are not honoured. Wrap those in a script instead.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parts = shlex.split(text)
        except ValueError as exc:
            logger.warning("image_gen.%s: unparseable command %r (%s)", PROVIDER_NAME, text, exc)
            return None
        return parts or None
    if isinstance(value, (list, tuple)):
        parts = [str(item) for item in value if str(item).strip()]
        return parts or None
    return None


class _Handoff:
    """Resolved handoff settings. ``enabled`` is False when unconfigured."""

    def __init__(self, cfg: Dict[str, Any], endpoint: str) -> None:
        raw = cfg.get("handoff")
        raw = raw if isinstance(raw, dict) else {}
        self.acquire = _command(raw.get("acquire"))
        self.release = _command(raw.get("release"))
        self.ready_url = _str_setting(raw, "ready_url", "LOCAL_FLUX_READY_URL", "")
        self.acquire_timeout = _float_setting(raw, "acquire_timeout", DEFAULT_ACQUIRE_TIMEOUT)
        self.release_timeout = _float_setting(raw, "release_timeout", DEFAULT_RELEASE_TIMEOUT)
        self.lock_timeout = _float_setting(raw, "lock_timeout", DEFAULT_LOCK_TIMEOUT)
        self.ready_timeout = _float_setting(raw, "ready_timeout", DEFAULT_READY_TIMEOUT)
        self.lock_path = _str_setting(raw, "lock_path", "LOCAL_FLUX_LOCK_PATH", _default_lock_path(endpoint))

    @property
    def enabled(self) -> bool:
        """True once an ``acquire`` command exists — the rest is optional."""
        return bool(self.acquire)


def _default_lock_path(endpoint: str) -> str:
    """A per-endpoint lock file in the system temp dir.

    Keyed by endpoint so two independent GPU hosts do not serialise against
    each other, and placed outside ``$HERMES_HOME`` because the GPU is a host
    resource shared by every profile, not per-agent state.
    """
    digest = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:12]
    return str(Path(tempfile.gettempdir()) / f"hermes-image-gen-{digest}.lock")


# ---------------------------------------------------------------------------
# Handoff mechanics
# ---------------------------------------------------------------------------


def _run(cmd: Sequence[str], timeout: float) -> Tuple[int, str]:
    """Run a command without a shell. Returns ``(returncode, output tail)``.

    Timeouts and missing executables become non-zero results with an
    explanatory message rather than exceptions, so every caller has one
    failure shape to handle.
    """
    try:
        proc = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    except (OSError, ValueError) as exc:
        return 127, str(exc)
    output = f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()
    # Command output can be long; the tail carries the refusal reason.
    if len(output) > 2000:
        output = "…" + output[-2000:]
    return proc.returncode, output


def _is_ready(url: str, timeout: float) -> bool:
    """True when the image endpoint answers — i.e. the GPU already serves it.

    Any failure is a False, never an exception: "cannot tell" and "not ready"
    lead to the same action (acquire), and being wrong costs one no-op
    acquire rather than a crash.
    """
    if not url:
        return False
    try:
        import requests

        response = requests.get(url, timeout=timeout)
        return response.status_code == 200
    except Exception as exc:  # noqa: BLE001
        logger.debug("image_gen.%s: readiness probe %s failed: %s", PROVIDER_NAME, url, exc)
        return False


class _GpuLock:
    """Advisory host lock serialising GPU handoffs. A no-op when disabled.

    Uses ``flock``, so the lock dies with the process — a crashed agent
    cannot wedge the GPU for everyone else.
    """

    def __init__(self, path: str, timeout: float, *, enabled: bool = True) -> None:
        self._path = path
        self._timeout = timeout
        self._enabled = enabled
        self._handle = None

    def __enter__(self) -> "_GpuLock":
        if not self._enabled:
            return self
        import fcntl

        self._handle = open(self._path, "a+")  # noqa: SIM115 — released in __exit__
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise TimeoutError(
                        f"another image generation held the GPU for more than "
                        f"{self._timeout:.0f}s"
                    )
                time.sleep(1.0)

    def __exit__(self, *_exc: Any) -> None:
        if self._handle is None:
            return
        import fcntl

        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def _release(handoff: _Handoff) -> Optional[str]:
    """Put the chat model back. Returns an error string, or None on success.

    Retried once: failing to restore takes the local model down for every
    consumer on the host, which is a worse outcome than the image failing.
    """
    if not handoff.release:
        return None
    code, output = _run(handoff.release, handoff.release_timeout)
    if code == 0:
        return None
    logger.error(
        "image_gen.%s: release failed (exit %s) — retrying once: %s",
        PROVIDER_NAME,
        code,
        output,
    )
    time.sleep(RELEASE_RETRY_DELAY)
    code, output = _run(handoff.release, handoff.release_timeout)
    if code == 0:
        logger.info("image_gen.%s: release succeeded on retry", PROVIDER_NAME)
        return None
    logger.error(
        "image_gen.%s: release FAILED twice (exit %s). The GPU may still be "
        "serving images and the local chat model may be down: %s",
        PROVIDER_NAME,
        code,
        output,
    )
    return f"exit {code}: {output}" if output else f"exit {code}"


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class LocalFluxImageGenProvider(ImageGenProvider):
    """Image generation on hardware you own, with an optional GPU handoff."""

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def display_name(self) -> str:
        return "Local (self-hosted)"

    def is_available(self) -> bool:
        """Available only once ``image_gen.local_flux`` exists in config.

        Deliberately strict: this provider needs no API key, so an
        always-True answer would let the registry's single-available-provider
        fallback route someone's images at a server they never configured.
        Opting in is the credential here.
        """
        return bool(_load_config())

    def list_models(self) -> List[Dict[str, Any]]:
        cfg = _load_config()
        model = _str_setting(cfg, "model", "LOCAL_FLUX_MODEL", "")
        endpoint = _endpoint(cfg)
        return [
            {
                "id": model or "server-default",
                "display": model or "Server default",
                "strengths": f"Self-hosted at {endpoint}",
                "price": "free (your hardware)",
            }
        ]

    def default_model(self) -> Optional[str]:
        cfg = _load_config()
        return _str_setting(cfg, "model", "LOCAL_FLUX_MODEL", "") or None

    def capabilities(self) -> Dict[str, Any]:
        """Text-to-image only.

        OpenAI-compatible ``/v1/images/generations`` has no portable
        image-to-image shape across self-hosted servers, so editing is
        declined explicitly rather than half-supported.
        """
        return {"modalities": ["text"], "max_reference_images": 0}

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Local (self-hosted)",
            "badge": "free",
            "tag": "Your own FLUX/SDXL server, with optional GPU handoff",
            "env_vars": [],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)

        if kwargs.get("image_url") or kwargs.get("reference_image_urls"):
            return error_response(
                error=(
                    "This backend is text-to-image only; image_url and "
                    "reference_image_urls are not supported."
                ),
                error_type="modality_unsupported",
                provider=PROVIDER_NAME,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider=PROVIDER_NAME,
                aspect_ratio=aspect,
            )

        cfg = _load_config()
        endpoint = _endpoint(cfg)
        model = _str_setting(cfg, "model", "LOCAL_FLUX_MODEL", "")
        quality = _str_setting(cfg, "quality", "LOCAL_FLUX_QUALITY", "high")
        size = _sizes(cfg).get(aspect, DEFAULT_SIZES["square"])
        generate_timeout = _float_setting(cfg, "timeout", DEFAULT_GENERATE_TIMEOUT)
        handoff = _Handoff(cfg, endpoint)

        try:
            with _GpuLock(handoff.lock_path, handoff.lock_timeout, enabled=handoff.enabled):
                return self._generate_locked(
                    prompt=prompt,
                    aspect=aspect,
                    endpoint=endpoint,
                    model=model,
                    quality=quality,
                    size=size,
                    generate_timeout=generate_timeout,
                    handoff=handoff,
                )
        except TimeoutError as exc:
            return error_response(
                error=f"Timed out waiting for the GPU: {exc}",
                error_type="gpu_busy",
                provider=PROVIDER_NAME,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except OSError as exc:
            return error_response(
                error=f"Could not open the GPU lock at {handoff.lock_path}: {exc}",
                error_type="lock_error",
                provider=PROVIDER_NAME,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

    def _generate_locked(
        self,
        *,
        prompt: str,
        aspect: str,
        endpoint: str,
        model: str,
        quality: str,
        size: str,
        generate_timeout: float,
        handoff: _Handoff,
    ) -> Dict[str, Any]:
        """Acquire the GPU if needed, generate, and always give it back."""
        acquired = False

        if handoff.enabled and not _is_ready(handoff.ready_url, handoff.ready_timeout):
            logger.info("image_gen.%s: acquiring the GPU for image generation", PROVIDER_NAME)
            code, output = _run(handoff.acquire or [], handoff.acquire_timeout)
            if code != 0:
                # A refusal is a decision made upstream — a maintenance
                # window, a reservation, a busy queue. Surface it, never
                # retry into it. Nothing was acquired, so nothing to release.
                logger.warning(
                    "image_gen.%s: acquire refused (exit %s): %s", PROVIDER_NAME, code, output
                )
                return error_response(
                    error=(
                        f"The GPU could not be switched to image generation "
                        f"(exit {code}). {output}".strip()
                    ),
                    error_type="handoff_refused",
                    provider=PROVIDER_NAME,
                    model=model,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
            acquired = True

        result: Optional[Dict[str, Any]] = None
        try:
            result = self._post(
                prompt=prompt,
                aspect=aspect,
                endpoint=endpoint,
                model=model,
                quality=quality,
                size=size,
                timeout=generate_timeout,
            )
        finally:
            if acquired:
                release_error = _release(handoff)
                if release_error and isinstance(result, dict):
                    # Surfaced on the response too, not only in the log: the
                    # local chat model may now be down for every consumer.
                    result["warning"] = (
                        f"The GPU was NOT switched back to the chat model "
                        f"({release_error}). Local model calls may fail until "
                        f"this is resolved."
                    )

        return result

    def _post(
        self,
        *,
        prompt: str,
        aspect: str,
        endpoint: str,
        model: str,
        quality: str,
        size: str,
        timeout: float,
    ) -> Dict[str, Any]:
        """POST to ``/images/generations`` and materialise the result."""
        try:
            import requests
        except ImportError:
            return error_response(
                error="requests package not installed (pip install requests)",
                error_type="missing_dependency",
                provider=PROVIDER_NAME,
                aspect_ratio=aspect,
            )

        url = f"{endpoint}/images/generations"
        payload: Dict[str, Any] = {
            "prompt": prompt,
            "n": 1,
            "size": size,
            "quality": quality,
            "response_format": "b64_json",
        }
        if model:
            payload["model"] = model

        logger.info(
            "image_gen.%s: generating %s at %s quality via %s", PROVIDER_NAME, size, quality, url
        )
        try:
            # Deliberately no retry layer: a retried request on a busy
            # single-GPU server queues behind the first one and doubles an
            # already multi-minute call.
            response = requests.post(url, json=payload, timeout=(10.0, timeout))
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 — one shape for every transport error
            logger.debug("image_gen.%s: request failed", PROVIDER_NAME, exc_info=True)
            return error_response(
                error=f"Local image generation failed: {exc}",
                error_type="api_error",
                provider=PROVIDER_NAME,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        if isinstance(body, dict) and body.get("error"):
            return error_response(
                error=f"Local image server reported: {body['error']}",
                error_type="api_error",
                provider=PROVIDER_NAME,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or not data:
            return error_response(
                error="Local image server returned no image data",
                error_type="empty_response",
                provider=PROVIDER_NAME,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        first = data[0] if isinstance(data[0], dict) else {}
        b64 = first.get("b64_json")
        remote_url = first.get("url")
        prefix = f"local_{(model or 'image').replace('/', '_').replace(':', '_')}"

        if b64:
            try:
                saved = save_b64_image(b64, prefix=prefix)
            except Exception as exc:  # noqa: BLE001
                return error_response(
                    error=f"Could not save the generated image: {exc}",
                    error_type="io_error",
                    provider=PROVIDER_NAME,
                    model=model,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
            image_ref = str(saved)
        elif remote_url:
            try:
                image_ref = str(save_url_image(remote_url, prefix=prefix))
            except Exception as exc:  # noqa: BLE001
                logger.debug("image_gen.%s: caching URL failed (%s)", PROVIDER_NAME, exc)
                image_ref = remote_url
        else:
            return error_response(
                error="Local image server returned neither b64_json nor url",
                error_type="empty_response",
                provider=PROVIDER_NAME,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        return success_response(
            image=image_ref,
            model=model or "server-default",
            prompt=prompt,
            aspect_ratio=aspect,
            provider=PROVIDER_NAME,
            extra={"size": size, "quality": quality, "endpoint": endpoint},
        )


def register(ctx) -> None:
    """Plugin entry point — wire ``LocalFluxImageGenProvider`` into the registry."""
    ctx.register_image_gen_provider(LocalFluxImageGenProvider())
