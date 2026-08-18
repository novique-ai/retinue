"""Cross-room post — membership is the whole gate (novique-ai/retinue#128).

Three invariants get most of the coverage here, because they are the three
ways this feature could quietly become a different feature:

* **Identity comes from the runtime scope.** Every tool-level test passes a
  hostile ``member=``/``speaker=`` argument alongside the real one and
  asserts the append is still attributed to whoever ``HERMES_HOME`` says.
* **The destination is an append, not a cycle.** ``post_user_message`` is
  the only entry point that schedules turns; the tests boobytrap it.
* **A refusal never becomes a delivery.** Each refusal path asserts the
  destination transcript is untouched, not merely that the prose is right.
"""

from __future__ import annotations

import json
import os

import pytest

from . import crossroom, engine, tools
from .crossroom import (
    REASON_AMBIGUOUS,
    REASON_NOT_A_MEMBER,
    REASON_SAME_ROOM,
    REASON_UNKNOWN,
)
from .engine import KIND_AGENT, KIND_USER, Room, RoomMessage
from .store import RoomStore


# ── fixtures ─────────────────────────────────────────────────────────────


def _room(**kwargs) -> Room:
    defaults = dict(id="r-a", name="Alpha", members=["scout", "editor"], lead="scout")
    defaults.update(kwargs)
    return Room(**defaults)


def _profile_home(tmp_path, member: str) -> str:
    """The home a member's turn actually runs with: <home>/profiles/<slug>."""
    path = tmp_path / "profiles" / member
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _scope(tmp_path, monkeypatch, member: str) -> RoomStore:
    """Point HERMES_HOME at *member*'s profile; return the workspace store.

    The store deliberately lives under the workspace home, not the profile
    home — that split is the bug ``workspace_home`` exists to prevent, so
    the fixture reproduces the real layout rather than flattening it.
    """
    monkeypatch.setenv("HERMES_HOME", _profile_home(tmp_path, member))
    return RoomStore(base_dir=str(tmp_path / "retinue_rooms"))


def _hire(tmp_path, slug: str, display_name: str) -> None:
    """Give *slug* a hire record so display-name mentions resolve."""
    pdir = tmp_path / "profiles" / slug
    pdir.mkdir(parents=True, exist_ok=True)
    from . import hire

    with open(pdir / hire.AGENT_META_FILENAME, "w", encoding="utf-8") as f:
        json.dump({"slug": slug, "display_name": display_name}, f)


def _texts(store: RoomStore, room_id: str) -> list[str]:
    return [m.text for m in store.read_since(room_id, 0)]


@pytest.fixture
def two_rooms(tmp_path, monkeypatch):
    """scout is in Alpha and Beta; editor is in Alpha only."""
    store = _scope(tmp_path, monkeypatch, "scout")
    store.create(_room(id="r-a", name="Alpha", members=["scout", "editor"]))
    store.create(_room(id="r-b", name="Beta", members=["scout", "critic"]))
    return store


# ── identity from scope, never from an argument ──────────────────────────


def test_workspace_home_collapses_a_profile_home(tmp_path):
    home = str(tmp_path)
    assert crossroom.workspace_home(_profile_home(tmp_path, "scout")) == home
    assert crossroom.workspace_home(home) == home


def test_caller_member_reads_the_profile_from_the_runtime_scope(tmp_path):
    assert crossroom.caller_member(_profile_home(tmp_path, "scout")) == "scout"
    assert crossroom.caller_member(str(tmp_path)) == "default"
    assert crossroom.caller_member("") is None


def test_identity_cannot_be_passed_as_a_tool_argument(two_rooms, tmp_path, monkeypatch):
    """A model that types someone else's slug does not borrow their turn."""
    store = two_rooms
    with crossroom.in_room("r-a"):
        out = tools.rooms_post(
            {
                "room": "Beta",
                "message": "handing this over",
                # Every plausible way a model might try to claim to be
                # someone else. None of them is read.
                "member": "critic",
                "speaker": "critic",
                "from": "critic",
                "caller": "critic",
                "as": "critic",
            }
        )
    assert out == crossroom.confirmation_line("Beta")
    posted = store.read_since("r-b", 0)
    assert [m.speaker for m in posted] == ["scout"]


