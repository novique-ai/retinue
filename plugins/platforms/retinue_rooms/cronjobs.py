"""Served-profile access to Hermes cron jobs for the rooms plugin."""

from __future__ import annotations

import contextlib
import logging
import os
import re
from collections.abc import Mapping
from typing import NamedTuple

logger = logging.getLogger(__name__)

DEFAULT_OWNER = "default"
ROOMS_PLATFORM = "retinue_rooms"
_OWNER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class UnknownOwner(KeyError):
    """A syntactically valid owner slug that this gateway does not serve."""


class UnknownJob(KeyError):
    """A job id that no served owner's store contains."""


class ServedOwner(NamedTuple):
    owner: str
    home: str


def _gateway_config():
    """Return the live gateway config, falling back to a fresh load."""
    try:
        from gateway.run import _gateway_runner_ref

        runner = _gateway_runner_ref()
        config = getattr(runner, "config", None)
        if config is not None:
            return config
    except Exception:
        pass
    try:
        from gateway.config import load_gateway_config

        return load_gateway_config()
    except Exception:
        return None


def _served_pairs(config) -> list[tuple[str, str]]:
    from hermes_cli.profiles import profiles_to_serve

    return [
        (str(name), str(path))
        for name, path in profiles_to_serve(
            multiplex=bool(getattr(config, "multiplex_profiles", False)),
            profile_allowlist=getattr(config, "multiplex_profile_allowlist", None),
        )
    ]


def _home_slug(home_dir: str) -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        name = str(get_active_profile_name() or "")
    except Exception:
        name = ""
    if name and name != "custom" and _OWNER_RE.fullmatch(name):
        return name
    return DEFAULT_OWNER


def _rooms_root(home_dir: str) -> str:
    home = os.path.realpath(home_dir)
    parent = os.path.dirname(home)
    derived = os.path.dirname(parent) if os.path.basename(parent) == "profiles" else home
    try:
        from hermes_constants import get_default_hermes_root

        root = os.path.realpath(str(get_default_hermes_root()))
    except Exception:
        return derived
    if root in (home, derived):
        return root
    logger.debug(
        "Hermes root %s does not contain rooms home %s; using %s",
        root,
        home,
        derived,
    )
    return derived


def served(home_dir: str) -> list[ServedOwner]:
    """Return the addressable served set without widening it."""
    config = _gateway_config()
    if config is None:
        return [ServedOwner(_home_slug(home_dir), os.path.realpath(home_dir))]
    try:
        pairs = _served_pairs(config)
    except Exception:
        logger.debug("Could not resolve served profiles", exc_info=True)
        return [ServedOwner(_home_slug(home_dir), os.path.realpath(home_dir))]

    home = os.path.realpath(home_dir)
    root = _rooms_root(home_dir)
    profiles_root = os.path.join(root, "profiles")
    result: list[ServedOwner] = []
    seen: set[str] = set()
    for name, path in pairs:
        resolved = os.path.realpath(path)
        addressable = (
            resolved == home
            or resolved == root
            or (
                os.path.dirname(resolved) == profiles_root
                and os.path.basename(resolved) == name
            )
        )
        exists = os.path.isdir(resolved) or resolved == home
        if not addressable or not exists:
            logger.debug(
                "Served profile %s at %s is not addressable under rooms root %s",
                name,
                resolved,
                root,
            )
            continue
        if name in seen:
            continue
        seen.add(name)
        result.append(ServedOwner(name, resolved))

    if pairs and not result:
        logger.warning(
            "Gateway serves %d profile(s) but none map under rooms root %s; "
            "the rooms cron API is empty",
            len(pairs),
            root,
        )
    return result


def served_owners(home_dir: str) -> list[str]:
    return [entry.owner for entry in served(home_dir)]


def _validate_owner(owner: str) -> str:
    value = str(owner or "").strip()
    if (
        not value
        or ".." in value
        or os.path.isabs(value)
        or value != os.path.basename(value)
        or not _OWNER_RE.fullmatch(value)
    ):
        raise ValueError("invalid retainer slug")
    return value


def owner_home(home_dir: str, owner: str) -> str:
    value = _validate_owner(owner)
    for entry in served(home_dir):
        if entry.owner == value:
            return entry.home
    raise UnknownOwner(value)


@contextlib.contextmanager
def _scoped_home(home: str):
    from cron import jobs as cron_jobs
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(home)
    try:
        with cron_jobs.use_cron_store(home):
            yield
    finally:
        reset_hermes_home_override(token)


