"""Profile-scoped cron access contracts for Retinue rooms."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from . import cronjobs, routines, skilldraft


def _config(multiplex=False, allowlist=None):
    return SimpleNamespace(
        multiplex_profiles=multiplex,
        multiplex_profile_allowlist=allowlist,
    )


def _served(monkeypatch, home, pairs, *, multiplex=True):
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(cronjobs, "_gateway_config", lambda: _config(multiplex))
    monkeypatch.setattr(cronjobs, "_served_pairs", lambda _cfg: pairs)


def _job(home, owner="sally", **overrides):
    values = {
        "name": "Brief",
        "schedule": "every 1h",
        "room": "room-a",
        "prompt": "Prepare the brief",
        "skill": None,
        "rooms": {"room-a": "Room A"},
    }
    values.update(overrides)
    return cronjobs.create_job(str(home), owner, **values)


def test_multiplex_serves_default_and_named_profiles(tmp_path, monkeypatch):
    (tmp_path / "profiles" / "sally").mkdir(parents=True)
    (tmp_path / "profiles" / "editor").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cronjobs, "_gateway_config", lambda: _config(True))
    assert cronjobs.served_owners(str(tmp_path)) == ["default", "editor", "sally"]


def test_allowlist_hides_a_profile(tmp_path, monkeypatch):
    (tmp_path / "profiles" / "sally").mkdir(parents=True)
    (tmp_path / "profiles" / "editor").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cronjobs, "_gateway_config", lambda: _config(True, ["sally"]))
    assert cronjobs.served_owners(str(tmp_path)) == ["default", "sally"]


def test_multiplex_named_home_serves_root_and_siblings(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "sally"
    sibling = tmp_path / "profiles" / "janitor"
    home.mkdir(parents=True)
    sibling.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(cronjobs, "_gateway_config", lambda: _config(True))
    assert cronjobs.served_owners(str(home)) == ["default", "janitor", "sally"]
    assert cronjobs.owner_home(str(home), "default") == os.path.realpath(tmp_path)
    assert cronjobs.owner_home(str(home), "janitor") == os.path.realpath(sibling)
    assert cronjobs.owner_home(str(home), "sally") == os.path.realpath(home)
    for owner in ("default", "janitor", "sally"):
        _job(home, owner=owner, name=owner)
    assert {row["owner"] for row in cronjobs.list_jobs(str(home))} == {
        "default", "janitor", "sally"
    }


def test_multiplex_named_home_respects_the_allowlist(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "sally"
    (tmp_path / "profiles" / "janitor").mkdir(parents=True)
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(cronjobs, "_gateway_config", lambda: _config(True, ["sally"]))
    assert cronjobs.served_owners(str(home)) == ["default", "sally"]
    with pytest.raises(cronjobs.UnknownOwner):
        cronjobs.owner_home(str(home), "janitor")


def test_phantom_custom_profile_pair_is_dropped(tmp_path, monkeypatch):
    phantom = tmp_path / "profiles" / "custom"
    _served(monkeypatch, tmp_path, [("custom", str(phantom))], multiplex=False)
    assert cronjobs.served(str(tmp_path)) == []
    assert not phantom.exists()


def test_rooms_root_falls_back_when_the_environment_drifts(tmp_path, monkeypatch):
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    home = tmp_path / "other" / "profiles" / "sally"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(unrelated))
    assert cronjobs._rooms_root(str(home)) == os.path.realpath(tmp_path / "other")


def test_named_active_profile_is_served_under_its_real_slug(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "sally"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(cronjobs, "_gateway_config", lambda: _config(False))
    assert cronjobs.served_owners(str(home)) == ["sally"]
    assert cronjobs.owner_home(str(home), "sally") == os.path.realpath(home)
    with pytest.raises(cronjobs.UnknownOwner):
        cronjobs.owner_home(str(home), "default")
    assert not (tmp_path / "cron" / "jobs.json").exists()


def test_unnamed_root_home_is_served_as_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cronjobs, "_gateway_config", lambda: _config(False))
    assert cronjobs.served_owners(str(tmp_path)) == ["default"]


def test_home_slug_never_translates_a_named_profile(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "sally"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert cronjobs._home_slug(str(home)) == "sally"


def test_resolved_but_unmappable_pairs_yield_an_empty_served_set(tmp_path, monkeypatch):
    elsewhere = tmp_path.parent / f"{tmp_path.name}-elsewhere"
    elsewhere.mkdir()
    _served(monkeypatch, tmp_path, [("elsewhere", str(elsewhere))])
    assert cronjobs.served(str(tmp_path)) == []
    assert cronjobs.served_owners(str(tmp_path)) == []
    assert cronjobs.list_jobs(str(tmp_path)) == []
    with pytest.raises(cronjobs.UnknownOwner):
        cronjobs.owner_home(str(tmp_path), "default")
    assert not (tmp_path / "cron" / "jobs.json").exists()


def test_config_failure_falls_back_to_the_home_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cronjobs, "_gateway_config", lambda: None)
    assert cronjobs.served_owners(str(tmp_path)) == ["default"]
    home = tmp_path / "profiles" / "sally"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert cronjobs.served_owners(str(home)) == ["sally"]


def test_pairs_exception_falls_back_to_the_home_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cronjobs, "_gateway_config", lambda: _config())
    monkeypatch.setattr(cronjobs, "_served_pairs", lambda _cfg: (_ for _ in ()).throw(RuntimeError()))
    assert cronjobs.served_owners(str(tmp_path)) == ["default"]


def test_owner_validation_precedes_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(cronjobs, "served", lambda _home: pytest.fail("served was called"))
    for bad in ("", "../escape", "/absolute", "a/b"):
        with pytest.raises(ValueError):
            cronjobs.owner_home(str(tmp_path), bad)


def test_create_writes_into_the_owner_store(tmp_path, monkeypatch):
    home = tmp_path
    profile = home / "profiles" / "sally"
    profile.mkdir(parents=True)
    _served(monkeypatch, home, [("sally", str(profile))])
    row = _job(home, skill="brief", prompt="")
    assert row["owner"] == "sally"
    assert "owner_profile" not in row
    with cronjobs.scoped(str(home), "sally"):
        from cron import jobs as cron_jobs
        stored = cron_jobs.get_job(row["id"])
    assert stored["deliver"] == "origin"
    assert stored["origin"]["thread_id"] == "sally"
    assert stored["retinue"]["owner"] == "sally"
    assert stored["retinue"]["registration_error"] is None
    assert (profile / "cron" / "jobs.json").exists()
    assert not (home / "cron" / "jobs.json").exists()


class _Provider:
    name = "test"

    def __init__(self, fail_register=False):
        self.fail_register = fail_register
        self.changed = 0

    def register_job(self, _job):
        if self.fail_register:
            raise RuntimeError("registration unavailable")

    def on_jobs_changed(self):
        self.changed += 1


def _provider(monkeypatch, provider):
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler", lambda: provider
    )


def test_saved_job_survives_scheduler_registration_failure(tmp_path, monkeypatch):
    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    provider = _Provider(fail_register=True)
    _provider(monkeypatch, provider)
    row = _job(tmp_path, owner="default")
    assert "don't re-create it" in row["registration_error"]
    assert cronjobs.list_jobs(str(tmp_path))[0]["id"] == row["id"]


def test_registration_failure_skips_the_provider_notify(tmp_path, monkeypatch):
    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    provider = _Provider(fail_register=True)
    _provider(monkeypatch, provider)
    _job(tmp_path, owner="default")
    assert provider.changed == 0


def test_successful_create_notifies_the_provider(tmp_path, monkeypatch):
    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    provider = _Provider()
    _provider(monkeypatch, provider)
    _job(tmp_path, owner="default")
    assert provider.changed == 1


def test_registration_error_survives_a_fresh_list(tmp_path, monkeypatch):
    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    provider = _Provider(fail_register=True)
    _provider(monkeypatch, provider)
    created = _job(tmp_path, owner="default")
    assert cronjobs.list_jobs(str(tmp_path))[0]["registration_error"] == created["registration_error"]


def test_registration_error_is_cleared_when_a_mutation_redrives_the_provider(tmp_path, monkeypatch):
    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    provider = _Provider(fail_register=True)
    _provider(monkeypatch, provider)
    row = _job(tmp_path, owner="default")
    provider.fail_register = False
    updated = cronjobs.patch_job(str(tmp_path), row["id"], {"name": "Updated"})
    assert updated["registration_error"] is None


def test_registration_error_survives_a_mutation_that_skipped_the_provider(tmp_path, monkeypatch):
    sibling = tmp_path / "profiles" / "sally"
    sibling.mkdir(parents=True)
    _served(monkeypatch, tmp_path, [("default", str(tmp_path)), ("sally", str(sibling))])
    provider = _Provider(fail_register=True)
    _provider(monkeypatch, provider)
    row = _job(tmp_path, owner="default")
    provider.fail_register = False
    updated = cronjobs.patch_job(str(tmp_path), row["id"], {"name": "Updated"})
    assert updated["registration_error"]
    assert provider.changed == 0


def test_metadata_stamp_failure_rolls_back_the_created_job(tmp_path, monkeypatch):
    from cron import jobs as cron_jobs

    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    original = cron_jobs.update_job
    monkeypatch.setattr(cron_jobs, "update_job", lambda *_a, **_k: (_ for _ in ()).throw(OSError("stamp")))
    with pytest.raises(OSError):
        _job(tmp_path, owner="default")
    monkeypatch.setattr(cron_jobs, "update_job", original)
    with cronjobs.scoped(str(tmp_path), "default"):
        assert cron_jobs.list_jobs(include_disabled=True) == []


def test_pre_persistence_create_failure_leaves_an_empty_store(tmp_path, monkeypatch):
    from cron import jobs as cron_jobs

    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    monkeypatch.setattr(cron_jobs, "create_job", lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad")))
    with pytest.raises(ValueError):
        _job(tmp_path, owner="default")
    assert cronjobs.list_jobs(str(tmp_path)) == []


def test_shape_is_total():
    required = {
        "id", "owner", "name", "prompt", "skill", "kind", "routine_slug",
        "schedule", "schedule_display", "schedule_input", "next_run_at",
        "last_run_at", "last_status", "last_error", "last_delivery_error",
        "registration_error", "state", "enabled", "deliver", "room",
        "room_name", "repeat", "timezone",
    }
    assert set(cronjobs.shape({}, "default", None)) == required
    assert set(cronjobs.shape({"schedule": "bad", "origin": [], "retinue": 7}, "default", None)) == required


def test_external_provider_is_notified_when_only_one_owner_is_served(tmp_path, monkeypatch):
    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    provider = _Provider()
    _provider(monkeypatch, provider)
    row = _job(tmp_path, owner="default")
    cronjobs.pause_job(str(tmp_path), row["id"])
    assert provider.changed == 2


def test_external_provider_is_skipped_when_several_owners_are_served(tmp_path, monkeypatch):
    sibling = tmp_path / "profiles" / "sally"
    sibling.mkdir(parents=True)
    _served(monkeypatch, tmp_path, [("default", str(tmp_path)), ("sally", str(sibling))])
    provider = _Provider()
    _provider(monkeypatch, provider)
    row = _job(tmp_path, owner="default")
    cronjobs.pause_job(str(tmp_path), row["id"])
    assert provider.changed == 0


def test_patch_a_roomless_job_without_a_room_key(tmp_path, monkeypatch):
    from cron import jobs as cron_jobs

    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    with cronjobs.scoped(str(tmp_path), "default"):
        direct = cron_jobs.create_job(
            prompt="hello", schedule="every 1h", deliver="local",
            origin={"platform": "cli", "chat_id": "x"}, name="Original",
        )
    updated = cronjobs.patch_job(
        str(tmp_path), direct["id"], {"name": "Renamed", "schedule": "every 2h"}
    )
    assert updated["room"] is None
    assert updated["schedule_display"] == "every 120m"
    with cronjobs.scoped(str(tmp_path), "default"):
        stored = cron_jobs.get_job(direct["id"])
    assert stored["origin"] == direct["origin"]
    assert stored["deliver"] == "local"


def test_patch_with_an_empty_room_is_rejected(tmp_path, monkeypatch):
    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    row = _job(tmp_path, owner="default")
    with pytest.raises(ValueError, match="room must be a room id"):
        cronjobs.patch_job(str(tmp_path), row["id"], {"room": ""})


def test_patch_sets_a_room_when_given_one(tmp_path, monkeypatch):
    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    row = _job(tmp_path, owner="default")
    updated = cronjobs.patch_job(
        str(tmp_path), row["id"], {"room": "room-b"}, rooms={"room-b": "Room B"}
    )
    assert updated["room"] == "room-b"
    assert updated["room_name"] == "Room B"


def test_patch_clears_prompt_when_sent_empty(tmp_path, monkeypatch):
    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    row = _job(tmp_path, owner="default", skill="brief")
    updated = cronjobs.patch_job(str(tmp_path), row["id"], {"prompt": ""})
    assert updated["prompt"] == ""
    assert updated["skill"] == "brief"
    assert cronjobs.patch_job(str(tmp_path), row["id"], {"name": "x"})["prompt"] == ""


def test_patch_clears_skill_without_resurrecting_the_legacy_field(tmp_path, monkeypatch):
    from cron import jobs as cron_jobs

    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    row = _job(tmp_path, owner="default", skill="brief")
    assert cronjobs.patch_job(str(tmp_path), row["id"], {"skill": ""})["skill"] is None
    with cronjobs.scoped(str(tmp_path), "default"):
        stored = cron_jobs.get_job(row["id"])
    assert stored["skills"] == []
    assert stored["skill"] is None


def test_patch_cannot_strand_a_job_with_no_prompt_skill_or_script(tmp_path, monkeypatch):
    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    row = _job(tmp_path, owner="default", skill="brief")
    before = cronjobs.get_job(str(tmp_path), row["id"])[1]
    with pytest.raises(ValueError, match="needs a skill, a prompt or a script"):
        cronjobs.patch_job(str(tmp_path), row["id"], {"prompt": "", "skill": ""})
    assert cronjobs.get_job(str(tmp_path), row["id"])[1] == before


def test_patch_a_scripted_job_may_clear_prompt_and_skill(tmp_path, monkeypatch):
    from cron import jobs as cron_jobs

    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    script = tmp_path / "noop.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    with cronjobs.scoped(str(tmp_path), "default"):
        direct = cron_jobs.create_job(
            prompt="run", schedule="every 1h", script=str(script), no_agent=True,
            deliver="local", origin={"platform": "cli"}, skill="brief",
        )
    row = cronjobs.patch_job(
        str(tmp_path), direct["id"],
        {"prompt": "", "skill": "", "name": "Renamed", "schedule": "every 2h"},
    )
    assert row["prompt"] == "" and row["skill"] is None
    assert row["schedule_display"] == "every 120m"


def test_patch_mirrors_name_and_skill_to_linked_routine(tmp_path, monkeypatch):
    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    routines.save_routine(str(tmp_path), "Demo", ["do it"], skill="demo")
    row = _job(tmp_path, owner="default", kind="routine", routine_slug="demo", skill="demo")
    cronjobs.patch_job(str(tmp_path), row["id"], {"name": "New", "skill": "new-skill"})
    meta = routines.get_routine(str(tmp_path), "demo")
    assert meta["name"] == "New" and meta["skill"] == "new-skill"


def test_patch_does_not_rewrite_skill_md(tmp_path, monkeypatch):
    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    routines.save_routine(str(tmp_path), "Demo", ["do it"], skill="demo")
    skilldraft.write_skill_draft(
        str(tmp_path), "default", slug="demo", name="Demo", steps=["do it"],
        expected_output="done", source_room="room-a",
    )
    path = tmp_path / "skills" / "demo" / "SKILL.md"
    before = path.read_bytes()
    row = _job(tmp_path, owner="default", kind="routine", routine_slug="demo", skill="demo")
    cronjobs.patch_job(str(tmp_path), row["id"], {"name": "New"})
    assert path.read_bytes() == before


def test_patch_with_missing_routine_file_still_succeeds(tmp_path, monkeypatch):
    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    row = _job(
        tmp_path,
        owner="default",
        kind="routine",
        routine_slug="missing-routine",
        skill="demo",
    )
    updated = cronjobs.patch_job(str(tmp_path), row["id"], {"name": "Still works"})
    assert updated["name"] == "Still works"


def test_delete_clears_routine_job_id_and_keeps_skill(tmp_path, monkeypatch):
    _served(monkeypatch, tmp_path, [("default", str(tmp_path))], multiplex=False)
    routines.save_routine(str(tmp_path), "Demo", ["do it"], skill="demo", job_id="pending")
    skilldraft.write_skill_draft(
        str(tmp_path), "default", slug="demo", name="Demo", steps=["do it"],
        expected_output="done", source_room="room-a",
    )
    row = _job(tmp_path, owner="default", kind="routine", routine_slug="demo", skill="demo")
    routines.update_routine(str(tmp_path), "demo", {"job_id": row["id"]})
    cronjobs.delete_job(str(tmp_path), row["id"])
    assert routines.get_routine(str(tmp_path), "demo")["job_id"] is None
    assert (tmp_path / "skills" / "demo" / "SKILL.md").is_file()
