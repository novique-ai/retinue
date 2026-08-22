"""Every member's skills reach the room container (novique-ai/retinue#188).

A room shares ONE container, created once. Upstream's skills mount resolves
the profile that happened to create it, so in a multi-member room exactly one
member's ``profiles/<slug>/skills`` was present — under the canonical path
``/root/.hermes/skills`` that every skill's own documentation points at. The
other members' skill scripts and their skill-local ``.env`` credentials were
simply not in the container, and the failure read as a missing credential.

These tests pin the replacement: every member's skills dir is mounted
read-only at its OWN path at container-creation time, and the speaking
member's turn resolves that path instead of the shared canonical one.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess

import pytest

from . import hire, ide
from .engine import KIND_USER, Room, RoomMessage, room_briefing
from .store import RoomStore


def _room(**kwargs) -> Room:
    defaults = dict(id="r-1", name="Test", members=["admin", "patty", "claude"])
    defaults.update(kwargs)
    return Room(**defaults)


def _skills(home, slug: str, skill: str = "vikunja-pm"):
    """Create ``profiles/<slug>/skills/<skill>/`` with a skill-local .env."""
    path = home / "profiles" / slug / "skills" / skill
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    (path / ".env").write_text("VIKUNJA_API_TOKEN=tok\n", encoding="utf-8")
    return path.parent


def _member_specs(vols: list) -> list:
    return [v for v in vols if ide.MEMBER_SKILLS_MOUNT in v]


def _volumes(room, monkeypatch) -> list:
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    return json.loads(ide.overlay_env(room)["TERMINAL_DOCKER_VOLUMES"])


def test_every_member_reaches_its_own_skills_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    admin = _skills(tmp_path, "admin", "devops")
    patty = _skills(tmp_path, "patty")
    vols = _volumes(_room(workspace="sandbox"), monkeypatch)
    assert _member_specs(vols) == [
        f"{admin}:{ide.MEMBER_SKILLS_MOUNT}/admin:ro",
        f"{patty}:{ide.MEMBER_SKILLS_MOUNT}/patty:ro",
    ]


def test_member_skills_are_mounted_in_an_ide_room_too(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    root = tmp_path / "IDE"
    root.mkdir()
    patty = _skills(tmp_path, "patty")
    vols = _volumes(_room(workspace="ide", ide_path=str(root)), monkeypatch)
    assert f"{patty}:{ide.MEMBER_SKILLS_MOUNT}/patty:ro" in vols
    assert f"{root}:/workspace:rw" in vols


def test_a_member_without_skills_does_not_break_container_creation(tmp_path, monkeypatch):
    """claude has no skills dir: no mount, no error, and the key still resolves."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _skills(tmp_path, "patty")
    room = _room(workspace="sandbox")
    vols = _volumes(room, monkeypatch)
    assert [v for v in vols if "/claude" in v] == []
    assert len(_member_specs(vols)) == 1
    assert ide.container_key_for_room(room)


def test_no_member_skills_at_all_leaves_the_volume_set_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert _volumes(_room(workspace="sandbox"), monkeypatch) == []


def test_the_mount_is_never_the_shared_canonical_skills_path(tmp_path, monkeypatch):
    """Member skills go to a member path, never over /root/.hermes/skills.

    Writing them to the canonical path would collide with upstream's own
    skills mount (duplicate destination) and would keep every member pointed
    at one tree — the defect, moved.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _skills(tmp_path, "patty")
    for spec in _volumes(_room(workspace="sandbox"), monkeypatch):
        assert not spec.endswith(":/root/.hermes/skills:ro")
        assert ":/root/.hermes/skills:" not in spec


def test_a_symlinked_skills_dir_is_refused_not_mounted(tmp_path, monkeypatch):
    """A bind mount follows symlinks; a skills dir holding one is not mounted.

    Upstream sanitises by copying, but its sanitiser keeps ONE process-wide
    temp dir and deletes it on the next call — mounting two symlinked member
    dirs would silently empty the first. Refusing is the honest answer here.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE", encoding="utf-8")
    skills = _skills(tmp_path, "patty")
    os.symlink(secret, skills / "vikunja-pm" / "leak.pem")
    assert ide.member_skills_host_dir("patty") is None
    assert _member_specs(_volumes(_room(workspace="sandbox"), monkeypatch)) == []


