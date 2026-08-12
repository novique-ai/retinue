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


def validate_config(_config) -> list:
    return []


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