@contextlib.contextmanager
def scoped(home_dir: str, owner: str):
    """Scope both Hermes home and cron storage to one served owner."""
    with _scoped_home(owner_home(home_dir, owner)):
        yield


def timezone_display() -> str:
    import hermes_time

    try:
        zone = hermes_time.get_timezone()
        if zone is not None:
            return str(zone)
        return str(hermes_time.now().tzinfo or "local")
    except Exception:
        return "local"


def _dict(value) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _room_for(job: Mapping) -> str | None:
    meta = _dict(job.get("retinue"))
    room = meta.get("room")
    if isinstance(room, str) and room:
        return room
    origin = _dict(job.get("origin"))
    if origin.get("platform") == ROOMS_PLATFORM:
        room = origin.get("chat_id")
        if isinstance(room, str) and room:
            return room
    deliver = job.get("deliver")
    if isinstance(deliver, str):
        for token in deliver.split(","):
            platform, separator, chat_id = token.strip().partition(":")
            if separator and platform == ROOMS_PLATFORM and chat_id:
                return chat_id
    return None


def _schedule_input(schedule: dict, display: str) -> str:
    kind = schedule.get("kind")
    if kind == "once":
        return str(schedule.get("run_at") or display)
    if kind == "interval":
        minutes = schedule.get("minutes")
        return f"every {minutes}m" if minutes is not None else display
    if kind == "cron":
        return str(schedule.get("expr") or display)
    return display


def shape(job: Mapping, owner: str, rooms: Mapping[str, str] | None) -> dict:
    """Return the stable JSON row for any mapping input."""
    from cron import jobs as cron_jobs

    record = job if isinstance(job, Mapping) else {}
    schedule = _dict(record.get("schedule"))
    origin = _dict(record.get("origin"))
    meta = _dict(record.get("retinue"))
    skills = record.get("skills")
    skill = skills[0] if isinstance(skills, list) and skills else None
    if skill is None and isinstance(record.get("skill"), str) and record.get("skill"):
        skill = record.get("skill")
    room = _room_for(record)
    display = str(record.get("schedule_display") or schedule.get("display") or "")
    try:
        state = cron_jobs.effective_job_state(dict(record))
    except Exception:
        state = str(record.get("state") or ("scheduled" if record.get("enabled", True) else "paused"))
    room_names = rooms if isinstance(rooms, Mapping) else {}
    return {
        "id": str(record.get("id") or ""),
        "owner": str(owner or ""),
        "name": str(record.get("name") or ""),
        "prompt": str(record.get("prompt") or ""),
        "skill": str(skill) if skill else None,
        "kind": str(meta.get("kind") or "reminder"),
        "routine_slug": str(meta.get("routine_slug")) if meta.get("routine_slug") else None,
        "schedule": schedule,
        "schedule_display": display,
        "schedule_input": _schedule_input(schedule, display),
        "next_run_at": record.get("next_run_at") if isinstance(record.get("next_run_at"), str) else None,
        "last_run_at": record.get("last_run_at") if isinstance(record.get("last_run_at"), str) else None,
        "last_status": record.get("last_status") if isinstance(record.get("last_status"), str) else None,
        "last_error": record.get("last_error") if isinstance(record.get("last_error"), str) else None,
        "last_delivery_error": record.get("last_delivery_error") if isinstance(record.get("last_delivery_error"), str) else None,
        "registration_error": meta.get("registration_error") if isinstance(meta.get("registration_error"), str) else None,
        "state": state,
        "enabled": bool(record.get("enabled", True)),
        "deliver": str(record.get("deliver") or ""),
        "room": room,
        "room_name": str(room_names.get(room) or "") if room else "",
        "repeat": record.get("repeat") if isinstance(record.get("repeat"), Mapping) else {},
        "timezone": timezone_display(),
    }


def list_jobs(
    home_dir: str,
    *,
    owner: str | None = None,
    room: str | None = None,
    rooms: Mapping[str, str] | None = None,
) -> list[dict]:
    from cron import jobs as cron_jobs

    entries = served(home_dir)
    if owner is not None:
        wanted_home = owner_home(home_dir, owner)
        entries = [ServedOwner(_validate_owner(owner), wanted_home)]
    rows: list[dict] = []
    for entry in entries:
        try:
            with _scoped_home(entry.home):
                records = cron_jobs.list_jobs(include_disabled=True)
        except Exception:
            logger.debug("Could not list cron jobs for owner %s", entry.owner, exc_info=True)
            continue
        for record in records:
            row = shape(record, entry.owner, rooms)
            if room is None or row["room"] == room:
                rows.append(row)
    return rows


