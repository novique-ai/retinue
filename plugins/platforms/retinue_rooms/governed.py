"""Governed retainers — the binding operating contract for room turns.

Some retainers are *governed agents* of the operator's ecosystem: they carry
an operating contract (rules of engagement — stop on missing capability,
never improvise around a refused tool, escalation paths) into **every turn**
they take in an ``ide`` room. The contract is a host file the operator's
infra repo owns; this module only loads it.

Design properties, in order of importance:

1. **Guaranteed delivery.** The contract rides the per-turn room briefing
   (``engine.room_briefing``), which every member turn receives — unlike a
   skill, which is load-on-trigger and can simply never be loaded.
2. **Fail closed.** A governed member whose contract cannot be read does not
   take the turn. Running ungoverned because a file went missing is the
   failure mode this whole feature exists to prevent — the turn is refused
   with a reason the transcript shows.
3. **One source of truth.** ``RETINUE_GOVERNED_CONTRACT`` names the file;
   the gateway never embeds contract text. Editing the file (git-owned,
   host-side) changes the next turn — no restart, thanks to the mtime cache.

Sandbox rooms are out of scope: no IDE tree is mounted there, and the
contract governs work on the operator's real tree.
"""

from __future__ import annotations

import os
import threading
from typing import Optional, Tuple

ENV_VAR = "RETINUE_GOVERNED_CONTRACT"

# Briefings ride the system prompt; an unbounded file here would dwarf the
# turn. The contract is designed to be ~60 lines — refuse silently-truncated
# rules the same way we refuse a missing file: loudly.
MAX_CONTRACT_BYTES = 32_768

_lock = threading.Lock()
_cache: dict = {"path": None, "mtime": None, "text": None}


def contract_text() -> Tuple[Optional[str], str]:
    """Return ``(text, "")`` or ``(None, reason)``. Never raises.

    mtime-cached: the file is re-read only when it changes, so per-turn cost
    is one ``stat``.
    """
    path = (os.getenv(ENV_VAR) or "").strip()
    if not path:
        return None, f"{ENV_VAR} is not set on the gateway"
    try:
        st = os.stat(path)
    except OSError as e:
        return None, f"contract file unreadable: {e}"
    if st.st_size > MAX_CONTRACT_BYTES:
        return None, (
            f"contract file is {st.st_size} bytes (cap {MAX_CONTRACT_BYTES}) — "
            "a truncated contract is not a contract"
        )
    with _lock:
        if _cache["path"] == path and _cache["mtime"] == st.st_mtime:
            cached = _cache["text"]
            return (cached, "") if cached else (None, "contract file is empty")
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read().strip()
        except OSError as e:
            return None, f"contract file unreadable: {e}"
        _cache.update(path=path, mtime=st.st_mtime, text=text or None)
    if not text:
        return None, "contract file is empty"
    return text, ""