def test_a_bogus_member_name_can_never_build_a_mount(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for bad in ("../../etc", "a/b", "", "   ", ".."):
        assert ide.member_skills_host_dir(bad) is None


def test_turn_env_points_the_speaking_member_at_its_own_skills(tmp_path, monkeypatch):
    """The container is shared per ROOM; only the per-TURN env is per member."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    from gateway.config import PlatformConfig
    from tools import turn_env

    from .adapter import RetinueRoomsAdapter

    _skills(tmp_path, "patty")
    for slug in ("patty", "claude"):
        pdir = tmp_path / "profiles" / slug
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / hire.AGENT_META_FILENAME).write_text(
            json.dumps({"display_name": slug, "slug": slug, "job": "x", "how": ""}),
            encoding="utf-8",
        )

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    room = Room(
        id="r-1", name="T", members=["patty", "claude"], lead="patty", workspace="sandbox"
    )
    adapter.store.create(room)
    adapter.store.append(
        room.id, RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hi")
    )

    seen: dict = {}

    def _turn(member: str) -> None:
        async def fake_handle_message(event):
            seen[member] = dict(turn_env.current() or {})
            adapter._resolve_pending(room.id, ok=True, text="done", member=member)

        adapter.handle_message = fake_handle_message
        ok, _ = asyncio.run(adapter._agent_turn(room, member))
        assert ok

    _turn("patty")
    _turn("claude")

    assert seen["patty"][ide.MEMBER_SKILLS_ENV] == f"{ide.MEMBER_SKILLS_MOUNT}/patty"
    # A member with no skills dir has no mount, so the turn advertises none
    # rather than pointing at a path that is not in the container.
    assert ide.MEMBER_SKILLS_ENV not in seen["claude"]
    assert turn_env.current() is None


def test_briefing_tells_the_member_where_its_own_skills_are(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _skills(tmp_path, "patty")
    room = _room(workspace="sandbox")
    mine = room_briefing(room, "patty", ["You"])
    assert f"{ide.MEMBER_SKILLS_MOUNT}/patty" in mine
    assert ide.MEMBER_SKILLS_ENV in mine
    # A member with no skills dir is told nothing about a mount it lacks.
    assert ide.MEMBER_SKILLS_MOUNT not in room_briefing(room, "claude", ["You"])


def test_inviting_a_member_rekeys_the_container_and_evicts_the_old_one(
    tmp_path, monkeypatch
):
    """Membership can change while a room lives.

    Per-member remounting is impossible in a running container, so the mount
    set is part of the container's identity: adding a member with skills
    changes the key, the next cycle builds a container that HAS their mount,
    and the old one is disposed instead of leaking.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    from gateway.config import PlatformConfig
    from tools import terminal_tool

    from .adapter import RetinueRoomsAdapter

    _skills(tmp_path, "patty")
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    created = adapter.create_room("Lab", ["admin"], None, None, workspace="sandbox")
    before = adapter.store.get(created["id"])
    assert before is not None
    old_key = ide.container_key_for_room(before)

    class _Env:
        def __init__(self) -> None:
            self.cleaned = False

        def cleanup(self) -> None:
            self.cleaned = True

    stale = _Env()
    monkeypatch.setattr(terminal_tool, "_active_environments", {old_key: stale})
    monkeypatch.setattr(terminal_tool, "_last_activity", {old_key: 1.0})

    adapter.add_room_member(created["id"], "patty")
    after = adapter.store.get(created["id"])
    assert after is not None
    new_key = ide.container_key_for_room(after)
    vols = json.loads(ide.overlay_env(after)["TERMINAL_DOCKER_VOLUMES"])
    assert _member_specs(vols) == [
        f"{tmp_path / 'profiles' / 'patty' / 'skills'}:{ide.MEMBER_SKILLS_MOUNT}/patty:ro"
    ]
    assert new_key != old_key
    assert old_key not in terminal_tool._active_environments
    assert stale.cleaned is True


def test_removing_a_member_rekeys_and_evicts_too(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    from gateway.config import PlatformConfig
    from tools import terminal_tool

    from .adapter import RetinueRoomsAdapter

    _skills(tmp_path, "patty")
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    created = adapter.create_room("Lab", ["admin", "patty"], None, None, workspace="sandbox")
    before = adapter.store.get(created["id"])
    assert before is not None
    old_key = ide.container_key_for_room(before)

    class _Env:
        def __init__(self) -> None:
            self.cleaned = False

        def cleanup(self) -> None:
            self.cleaned = True

    stale = _Env()
    monkeypatch.setattr(terminal_tool, "_active_environments", {old_key: stale})
    monkeypatch.setattr(terminal_tool, "_last_activity", {old_key: 1.0})

    adapter.remove_room_member(created["id"], "patty")
    after = adapter.store.get(created["id"])
    assert after is not None
    assert ide.container_key_for_room(after) != old_key
    assert old_key not in terminal_tool._active_environments
    assert stale.cleaned is True


def test_restaffing_through_patch_rekeys_and_evicts(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    from gateway.config import PlatformConfig
    from tools import terminal_tool

    from .adapter import RetinueRoomsAdapter

    _skills(tmp_path, "patty")
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    created = adapter.create_room("Lab", ["admin"], None, None, workspace="sandbox")
    before = adapter.store.get(created["id"])
    assert before is not None
    old_key = ide.container_key_for_room(before)

    class _Env:
        def __init__(self) -> None:
            self.cleaned = False

        def cleanup(self) -> None:
            self.cleaned = True

    stale = _Env()
    monkeypatch.setattr(terminal_tool, "_active_environments", {old_key: stale})
    monkeypatch.setattr(terminal_tool, "_last_activity", {old_key: 1.0})

    adapter.patch_room(created["id"], {"members": ["admin", "patty"]})
    after = adapter.store.get(created["id"])
    assert after is not None
    assert ide.container_key_for_room(after) != old_key
    assert old_key not in terminal_tool._active_environments
    assert stale.cleaned is True


def test_container_key_ignores_member_order(tmp_path, monkeypatch):
    """The same roster in a different order is the same container."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    _skills(tmp_path, "patty")
    _skills(tmp_path, "admin", "devops")
    a = ide.container_key_for_room(_room(members=["admin", "patty"]))
    b = ide.container_key_for_room(_room(members=["patty", "admin"]))
    assert a == b


@pytest.mark.skipif(
    not __import__("shutil").which("podman"), reason="podman not on PATH"
)
def test_podman_mounts_two_members_skills_at_their_own_paths(tmp_path, monkeypatch):
    """Smoke: the generated specs are real, read-only, member-scoped binds."""
    import shutil
    import subprocess
    import uuid

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    runtime = shutil.which("podman")
    assert runtime
    _skills(tmp_path, "patty")
    _skills(tmp_path, "admin", "devops")
    specs = _member_specs(_volumes(_room(workspace="sandbox"), monkeypatch))
    assert len(specs) == 2
    name = f"retinue-skills-{uuid.uuid4().hex[:8]}"
    args = [runtime, "run", "-d", "--name", name]
    for spec in specs:
        args += ["-v", spec]
    args += ["docker.io/library/python:3.12-slim", "sleep", "60"]

    def run(cmd, timeout=120):
        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )

    try:
        created = run(args)
        if created.returncode != 0:
            pytest.skip(f"podman run failed (image pull?): {created.stderr[-400:]}")
        got = run(
            [
                runtime,
                "exec",
                name,
                "cat",
                f"{ide.MEMBER_SKILLS_MOUNT}/patty/vikunja-pm/.env",
            ]
        )
        assert got.returncode == 0, got.stderr
        assert "VIKUNJA_API_TOKEN=tok" in got.stdout
        other = run(
            [runtime, "exec", name, "ls", f"{ide.MEMBER_SKILLS_MOUNT}/admin"]
        )
        assert other.returncode == 0, other.stderr
        assert "devops" in other.stdout
        wrote = run(
            [
                runtime,
                "exec",
                name,
                "sh",
                "-c",
                f"echo x > {ide.MEMBER_SKILLS_MOUNT}/patty/nope",
            ]
        )
        assert wrote.returncode != 0, "member skills must be read-only"
    finally:
        run([runtime, "rm", "-f", name], timeout=30)


def test_overlay_env_sets_skip_profile_skills_mount(monkeypatch):
    """Rooms tell the docker backend not to mount the creating profile's skills.

    novique-ai/retinue#192: /root/.hermes/skills is the anchor's tree, readable
    by every member. overlay_env suppresses that mount; members keep their
    own path via member_skills/<slug>.
    """
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    env = ide.overlay_env(_room(workspace="sandbox"))
    assert env["TERMINAL_DOCKER_SKIP_PROFILE_SKILLS_MOUNT"] == "1"
    assert env["TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE"] == "0"


def test_briefing_does_not_claim_canonical_skills_path_is_another_member(
    tmp_path, monkeypatch
):
    """#192: /root/.hermes/skills is not mounted in rooms. Fail loud, not silent."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _skills(tmp_path, "patty")
    mine = room_briefing(_room(workspace="sandbox"), "patty", ["You"])
    assert "another member's skills" not in mine
    assert "/root/.hermes/skills is not mounted" in mine


def _docker_run_calls(monkeypatch):
    """Capture ``docker run`` argv from DockerEnvironment construction."""
    from tools.environments import docker as docker_env

    docker_env._cgroup_limits_ok = True
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    calls = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd) if isinstance(cmd, list) else cmd)
        if isinstance(cmd, list) and len(cmd) >= 2:
            if cmd[1] == "version":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="Docker version", stderr=""
                )
            if cmd[1] == "run":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="fake-container-id\n", stderr=""
                )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_env.subprocess, "run", _run)
    return docker_env, calls