def get_job(
    home_dir: str, job_id: str, *, rooms: Mapping[str, str] | None = None
) -> tuple[ServedOwner, dict] | None:
    from cron import jobs as cron_jobs

    found: tuple[ServedOwner, dict] | None = None
    for entry in served(home_dir):
        with _scoped_home(entry.home):
            record = cron_jobs.get_job(job_id)
        if record is None:
            continue
        if found is not None:
            logger.debug("Cron job id %s exists in more than one served profile", job_id)
            continue
        found = (entry, record)
    return found


def _sanitize_error(message: object) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(message or ""))
    return " ".join(text.split())[:400]


def _notify_provider(home_dir: str, owner: str) -> bool:
    try:
        with scoped(home_dir, owner):
            from cron.scheduler_provider import InProcessCronScheduler, resolve_cron_scheduler

            provider = resolve_cron_scheduler()
            if not isinstance(provider, InProcessCronScheduler) and len(served(home_dir)) > 1:
                logger.warning(
                    "Skipping cron provider reconcile for owner %s because provider %s "
                    "is not profile-scoped",
                    owner,
                    getattr(provider, "name", provider),
                )
                return False
            provider.on_jobs_changed()
            return True
    except Exception:
        logger.debug("Cron provider reconciliation failed for owner %s", owner, exc_info=True)
        return False


def _clear_registration_error(job_id: str) -> dict | None:
    from cron import jobs as cron_jobs

    record = cron_jobs.get_job(job_id)
    if record is None:
        return None
    meta = _dict(record.get("retinue"))
    if not meta.get("registration_error") and not meta.get("registration_error_at"):
        return record
    meta.update({"registration_error": None, "registration_error_at": None})
    return cron_jobs.update_job(job_id, {"retinue": meta})


def create_job(
    home_dir: str,
    owner: str,
    *,
    name: str,
    schedule: str,
    room: str,
    prompt: str | None = None,
    skill: str | None = None,
    kind: str = "reminder",
    routine_slug: str | None = None,
    room_name: str = "",
    rooms: Mapping[str, str] | None = None,
) -> dict:
    from cron import jobs as cron_jobs
    from cron.scheduler import (
        CronSchedulerRegistrationError,
        create_job_with_scheduler_registration,
    )
    import hermes_time

    owner_home(home_dir, owner)
    room = str(room or "").strip()
    if not room:
        raise ValueError("a scheduled job needs a destination room")
    prompt = str(prompt or "")
    skill = str(skill or "").strip()
    if not prompt.strip() and not skill:
        raise ValueError("a routine needs a skill or a prompt")

    with scoped(home_dir, owner):
        registration_error: str | None = None
        try:
            job = create_job_with_scheduler_registration(
                prompt=prompt,
                schedule=schedule,
                name=str(name or "").strip() or None,
                skills=[skill] if skill else None,
                deliver="origin",
                origin={
                    "platform": ROOMS_PLATFORM,
                    "chat_id": room,
                    "chat_name": f"room:{room_name or room}",
                    "thread_id": owner,
                    "user_id": None,
                },
            )
        except CronSchedulerRegistrationError as exc:
            job = exc.job
            registration_error = _sanitize_error(exc.user_message())

        persisted_id = job["id"]
        meta = {
            "kind": str(kind or "reminder"),
            "room": room,
            "skill": skill or None,
            "routine_slug": routine_slug or None,
            "owner": owner,
            "registration_error": registration_error,
            "registration_error_at": hermes_time.now().isoformat() if registration_error else None,
        }
        try:
            record = cron_jobs.update_job(persisted_id, {"retinue": meta})
            if record is None:
                raise RuntimeError(
                    f"cron job {persisted_id} vanished before its Retinue metadata was stamped"
                )
        except Exception:
            try:
                cron_jobs.remove_job(persisted_id)
            except Exception:
                logger.warning(
                    "Cron job %s was created for owner %s, its Retinue metadata stamp "
                    "failed, and rollback removal also failed",
                    persisted_id,
                    owner,
                    exc_info=True,
                )
            raise

    if registration_error is None:
        _notify_provider(home_dir, owner)
    return shape(record, owner, rooms)


def _resolved_job(home_dir: str, job_id: str) -> tuple[ServedOwner, dict]:
    found = get_job(home_dir, job_id)
    if found is None:
        raise UnknownJob(job_id)
    return found


