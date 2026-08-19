"""Per-turn broker credential for governed room retainers.

The host capability broker (operator infra, bead infra-blo5/infra-8537)
executes allowlisted host commands on behalf of room retainers. The socket
cannot tell callers apart — every room container connects as the same uid —
so identity rides a token the GATEWAY mints per turn and injects into each
executed command's environment (:mod:`tools.turn_env`).

Token format (also implemented, independently, by the infra broker —
``infra/scripts/retinue-broker.py``; bump the version prefix on any change):

    v1:<slug>:<expiry_unix>:<hmac_sha256(key, "v1:<slug>:<expiry_unix>")hex>

The key lives OUTSIDE the bind-mount (default ``<HERMES_HOME>/broker.key``),
so nothing a container can read suffices to forge a token. Turns within a
room are serialized, so two members' tokens never coexist in one container.
Expiry bounds replay of a token a model deliberately wrote to disk.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

ENV_KEY_FILE = "RETINUE_BROKER_KEY_FILE"
TOKEN_ENV = "RETINUE_BROKER_TOKEN"
TTL_SECONDS = 6 * 3600  # long turns + background commands, still bounded


def key_path(home_dir: str) -> str:
    override = (os.getenv(ENV_KEY_FILE) or "").strip()
    return override or os.path.join(home_dir, "broker.key")


def _load_key(home_dir: str) -> bytes:
    """Read the shared HMAC key, creating it 0600 on first use."""
    path = key_path(home_dir)
    try:
        with open(path, "rb") as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = os.urandom(32).hex().encode("ascii")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    return key


def mint(home_dir: str, slug: str, now: float | None = None) -> str:
    """Token for *slug*, valid TTL_SECONDS from *now*."""
    slug = (slug or "").strip()
    if not slug:
        raise ValueError("slug is required")
    expiry = int((now if now is not None else time.time()) + TTL_SECONDS)
    payload = f"v1:{slug}:{expiry}"
    digest = hmac.new(_load_key(home_dir), payload.encode("utf-8"), hashlib.sha256)
    return f"{payload}:{digest.hexdigest()}"


def verify(home_dir: str, token: str, now: float | None = None) -> str | None:
    """Slug for a valid, unexpired *token*; ``None`` otherwise.

    The broker has its own verifier; this one exists so the mint side is
    testable against the same rules it promises.
    """
    parts = (token or "").split(":")
    if len(parts) != 4 or parts[0] != "v1":
        return None
    _, slug, expiry_s, mac = parts
    try:
        expiry = int(expiry_s)
    except ValueError:
        return None
    if expiry < (now if now is not None else time.time()):
        return None
    payload = f"v1:{slug}:{expiry}"
    want = hmac.new(_load_key(home_dir), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return slug if hmac.compare_digest(want, mac) else None