def _last_docker_run(calls):
    run_calls = [
        c for c in calls if isinstance(c, list) and len(c) >= 2 and c[1] == "run"
    ]
    assert run_calls, "docker run should have been called"
    return " ".join(run_calls[-1])


def test_docker_honours_skip_profile_skills_mount(tmp_path, monkeypatch):
    """Flag off: canonical skills volume present. Flag on: omitted.

    External skill dirs stay either way — they are shared checkouts, not
    per-member credentials (novique-ai/retinue#192).
    """
    hermes_home = tmp_path / ".hermes"
    skills_dir = hermes_home / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "profile-skill").mkdir()
    external = tmp_path / "external-skills"
    external.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(
        "agent.skill_utils.get_external_skills_dirs", lambda: [external]
    )
    monkeypatch.setattr("agent.skill_utils.get_project_skills_dirs", lambda: [])
    monkeypatch.setattr(
        "tools.credential_files.get_credential_file_mounts", lambda: []
    )
    monkeypatch.setattr(
        "tools.credential_files.get_cache_directory_mounts", lambda: []
    )

    docker_env, calls = _docker_run_calls(monkeypatch)

    monkeypatch.delenv("TERMINAL_DOCKER_SKIP_PROFILE_SKILLS_MOUNT", raising=False)
    docker_env.DockerEnvironment(
        image="python:3.11",
        persistent_filesystem=False,
        cpu=0,
        memory=0,
        disk=0,
        task_id="skills-unset",
    )
    unset_args = _last_docker_run(calls)
    assert ":/root/.hermes/skills:ro" in unset_args
    assert ":/root/.hermes/external_skills/0:ro" in unset_args

    calls.clear()
    monkeypatch.setenv("TERMINAL_DOCKER_SKIP_PROFILE_SKILLS_MOUNT", "1")
    docker_env.DockerEnvironment(
        image="python:3.11",
        persistent_filesystem=False,
        cpu=0,
        memory=0,
        disk=0,
        task_id="skills-set",
    )
    set_args = _last_docker_run(calls)
    assert ":/root/.hermes/skills:ro" not in set_args
    assert ":/root/.hermes/external_skills/0:ro" in set_args


def test_room_overlay_makes_docker_skip_the_canonical_skills_volume(
    tmp_path, monkeypatch
):
    """The rooms path: overlay_env bound via workspace_context, not process env."""
    from tools import workspace_context

    hermes_home = tmp_path / ".hermes"
    skills_dir = hermes_home / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "profile-skill").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    monkeypatch.delenv("TERMINAL_DOCKER_SKIP_PROFILE_SKILLS_MOUNT", raising=False)
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [])
    monkeypatch.setattr("agent.skill_utils.get_project_skills_dirs", lambda: [])
    monkeypatch.setattr(
        "tools.credential_files.get_credential_file_mounts", lambda: []
    )
    monkeypatch.setattr(
        "tools.credential_files.get_cache_directory_mounts", lambda: []
    )

    docker_env, calls = _docker_run_calls(monkeypatch)
    overlay = ide.overlay_env(_room(workspace="sandbox"))
    with workspace_context.workspace(overlay):
        docker_env.DockerEnvironment(
            image="python:3.11",
            persistent_filesystem=False,
            cpu=0,
            memory=0,
            disk=0,
            task_id="skills-overlay",
        )
    args = _last_docker_run(calls)
    assert ":/root/.hermes/skills:ro" not in args