def _mirror_routine(home_dir: str, job: Mapping, body: Mapping) -> None:
    meta = _dict(job.get("retinue"))
    slug = meta.get("routine_slug")
    if not isinstance(slug, str) or not slug:
        return
    from . import routines

    if routines.get_routine(home_dir, slug) is None:
        return
    updates = {}
    if "name" in body:
        updates["name"] = str(job.get("name") or "")
    if "skill" in body:
        skills = job.get("skills")
        updates["skill"] = str(skills[0]) if isinstance(skills, list) and skills else ""
    if updates:
        routines.update_routine(home_dir, slug, updates)


def patch_job(
    home_dir: str,
    job_id: str,
    body: Mapping,
    *,
    rooms: Mapping[str, str] | None = None,
) -> dict:
    from cron import jobs as cron_jobs

    owner, job = _resolved_job(home_dir, job_id)
    updates: dict = {}
    for key in ("name", "schedule"):
        if key in body:
            updates[key] = str(body.get(key) or "")
    if "prompt" in body:
        updates["prompt"] = str(body.get("prompt") or "")
    if "skill" in body:
        selected = str(body.get("skill") or "").strip()
        updates.update({"skills": [selected] if selected else [], "skill": selected or None})
    if "room" in body:
        selected_room = body.get("room")
        if not isinstance(selected_room, str) or not selected_room:
            raise ValueError("room must be a room id")
        meta = _dict(job.get("retinue"))
        meta["room"] = selected_room
        origin = _dict(job.get("origin"))
        origin.update(
            {
                "platform": ROOMS_PLATFORM,
                "chat_id": selected_room,
                "chat_name": f"room:{(rooms or {}).get(selected_room) or selected_room}",
            }
        )
        updates.update({"retinue": meta, "origin": origin})

    merged = {**dict(job), **updates}
    merged_skills = merged.get("skills")
    has_skill = isinstance(merged_skills, list) and bool(merged_skills)
    if not str(merged.get("prompt") or "").strip() and not has_skill and not merged.get("script"):
        raise ValueError("a scheduled job needs a skill, a prompt or a script")

    with scoped(home_dir, owner.owner):
        record = cron_jobs.update_job(job_id, updates) if updates else cron_jobs.get_job(job_id)
        if record is None:
            raise UnknownJob(job_id)
        if "enabled" in body:
            record = (
                cron_jobs.resume_job(job_id)
                if bool(body.get("enabled"))
                else cron_jobs.pause_job(job_id)
            )
        if record is None:
            raise UnknownJob(job_id)

    notified = _notify_provider(home_dir, owner.owner)
    with scoped(home_dir, owner.owner):
        if notified:
            record = _clear_registration_error(job_id) or record
        record = cron_jobs.get_job(job_id) or record
    _mirror_routine(home_dir, record, body)
    return shape(record, owner.owner, rooms)


def _simple_mutation(home_dir: str, job_id: str, operation, rooms=None) -> dict:
    from cron import jobs as cron_jobs

    owner, _ = _resolved_job(home_dir, job_id)
    with scoped(home_dir, owner.owner):
        record = operation(cron_jobs, job_id)
        if record is None:
            raise UnknownJob(job_id)
    notified = _notify_provider(home_dir, owner.owner)
    with scoped(home_dir, owner.owner):
        if notified:
            record = _clear_registration_error(job_id) or record
        record = cron_jobs.get_job(job_id) or record
    return shape(record, owner.owner, rooms)


def pause_job(home_dir: str, job_id: str, *, rooms=None) -> dict:
    return _simple_mutation(home_dir, job_id, lambda jobs, value: jobs.pause_job(value), rooms)


def resume_job(home_dir: str, job_id: str, *, rooms=None) -> dict:
    return _simple_mutation(home_dir, job_id, lambda jobs, value: jobs.resume_job(value), rooms)


def run_job(home_dir: str, job_id: str, *, rooms=None) -> dict:
    return _simple_mutation(home_dir, job_id, lambda jobs, value: jobs.trigger_job(value), rooms)


def delete_job(home_dir: str, job_id: str) -> dict:
    from cron import jobs as cron_jobs
    from . import routines

    owner, job = _resolved_job(home_dir, job_id)
    meta = _dict(job.get("retinue"))
    routine_slug = meta.get("routine_slug") if isinstance(meta.get("routine_slug"), str) else None
    with scoped(home_dir, owner.owner):
        if not cron_jobs.remove_job(job_id):
            raise UnknownJob(job_id)
    _notify_provider(home_dir, owner.owner)
    if routine_slug and routines.get_routine(home_dir, routine_slug) is not None:
        routines.update_routine(home_dir, routine_slug, {"job_id": None})
    return {"deleted": job_id, "routine_slug": routine_slug}