def test_rooms_list_ignores_an_identity_argument(two_rooms, tmp_path, monkeypatch):
    # editor is in Alpha only; scout's scope must not be borrowable.
    with crossroom.in_room("r-a"):
        out = tools.rooms_list({"member": "critic", "speaker": "critic"})
    assert "#Alpha" in out and "#Beta" in out
    monkeypatch.setenv("HERMES_HOME", _profile_home(tmp_path, "editor"))
    with crossroom.in_room("r-a"):
        out = tools.rooms_list({"member": "scout"})
    assert "#Alpha" in out and "#Beta" not in out


def test_tools_refuse_outside_a_room_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "retinue_rooms").mkdir()
    # No current room bound: identity resolves ("default") but there is no
    # source room, so a post has nowhere to say it came from.
    assert "only works inside a room turn" in tools.rooms_post(
        {"room": "Beta", "message": "hi"}
    )


# ── the happy path ───────────────────────────────────────────────────────


def test_member_of_both_rooms_posts_from_a_into_b(two_rooms):
    store = two_rooms
    with crossroom.in_room("r-a"):
        out = tools.rooms_post({"room": "Beta", "message": "draft is ready"})

    assert out == "Posted to #Beta."
    posted = store.read_since("r-b", 0)
    assert len(posted) == 1
    line = posted[0]
    assert line.kind == KIND_AGENT
    assert line.speaker == "scout"
    assert line.text == "(from #Alpha) draft is ready"
    # The source room is not written to — the confirmation is the turn's
    # own reply, not a second transcript line.
    assert _texts(store, "r-a") == []


def test_room_id_is_accepted_as_well_as_the_display_name(two_rooms):
    store = two_rooms
    with crossroom.in_room("r-a"):
        assert tools.rooms_post({"room": "r-b", "message": "by id"}) == "Posted to #Beta."
    assert _texts(store, "r-b") == ["(from #Alpha) by id"]


def test_oversized_message_is_refused_not_truncated(two_rooms):
    store = two_rooms
    with crossroom.in_room("r-a"):
        out = tools.rooms_post(
            {"room": "Beta", "message": "x" * (crossroom.MAX_POST_CHARS + 1)}
        )
    assert "limit is" in out
    assert _texts(store, "r-b") == []


# ── membership is the gate ───────────────────────────────────────────────


def test_member_of_a_only_cannot_post_into_b(tmp_path, monkeypatch):
    """editor is in Alpha only. Beta is not reachable — and not disclosed."""
    store = _scope(tmp_path, monkeypatch, "editor")
    store.create(_room(id="r-a", name="Alpha", members=["scout", "editor"]))
    store.create(_room(id="r-b", name="Beta", members=["scout", "critic"]))

    with crossroom.in_room("r-a"):
        out = tools.rooms_post({"room": "Beta", "message": "let me in"})

    assert "No room of yours matches 'Beta'" in out
    # Fail closed: nothing landed in Beta.
    assert _texts(store, "r-b") == []
    # And the refusal is not a membership oracle — it neither confirms nor
    # denies that a room called Beta exists.
    assert "#Beta" not in out


def test_not_a_member_fails_closed_even_if_the_candidate_list_widens(
    two_rooms, monkeypatch
):
    """The belt-and-braces gate in rooms_post is load-bearing, so test it.

    ``resolve_destination`` is only ever handed the caller's own rooms
    today. This asserts the second check underneath it: a future caller
    that widens the candidate list refuses instead of delivering.
    """
    store = two_rooms
    gamma = _room(id="r-g", name="Gamma", members=["critic"])
    store.create(gamma)
    monkeypatch.setattr(
        crossroom,
        "member_rooms",
        lambda rooms, member: [r for r in rooms if r.id in {"r-a", "r-g"}],
    )

    with crossroom.in_room("r-a"):
        out = tools.rooms_post({"room": "Gamma", "message": "should not land"})

    assert out == crossroom.refusal_line(REASON_NOT_A_MEMBER, "Gamma", [gamma])
    assert "not a member" in out
    assert _texts(store, "r-g") == []


def test_archived_rooms_are_not_a_destination(tmp_path, monkeypatch):
    store = _scope(tmp_path, monkeypatch, "scout")
    store.create(_room(id="r-a", name="Alpha", members=["scout"]))
    store.create(_room(id="r-b", name="Beta", members=["scout"], archived=True))
    with crossroom.in_room("r-a"):
        out = tools.rooms_post({"room": "Beta", "message": "anyone there"})
    assert "No room of yours matches" in out
    assert _texts(store, "r-b") == []


# ── resolution refusals ──────────────────────────────────────────────────


