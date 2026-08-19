"""Session-key profile namespacing for room turns (#139).

The peer-DM lane (`hermes peer dm <peer>/<member>`) needs
``gateway.multiplex_profiles`` on, and under multiplex every room turn's
session key must land in the *member's* namespace — the adapter stamps
``source.profile`` before deriving the key, and the session store's
resolver prefers that stamp. With multiplex off, keys must stay
byte-identical to the legacy ``agent:main:`` namespace.

These tests pin that property in both modes so a refactor of either half
(the stamp, or the resolver preference) cannot silently break peer
routing or re-namespace production sessions.
"""

from __future__ import annotations

from types import SimpleNamespace

from gateway.config import PlatformConfig
from gateway.session import SessionStore

from .adapter import RetinueRoomsAdapter


def _adapter_with_store(multiplex: bool) -> RetinueRoomsAdapter:
    adapter = RetinueRoomsAdapter(PlatformConfig())
    store = object.__new__(SessionStore)  # avoid DB setup; resolver reads config only
    store.config = SimpleNamespace(multiplex_profiles=multiplex)
    adapter._session_store = store
    return adapter


def _stamped_source(adapter: RetinueRoomsAdapter, member: str):
    source = adapter.build_source(
        chat_id="room-1",
        chat_name="room:Test",
        chat_type="group",
        user_id="user:Mark",
        user_name="Mark",
    )
    source.profile = None if member == "default" else member
    return source


def test_multiplex_off_keeps_the_legacy_namespace():
    adapter = _adapter_with_store(multiplex=False)
    key = adapter._session_key_for(_stamped_source(adapter, "ellie"))
    assert key.startswith("agent:main:"), key


def test_multiplex_on_uses_the_stamped_member_namespace():
    adapter = _adapter_with_store(multiplex=True)
    key = adapter._session_key_for(_stamped_source(adapter, "ellie"))
    assert key.startswith("agent:ellie:"), key


def test_multiplex_on_default_member_stays_legacy():
    """The default profile is stamped as None — legacy namespace even multiplexed
    (the resolver then falls back to the active profile; pin only that the
    member string 'default' never namespaces as agent:default via the stamp)."""
    adapter = _adapter_with_store(multiplex=True)
    source = _stamped_source(adapter, "default")
    assert source.profile is None
