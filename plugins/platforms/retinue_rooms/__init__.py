"""Retinue rooms — shared multi-agent conversations (see retinue/ROOMS.md)."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def check_requirements() -> bool:
    return True  # stdlib only


def _in_secondary_profile_scope() -> bool:
    """True when running inside a secondary profile's runtime scope.

    The rooms adapter binds a port and owns the shared room store, so exactly
    one instance must exist: the default profile's. Under the in-process
    multiplexer, secondary-profile platform calls run inside a profile runtime
    scope (hermes home resolves under .../profiles/<name>).
    """
    from pathlib import Path

    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()).parent.name == "profiles"


def is_connected(_config) -> bool:
    """Gateway enablement gate: opt-in via RETINUE_ROOMS_ENABLED or an API key.

    Without this gate every gateway would bind a rooms port by default.
    Secondary profile scopes are never "connected" — declining here keeps the
    per-profile skip on the registry's quiet (debug) enablement path instead
    of the validate_config WARNING path.
    """
    from .adapter import rooms_enabled

    return rooms_enabled() and not _in_secondary_profile_scope()


def validate_config(_config) -> bool:
    """Return True when this adapter should be created (registry treats the
    return as a boolean validity flag).

    Backstop for the is_connected enablement gate: any path that reaches
    adapter creation in a secondary profile scope anyway must still decline,
    or N instances race for the same bind. The registry logs this decline as
    a WARNING — correct here, since this path only fires when enablement was
    bypassed.
    """
    if _in_secondary_profile_scope():
        logger.debug(
            "Retinue rooms: declining adapter for secondary profile scope"
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
                "other AI members. Reply only as yourself — your persona name, "
                "never the name of the model or coding tool you run on. Mention "
                "@name to hand a task to another agent member."
            ),
        )
    except Exception:
        logger.warning("Retinue rooms: failed to register platform adapter", exc_info=True)