def test_unknown_name_is_unknown():
    rooms = [_room(id="r-b", name="Beta", members=["scout"])]
    assert crossroom.resolve_destination("Zeta", rooms) == (None, REASON_UNKNOWN)
    assert crossroom.resolve_destination("", rooms) == (None, REASON_UNKNOWN)


def test_ambiguous_name_refuses_rather_than_guessing():
    rooms = [
        _room(id="r-1", name="Beta", members=["scout"]),
        _room(id="r-2", name="Beta", members=["scout"]),
    ]
    assert crossroom.resolve_destination("Beta", rooms) == (None, REASON_AMBIGUOUS)

    prefixes = [
        _room(id="r-1", name="Beta", members=["scout"]),
        _room(id="r-2", name="Bengal", members=["scout"]),
    ]
    assert crossroom.resolve_destination("Be", prefixes) == (None, REASON_AMBIGUOUS)
    # An exact name still wins over the tie its prefix would create.
    assert crossroom.resolve_destination("Beta", prefixes)[0].id == "r-1"


def test_exact_id_beats_a_name_that_collides_with_it():
    rooms = [
        _room(id="Beta", name="Something Else", members=["scout"]),
        _room(id="r-2", name="Beta", members=["scout"]),
    ]
    assert crossroom.resolve_destination("Beta", rooms)[0].id == "Beta"


def test_ambiguous_destination_is_refused_at_the_tool(tmp_path, monkeypatch):
    store = _scope(tmp_path, monkeypatch, "scout")
    store.create(_room(id="r-a", name="Alpha", members=["scout"]))
    store.create(_room(id="r-1", name="Beta", members=["scout"]))
    store.create(_room(id="r-2", name="Bengal", members=["scout"]))

    with crossroom.in_room("r-a"):
        out = tools.rooms_post({"room": "Be", "message": "which one"})

    assert "matches more than one of your rooms" in out
    assert _texts(store, "r-1") == [] and _texts(store, "r-2") == []


def test_same_room_is_refused_with_its_own_reason(two_rooms):
    store = two_rooms
    with crossroom.in_room("r-a"):
        out = tools.rooms_post({"room": "Alpha", "message": "talking to myself"})

    assert out == crossroom.refusal_line(REASON_SAME_ROOM, "Alpha", [])
    assert "is this room" in out
    # Not delivered anywhere, least of all back into the room it came from.
    assert _texts(store, "r-a") == []
    assert _texts(store, "r-b") == []


# ── the destination does not start a cycle ───────────────────────────────


