"""Retinue rooms — shared multi-agent conversations (see retinue/ROOMS.md)."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def check_requirements() -> bool:
    return True  # stdlib only


def is_connected(_config) -> bool:
    """Gateway enablement gate: opt-in via RETINUE_ROOMS_ENABLED or an API key.

    Without this gate every gateway would bind a rooms port by default.
    """
    from .adapter import rooms_enabled

    return rooms_enabled()


def validate_config(_config) -> bool:
    """Return True when this adapter should be created (registry treats the
    return as a boolean validity flag).

    The rooms adapter binds a port and owns the shared room store, so exactly
    one instance must exist: the default profile's. Under the in-process
    multiplexer, secondary-profile adapter creation runs inside a profile
    runtime scope (hermes-home override set) — decline there instead of
    letting N instances race for the same bind.
    """
    from pathlib import Path

    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    if home.parent.name == "profiles":
        logger.debug(
            "Retinue rooms: declining adapter for secondary profile scope (%s)", home
        )
        return False
    return True


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    try:
        from .adapter import RetinueRoomsAdapter

        ctx.register_platform(
            name="retinue_rooms",
            label="Retinue Rooms",
            adapter_factory=lambda cfg: RetinueRoomsAdapter(cfg),
            check_fn=check_requirements,
            validate_config=validate_config,
            is_connected=is_connected,
            required_env=[],
            install_hint="No extra packages needed (stdlib only)",
            emoji="\U0001f3db",  # classical building — the forum
            allow_update_command=False,
            platform_hint=(
                "You are speaking inside a Retinue room — a shared conversation "
                "between the user and several named agents. Messages are prefixed "
                "[speaker] so you can tell participants apart; '(agent)' marks "
                "other AI members. Reply only as yourself. Mention @name to hand "
                "a task to another agent member."
            ),
        )
    except Exception:
        logger.warning("Retinue rooms: failed to register platform adapter", exc_info=True)