def test_post_does_not_go_through_post_user_message(two_rooms, monkeypatch):
    """A cross-room line schedules nobody.

    Turn cycles begin at ``post_user_message`` and nowhere else, so this
    boobytraps it: a refactor that routes the destination append through
    the cycle entry point fails here instead of in production, where the
    symptom would be two rooms triggering each other.
    """
    from .adapter import RetinueRoomsAdapter

    def _boom(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("cross-room post must not start a turn cycle")

    monkeypatch.setattr(RetinueRoomsAdapter, "post_user_message", _boom)
    monkeypatch.setattr(RetinueRoomsAdapter, "_run_cycle", _boom)

    store = two_rooms
    with crossroom.in_room("r-a"):
        assert tools.rooms_post({"room": "Beta", "message": "fyi"}) == "Posted to #Beta."

    posted = store.read_since("r-b", 0)
    # KIND_USER is what plan_user_turns keys off. An agent line is inert.
    assert [m.kind for m in posted] == [KIND_AGENT]
    assert not any(m.kind == KIND_USER for m in posted)
    # last_seen untouched: nobody was scheduled, so nobody consumed it.
    assert store.get("r-b").last_seen == {}


def test_live_mentions_are_defanged_in_the_destination(two_rooms, tmp_path):
    """@critic in Beta would read as a handoff nobody will ever take."""
    _hire(tmp_path, "critic", "Critic")
    store = two_rooms
    with crossroom.in_room("r-a"):
        tools.rooms_post({"room": "Beta", "message": "@critic can you review this"})

    text = _texts(store, "r-b")[0]
    assert text == "(from #Alpha) critic can you review this"
    assert "@" not in text


def test_defang_only_touches_members_of_the_destination():
    members = ["critic", "scout"]
    out = crossroom.defang_mentions("@critic ping @nobody and @scout", members)
    assert out == "critic ping @nobody and scout"


def test_defang_respects_display_names_and_leaves_fenced_code_alone():
    members = ["critic"]
    names = {"critic": "Critic"}
    assert crossroom.defang_mentions("@Critic look", members, names) == "Critic look"
    fenced = "see ```\n@Critic\n``` above"
    assert crossroom.defang_mentions(fenced, members, names) == fenced


def test_destination_line_names_the_source_room():
    assert crossroom.destination_line("Alpha", " hi ") == "(from #Alpha) hi"
    assert crossroom.destination_line("", "hi") == "(from #another room) hi"


# ── briefing ─────────────────────────────────────────────────────────────


def test_member_rooms_and_other_rooms_are_membership_scoped():
    rooms = [
        _room(id="r-a", name="Alpha", members=["scout", "editor"]),
        _room(id="r-b", name="Beta", members=["scout"]),
        _room(id="r-c", name="Gamma", members=["critic"]),
        _room(id="r-d", name="Delta", members=["scout"], archived=True),
    ]
    assert [r.id for r in crossroom.member_rooms(rooms, "scout")] == ["r-a", "r-b"]
    assert [r.id for r in crossroom.other_rooms(rooms, "scout", "r-a")] == ["r-b"]
    assert crossroom.member_rooms(rooms, "critic") == [rooms[2]]
    assert crossroom.member_rooms(rooms, "") == []


def test_briefing_names_the_members_other_rooms():
    room = _room(id="r-a", name="Alpha", members=["scout", "editor"], lead="scout")
    others = [_room(id="r-b", name="Beta", members=["scout"])]
    text = engine.room_briefing(room, "scout", ["Mark"], other_rooms=others)
    assert "You are also a member of: #Beta." in text
    assert "rooms_post" in text and "rooms_list" in text
    assert "does not start a turn there" in text


def test_briefing_does_not_name_rooms_the_member_is_not_in():
    """The briefing shows exactly what the gate allows — no more."""
    rooms = [
        _room(id="r-a", name="Alpha", members=["scout", "editor"]),
        _room(id="r-b", name="Beta", members=["scout"]),
        _room(id="r-c", name="Gamma", members=["critic"]),
    ]
    room = rooms[0]
    others = crossroom.other_rooms(rooms, "editor", "r-a")
    text = engine.room_briefing(room, "editor", ["Mark"], other_rooms=others)
    assert others == []
    assert "also a member of" not in text
    assert "#Gamma" not in text and "rooms_post" not in text

    scout_text = engine.room_briefing(
        room, "scout", ["Mark"], other_rooms=crossroom.other_rooms(rooms, "scout", "r-a")
    )
    assert "#Beta" in scout_text
    assert "#Gamma" not in scout_text


def test_briefing_is_unchanged_without_the_new_kwarg():
    """Existing callers (and existing tests) see no drift."""
    room = _room(lead="scout")
    assert engine.room_briefing(room, "scout", ["Mark"]) == engine.room_briefing(
        room, "scout", ["Mark"], other_rooms=[]
    )
    assert "rooms_post" not in engine.room_briefing(room, "scout", ["Mark"])


# ── tool registration ────────────────────────────────────────────────────


def test_register_wires_the_tools_even_when_the_adapter_defers():
    """`provides_tools` is a promise; register() has to keep it.

    A member's turn runs in a secondary profile scope, where the platform
    adapter deliberately declines — that is exactly the process that needs
    rooms_post. Registering tools only alongside a live adapter would make
    the capability unreachable for every retainer it was built for.
    """
    from . import register

    registered: dict[str, dict] = {}

    class _Ctx:
        def register_tool(self, name, toolset, schema, handler, **kw):
            registered[name] = {"toolset": toolset, "handler": handler}

        def register_platform(self, **kw):
            raise RuntimeError("adapter unavailable in this process")

    register(_Ctx())

    assert set(registered) == {"rooms_list", "rooms_post"}
    assert {v["toolset"] for v in registered.values()} == {tools.TOOLSET}
    assert registered["rooms_post"]["handler"] is tools.rooms_post


def test_plugin_manifest_declares_exactly_the_registered_tools():
    """Discovery reads provides_tools; a drifted list is a silent outage."""
    import re

    path = os.path.join(os.path.dirname(__file__), "plugin.yaml")
    with open(path, encoding="utf-8") as f:
        body = f.read()
    block = body.split("provides_tools:", 1)[1]
    declared = []
    for line in block.splitlines()[1:]:
        m = re.match(r"^\s+-\s+(\S+)\s*$", line)
        if not m:
            break
        declared.append(m.group(1))
    assert set(declared) == set(tools._HANDLERS)
