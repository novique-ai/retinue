"""Unit tests for the room engine + store (no gateway required).

Run:  .venv/bin/python -m pytest plugins/platforms/retinue_rooms/ -q
"""

from __future__ import annotations

from pathlib import Path

from . import engine
from .engine import KIND_AGENT, KIND_SYSTEM, KIND_USER, Room, RoomMessage
from .store import RoomStore


def _room(**kwargs) -> Room:
    defaults = dict(id="r-1", name="Test", members=["scout", "editor", "critic"])
    defaults.update(kwargs)
    return Room(**defaults)


# ── mentions ─────────────────────────────────────────────────────────────


def test_join_draft_and_speech_prefixes_mentions():
    assert engine.join_draft_and_speech("", "hello") == "hello"
    assert engine.join_draft_and_speech("  ", "hello") == "hello"
    assert engine.join_draft_and_speech("@Patty", "") == "@Patty"
    assert (
        engine.join_draft_and_speech("@Patty ", "I want you to file the invoice")
        == "@Patty I want you to file the invoice"
    )
    assert (
        engine.join_draft_and_speech("@Patty please look at", "the invoice")
        == "@Patty please look at the invoice"
    )


def test_rewrite_spoken_address_at_and_hey():
    members = ["patty", "ellie", "claude", "admin"]
    names = {
        "patty": "Patty",
        "ellie": "Ellie",
        "claude": "Claude",
        "admin": "Carlos",
    }
    assert (
        engine.rewrite_spoken_address(
            "at Patty, I want you to file the invoice", members, names
        )
        == "@Patty I want you to file the invoice"
    )
    assert (
        engine.rewrite_spoken_address("hey Ellie how are you", members, names)
        == "@Ellie how are you"
    )
    assert (
        engine.rewrite_spoken_address("yo Claude check this", members, names)
        == "@Claude check this"
    )
    # STT often inserts a comma after Hey (Voice Testing 1, seq 84).
    assert (
        engine.rewrite_spoken_address("Hey, Dave, how are you doing?", ["dave"], {"dave": "Dave"})
        == "@Dave how are you doing?"
    )


def test_rewrite_spoken_address_greeting_is_kept():
    members = ["ellie", "claude", "admin"]
    names = {"ellie": "Ellie", "claude": "Claude", "admin": "Carlos"}
    assert (
        engine.rewrite_spoken_address(
            "Hi, Ellie. This is a voice test. How are you doing?",
            members,
            names,
        )
        == "@Ellie Hi, Ellie. This is a voice test. How are you doing?"
    )
    assert (
        engine.rewrite_spoken_address("hello Claude this is a test", members, names)
        == "@Claude hello Claude this is a test"
    )


def test_rewrite_spoken_address_name_comma_and_room():
    members = ["claude", "ellie"]
    names = {"claude": "Claude", "ellie": "Ellie"}
    assert (
        engine.rewrite_spoken_address("Claude, review the patch", members, names)
        == "@Claude review the patch"
    )
    assert engine.rewrite_spoken_address("at room stand by", members, names) == (
        "@room stand by"
    )
    assert engine.rewrite_spoken_address("hey room", members, names) == "@room"


def test_rewrite_spoken_address_does_not_steal():
    members = ["patty", "ellie", "editor", "sally", "scout"]
    names = {
        "patty": "Patty",
        "ellie": "Ellie",
        "editor": "Editor",
        "sally": "Sally",
        "scout": "Scout",
    }
    assert (
        engine.rewrite_spoken_address("look at Patty later", members, names)
        == "look at Patty later"
    )
    assert (
        engine.rewrite_spoken_address("@Ellie already tapped", members, names)
        == "@Ellie already tapped"
    )
    assert (
        engine.rewrite_spoken_address("at the invoice", members, names)
        == "at the invoice"
    )
    assert engine.rewrite_spoken_address("at S what now", members, names) == (
        "at S what now"
    )
    assert engine.rewrite_spoken_address("at Ed please", members, names) == (
        "at Ed please"
    )


def test_rewrite_spoken_address_stt_near_miss():
    members = ["mangus", "dave", "admin"]
    names = {"mangus": "Mangus", "dave": "Dave", "admin": "Carlos"}
    # Voice Testing 1 seq 79: STT wrote Mingus for Mangus.
    assert (
        engine.rewrite_spoken_address(
            "Hey, Mingus, how are you doing today?", members, names
        )
        == "@Mangus how are you doing today?"
    )
    assert (
        engine.rewrite_spoken_address("at Mingus stand by", members, names)
        == "@Mangus stand by"
    )
    # Two one-edit hits must not steal a turn.
    members = ["patty", "petty"]
    names = {"patty": "Patty", "petty": "Petty"}
    assert engine.rewrite_spoken_address("at Potty hello", members, names) == (
        "at Potty hello"
    )


def test_rewrite_spoken_address_unique_prefix():
    members = ["claude", "ellie"]
    names = {"claude": "Claude", "ellie": "Ellie"}
    assert (
        engine.rewrite_spoken_address("at Claud how are you", members, names)
        == "@Claude how are you"
    )


def test_join_draft_and_speech_clips_a_long_prefix():
    draft = "@Patty " + ("x" * (engine._MAX_AUDIO_DRAFT + 50))
    got = engine.join_draft_and_speech(draft, "now")
    assert got.startswith("@Patty ")
    assert got.endswith(" now")
    assert len(got) <= engine._MAX_AUDIO_DRAFT + len(" now")


def test_mentions_in_order_deduped_case_insensitive():
    room = _room()
    got = engine.parse_mentions("@Editor then @scout, and @EDITOR again", room.members)
    assert got == ["editor", "scout"]


def test_mentions_ignore_non_members_and_support_hyphens():
    members = ["data-scout", "editor"]
    got = engine.parse_mentions("cc @data-scout @nobody @Mark", members)
    assert got == ["data-scout"]


def test_heading_mention_is_live_fenced_mention_is_not():
    members = ["sheila-graphics-and-visual-produ", "editor"]
    names = {"sheila-graphics-and-visual-produ": "Sheila", "editor": "Editor"}
    text = (
        "### @Sheila — please take the image pass\n\n"
        "```markdown\n"
        "Call @editor in the blog body as an example.\n"
        "```\n"
    )
    assert engine.parse_mentions(text, members, names) == [
        "sheila-graphics-and-visual-produ"
    ]


def test_tilde_fence_is_also_literal():
    members = ["editor"]
    text = "~~~md\n@editor is example copy\n~~~\n"
    assert engine.parse_mentions(text, members) == []


def test_display_name_and_slug_address_same_member():
    members = ["sheila-graphics-and-visual-produ", "editor"]
    names = {"sheila-graphics-and-visual-produ": "Sheila", "editor": "Editor"}
    assert engine.parse_mentions("@Sheila then @editor", members, names) == [
        "sheila-graphics-and-visual-produ",
        "editor",
    ]
    assert engine.parse_mentions(
        "@sheila-graphics-and-visual-produ", members, names
    ) == ["sheila-graphics-and-visual-produ"]


def test_ambiguous_prefix_does_not_steal_a_turn():
    members = ["sally", "scout"]
    names = {"sally": "Sally", "scout": "Scout"}
    assert engine.parse_mentions("hey @S what do you think", members, names) == []


def test_unique_prefix_resolves_to_one_member():
    members = ["sheila-graphics-and-visual-produ", "editor"]
    names = {"sheila-graphics-and-visual-produ": "Sheila", "editor": "Editor"}
    assert engine.parse_mentions("@Sheil please", members, names) == [
        "sheila-graphics-and-visual-produ"
    ]


def test_followups_skip_self_when_called_by_display_name():
    room = _room(members=["sheila-graphics-and-visual-produ", "editor"])
    names = {"sheila-graphics-and-visual-produ": "Sheila", "editor": "Editor"}
    got = engine.plan_agent_followups(
        room,
        "sheila-graphics-and-visual-produ",
        "done @Sheila — @editor please tighten",
        [],
        5,
        names,
    )
    assert got == ["editor"]


def test_already_queued_still_skipped_with_aliases():
    room = _room(members=["sheila-graphics-and-visual-produ", "editor"])
    names = {"sheila-graphics-and-visual-produ": "Sheila", "editor": "Editor"}
    got = engine.plan_agent_followups(
        room,
        "scout",
        "@Sheila @editor",
        ["sheila-graphics-and-visual-produ"],
        5,
        names,
    )
    assert got == ["editor"]


def test_mention_handle_prefers_unique_display_name():
    members = ["sheila-graphics-and-visual-produ", "editor"]
    names = {"sheila-graphics-and-visual-produ": "Sheila", "editor": "Editor"}
    assert (
        engine.mention_handle(
            "sheila-graphics-and-visual-produ", "Sheila", members, names
        )
        == "Sheila"
    )


def test_mention_handle_falls_back_when_first_names_collide():
    members = ["sheila-a", "sheila-b"]
    names = {"sheila-a": "Sheila West", "sheila-b": "Sheila East"}
    assert engine.mention_handle("sheila-a", "Sheila West", members, names) == "sheila-a"


def test_mention_regex_does_not_match_emails():
    got = engine.parse_mentions("mail me at mark@how3ll.net", ["how3ll"])
    # 'how3ll' appears after '@' in an email; accepting it would make every
    # email address a turn trigger. Documented current behavior: it DOES
    # match the token — the engine relies on member names not colliding with
    # mail domains. If this becomes a problem, tighten the regex with a
    # lookbehind and update this test.
    assert got == ["how3ll"]


# ── user-turn planning ───────────────────────────────────────────────────


def test_user_message_with_mentions_plans_those_turns():
    room = _room()
    assert engine.plan_user_turns(room, "@critic then @scout please") == ["critic", "scout"]


def test_user_message_without_mentions_goes_to_lead():
    room = _room(lead="editor")
    assert engine.plan_user_turns(room, "what do you all think?") == ["editor"]


def test_room_broadcast_plans_every_member():
    room = _room(lead="editor")
    assert engine.plan_user_turns(room, "Hello @room") == [
        "scout",
        "editor",
        "critic",
    ]


def test_room_broadcast_keeps_explicit_mentions_first():
    room = _room()
    assert engine.plan_user_turns(room, "@critic look at this @room") == [
        "critic",
        "scout",
        "editor",
    ]


def test_fenced_room_broadcast_is_not_live():
    room = _room(lead="editor")
    text = "```\nHello @room\n```\n"
    assert engine.has_room_broadcast(text) is False
    assert engine.plan_user_turns(room, text) == ["editor"]


def test_agent_followup_room_does_not_fan_out():
    room = _room()
    got = engine.plan_agent_followups(room, "scout", "thanks @room", [], 5)
    assert got == []


def test_no_lead_falls_back_to_first_member():
    room = _room(lead=None)
    assert engine.plan_user_turns(room, "hello") == ["scout"]


def test_invalid_lead_falls_back_to_first_member():
    room = _room(lead="ghost")
    assert engine.plan_user_turns(room, "hello") == ["scout"]


# ── follow-up planning (the budget rules) ────────────────────────────────


def test_followups_exclude_self():
    room = _room()
    got = engine.plan_agent_followups(room, "scout", "I think @scout and @editor", [], 5)
    assert got == ["editor"]


def test_followups_exclude_already_queued():
    room = _room()
    got = engine.plan_agent_followups(room, "scout", "@editor @critic", ["critic"], 5)
    assert got == ["editor"]


def test_followups_truncated_to_budget():
    room = _room()
    got = engine.plan_agent_followups(room, "scout", "@editor @critic", [], 1)
    assert got == ["editor"]


def test_followups_zero_budget_yields_nothing():
    room = _room()
    assert engine.plan_agent_followups(room, "scout", "@editor", [], 0) == []


# ── sequential turns ─────────────────────────────────────────────────────


def test_take_wave_is_one_speaker_even_with_budget():
    """A leftover budget is not a license to fan out. Reviewers must see
    the earlier reply before they start."""
    wave, rest = engine.take_wave(["scout", "editor", "critic"], 8)
    assert wave == ["scout"]
    assert rest == ["editor", "critic"]


def test_take_wave_zero_or_empty():
    assert engine.take_wave(["scout"], 0) == ([], ["scout"])
    assert engine.take_wave([], 4) == ([], [])


def test_sequential_cycle_next_speaker_sees_prior_reply():
    """Engine-level walk of one user message: mention order is a queue,
    each reply is recorded before the next speaker is taken, and a
    follow-up @mention is appended after the remaining queue."""
    room = _room(members=["sally", "sheila", "editor", "scout"], lead="sally")
    queue = engine.plan_user_turns(room, "@sally @editor please")
    spoken: list[str] = []
    transcript: list[tuple[str, str]] = []
    replies = {
        "sally": "draft here. @sheila header image.",
        "editor": "tightened the draft.",
        "sheila": "image brief ready.",
    }
    budget = 8
    while queue and len(spoken) < budget:
        wave, queue = engine.take_wave(queue, budget - len(spoken))
        assert len(wave) == 1
        member = wave[0]
        # Prior speakers' replies are already on the transcript.
        assert [s for s, _ in transcript] == spoken
        spoken.append(member)
        text = replies[member]
        transcript.append((member, text))
        queue.extend(
            engine.merge_followups(
                room, [(member, text)], queue, spoken, budget - len(spoken)
            )
        )
    assert [s for s, _ in transcript] == ["sally", "editor", "sheila"]
    assert transcript[0][1].startswith("draft here")
    assert spoken.index("sally") < spoken.index("editor")
    assert spoken.index("editor") < spoken.index("sheila")


def test_merge_followups_from_replies_preserves_mention_order():
    """Follow-ups from several replies merge in speaker-then-mention
    order, skipping anyone already spoken/queued."""
    room = _room()
    follow = engine.merge_followups(
        room,
        replies=[
            ("scout", "hand this to @critic and @editor"),
            ("editor", "also ask @critic"),
        ],
        already_queued=[],
        already_spoken=["scout", "editor"],
        budget_left=4,
    )
    # editor is already spoken, critic once only
    assert follow == ["critic"]


def test_merge_followups_respects_budget():
    room = _room(members=["scout", "editor", "critic", "herald"])
    follow = engine.merge_followups(
        room,
        replies=[("scout", "@editor @critic @herald")],
        already_queued=[],
        already_spoken=["scout"],
        budget_left=2,
    )
    assert follow == ["editor", "critic"]


# ── speak-or-pass + round settling ───────────────────────────────────────


def test_pass_is_the_structured_payload_not_prose():
    """Pass is an exact JSON object, not a regex over free-form output.

    Upstream group chats infer silence by matching ``(pass)`` in prose and
    misfire on preambles / 'image pass'. The engine must not.
    """
    token = engine.pass_payload_text()
    assert engine.is_pass_reply(token) is True
    assert engine.is_pass_reply('  {"pass": true}  \n') is True
    assert engine.is_pass_reply('{"pass":true}') is True
    assert engine.classify_turn(True, token) == engine.TURN_PASS

    # Surrounding prose, the upstream-style marker, and the English word
    # are all spoken replies — not a pass.
    for text in (
        "I'll (pass) on this.",
        "please take the image pass",
        "(pass)",
        "pass",
        '{"pass": true} and also here is a note',
        'Sure.\n{"pass": true}',
        '{"pass": true, "reason": "nothing to add"}',
        '[{"pass": true}]',
        "true",
        "",
        "   ",
    ):
        assert engine.is_pass_reply(text) is False, text
        assert engine.classify_turn(True, text) == engine.TURN_SPEAK, text


def test_classify_turn_fail_is_not_a_pass():
    """A failed turn stays FAIL even if the error text looks like the payload."""
    token = engine.pass_payload_text()
    assert engine.classify_turn(False, token) == engine.TURN_FAIL
    assert engine.classify_turn(False, "no reply within 300s") == engine.TURN_FAIL
    assert engine.classify_turn(True, "hello") == engine.TURN_SPEAK


def test_plan_followup_round_skips_first_wave_and_respects_budget():
    members = ["scout", "editor", "critic"]
    assert engine.plan_followup_round(members, skip=["scout"], budget_left=8) == [
        "editor",
        "critic",
    ]
    assert engine.plan_followup_round(members, skip=["scout"], budget_left=1) == [
        "editor"
    ]
    assert engine.plan_followup_round(members, skip=["scout"], budget_left=0) == []
    # Empty skip is "everyone still to ask" (first-wave all passed / no-op).
    assert engine.plan_followup_round(members, skip=[], budget_left=8) == members
    # Nobody left to ask — the room has settled this round.
    assert engine.plan_followup_round(members, skip=members, budget_left=8) == []


def test_followup_round_settled_when_nobody_spoke():
    assert engine.followup_round_settled([]) is True
    assert engine.followup_round_settled(["editor"]) is False


def test_room_max_followup_rounds_defaults_and_roundtrips():
    room = _room()
    assert room.max_followup_rounds == engine.DEFAULT_MAX_FOLLOWUP_ROUNDS
    assert engine.DEFAULT_MAX_FOLLOWUP_ROUNDS == 0
    loaded = Room.from_dict(room.to_dict())
    assert loaded.max_followup_rounds == 0
    three = Room.from_dict({**room.to_dict(), "max_followup_rounds": 3})
    assert three.max_followup_rounds == 3
    missing = Room.from_dict({"id": "r-1", "name": "Test", "members": ["scout"]})
    assert missing.max_followup_rounds == 0


def test_first_wave_routing_unchanged_then_followups_settle():
    """@mentions / lead still pick the first wave. Follow-up rounds are
    everyone else, and a full round of passes settles the room."""
    room = _room(lead="scout")
    first = engine.plan_user_turns(room, "what do you all think?")
    assert first == ["scout"]
    follow = engine.plan_followup_round(
        room.members, skip=first, budget_left=room.max_agent_turns - 1
    )
    assert follow == ["editor", "critic"]
    assert engine.followup_round_settled([]) is True

    mentioned = engine.plan_user_turns(room, "@critic then @scout please")
    assert mentioned == ["critic", "scout"]
    follow_mentioned = engine.plan_followup_round(
        room.members, skip=mentioned, budget_left=6
    )
    assert follow_mentioned == ["editor"]


def test_followup_round_cap_is_a_bounded_loop():
    """Engine-level walk: after the first wave, at most N follow-up
    rounds run; a speech re-opens a round, a full pass settles early."""
    room = _room(lead="scout", max_followup_rounds=2)
    first = engine.plan_user_turns(room, "hello")
    attempted = list(first)
    spoken = list(first)
    rounds = 0
    while rounds < room.max_followup_rounds:
        wave = engine.plan_followup_round(
            room.members, skip=attempted if rounds == 0 else spoken[-1:], budget_left=8
        )
        if not wave:
            break
        rounds += 1
        round_speakers = []
        for member in wave:
            # editor speaks once; everyone else passes
            if member == "editor" and rounds == 1:
                round_speakers.append(member)
        attempted.extend(wave)
        if engine.followup_round_settled(round_speakers):
            break
        spoken.extend(round_speakers)
    assert rounds == 2  # editor spoke in round 1, round 2 all passed
    assert spoken == ["scout", "editor"]


# ── budget-exhaustion drop report (#189) ─────────────────────────────────


def test_pending_mentioned_exposes_mentions_that_did_not_run():
    """Last-round @mention of a member who already spoke is still pending.

    merge_followups skips already-attempted members, so the assignment
    dies with the round cap unless the engine reports the drop.
    """
    room = _room(members=["scout", "claude"], lead="scout")
    replies = [
        ("scout", "lead start"),
        ("claude", "claude first"),
        ("scout", "@claude go on T5-T8"),
    ]
    attempted = ["scout", "claude", "scout"]
    pending = engine.pending_mentioned(room, replies, attempted)
    assert pending == ["claude"]
    drop = engine.dropped_pending(
        pending, reason=engine.REASON_FOLLOWUP_ROUNDS, used=2
    )
    assert drop is not None
    assert drop.speakers == ("claude",)
    assert drop.reason == engine.REASON_FOLLOWUP_ROUNDS
    assert drop.used == 2
    text = engine.dropped_pending_notice(drop)
    assert text == (
        "⚠️ round budget reached — @claude did not get a turn "
        "(2 followup rounds used). A new user message grants fresh turns."
    )
    assert text.startswith(engine.CYCLE_ROUND_BUDGET_PREFIX)
    assert engine.is_cycle_abort_notice(text)


def test_pending_mentioned_empty_when_mentioned_member_ran_after():
    room = _room(members=["scout", "claude"], lead="scout")
    replies = [("scout", "@claude please take this"), ("claude", "on it")]
    attempted = ["scout", "claude"]
    assert engine.pending_mentioned(room, replies, attempted) == []
    assert (
        engine.dropped_pending([], reason=engine.REASON_FOLLOWUP_ROUNDS, used=2)
        is None
    )


def test_dropped_pending_empty_when_nothing_pending():
    assert engine.dropped_pending([], reason=engine.REASON_AGENT_TURNS, used=8) is None
    notice = engine.dropped_pending_notice(None)
    assert notice is None


def test_pending_mentioned_ignores_self_mentions_and_already_noticed():
    room = _room(members=["scout", "claude", "editor"], lead="scout")
    replies = [("scout", "@scout keep going. @claude and @editor please.")]
    attempted = ["scout"]
    # leftover queue already named by cycle_budget_notice
    pending = engine.pending_mentioned(
        room, replies, attempted, exclude=["editor"]
    )
    assert pending == ["claude"]


def test_dropped_pending_notice_turn_budget_and_several_speakers():
    drop = engine.dropped_pending(
        ["claude", "editor"], reason=engine.REASON_AGENT_TURNS, used=1
    )
    text = engine.dropped_pending_notice(drop)
    assert text.startswith(engine.CYCLE_ROUND_BUDGET_PREFIX)
    assert "@claude" in text and "@editor" in text
    assert "did not get a turn" in text
    assert "1 agent turn used" in text
    assert "A new user message grants fresh turns." in text
    assert engine.is_cycle_abort_notice(text)


def test_briefing_teaches_the_structured_pass():
    text = engine.room_briefing(_room(lead="scout"), "scout", ["Mark"])
    token = engine.pass_payload_text()
    assert token in text
    assert "pass" in text.lower()
    # Must not tell the model to write the upstream regex bait as prose.
    assert "write (pass)" not in text.lower()


# ── formatting ───────────────────────────────────────────────────────────


def test_invite_notices_match_existing_system_voice():
    assert engine.member_joined_notice("critic") == "critic joined the room"
    assert engine.member_left_notice("critic") == "critic left the room"
    assert engine.turn_started_notice("Lucy") == "Lucy is on it."
    assert engine.INVITE_TRANSCRIPT_WINDOW == 20


def test_seed_invite_last_seen_long_room_is_head_minus_window():
    room = _room()
    engine.seed_invite_last_seen(room, "critic", 26)
    assert room.last_seen["critic"] == 6


def test_seed_invite_last_seen_short_or_empty_is_zero():
    room = _room()
    engine.seed_invite_last_seen(room, "critic", 5)
    assert room.last_seen["critic"] == 0
    empty = _room()
    engine.seed_invite_last_seen(empty, "critic", 0)
    assert empty.last_seen["critic"] == 0


def test_seed_invite_last_seen_keeps_existing_entry_including_zero():
    room = _room()
    room.last_seen["critic"] = 7
    engine.seed_invite_last_seen(room, "critic", 100)
    assert room.last_seen["critic"] == 7
    room.last_seen["critic"] = 0
    engine.seed_invite_last_seen(room, "critic", 100)
    assert room.last_seen["critic"] == 0


def test_with_members_snapshot_ignores_live_roster():
    live = _room(members=["scout", "editor", "critic"], lead="scout")
    planned = engine.plan_user_turns(
        engine.with_members(live, ["scout", "editor"]), "@critic hello"
    )
    # critic is on the live roster but not in this cycle's snapshot, so
    # the mention is ignored and the lead takes the turn.
    assert planned == ["scout"]
    still_queued = engine.plan_user_turns(
        engine.with_members(live, ["scout", "critic"]), "@critic"
    )
    assert still_queued == ["critic"]


def test_did_not_reply_notice_is_what_the_web_ui_parses():
    text = engine.did_not_reply_notice(
        "sheila-graphics-and-visual-produ", "agent returned no reply"
    )
    assert text == (
        "sheila-graphics-and-visual-produ did not reply (agent returned no reply)"
    )
    assert text.startswith("sheila-graphics-and-visual-produ" + engine.DID_NOT_REPLY_INFIX)
    abort = engine.cycle_internal_error_notice()
    assert abort.startswith(engine.CYCLE_INTERNAL_ERROR_PREFIX)
    budget = engine.cycle_budget_notice(8, ["scribe", "admin"])
    assert budget.startswith(engine.CYCLE_BUDGET_PREFIX)
    assert "scribe, admin" in budget
    stopped = engine.cycle_stopped_notice("Mark")
    assert stopped.startswith(engine.CYCLE_STOPPED_PREFIX)
    assert "Mark stopped this turn." in stopped
    assert engine.cycle_stopped_notice() == engine.CYCLE_STOPPED_PREFIX
    assert engine.cycle_stopped_notice("  ") == engine.CYCLE_STOPPED_PREFIX
    assert engine.is_cycle_abort_notice(stopped)


def test_web_thinking_prefixes_stay_in_lockstep():
    thinking = (
        Path(__file__).resolve().parents[3] / "retinue-web" / "src" / "thinking.ts"
    ).read_text(encoding="utf-8")
    for prefix in (
        engine.CYCLE_INTERNAL_ERROR_PREFIX,
        engine.CYCLE_BUDGET_PREFIX,
        engine.CYCLE_STOPPED_PREFIX,
        engine.CYCLE_ROUND_BUDGET_PREFIX,
        engine.DID_NOT_REPLY_INFIX,
    ):
        assert prefix in thinking, prefix


def test_old_agent_only_filter_misses_no_reply_system_line():
    """The live Graphics Test bug: thinking stayed up because the UI only
    dropped a waiter on kind=agent. A system 'did not reply' is the end of
    that turn and must clear the bubble."""
    slug = "sheila-graphics-and-visual-produ"
    fresh = [
        RoomMessage(
            seq=9,
            ts=1,
            kind=KIND_SYSTEM,
            speaker="room",
            text=engine.did_not_reply_notice(slug, "agent returned no reply"),
        )
    ]
    old_filter = [
        w
        for w in [slug]
        if not any(m.kind == KIND_AGENT and m.speaker == w for m in fresh)
    ]
    assert old_filter == [slug]
    assert engine.remaining_thinkers([slug], fresh) == []


def test_remaining_thinkers_keeps_queued_after_one_failure():
    fresh = [
        RoomMessage(
            seq=2,
            ts=1,
            kind=KIND_SYSTEM,
            speaker="room",
            text=engine.did_not_reply_notice("admin", "agent returned no reply"),
        )
    ]
    assert engine.remaining_thinkers(
        ["admin", "sheila-graphics-and-visual-produ"], fresh
    ) == ["sheila-graphics-and-visual-produ"]


def test_remaining_thinkers_after_ignores_previous_turn_reply():
    slug = "sheila-graphics-and-visual-produ"
    messages = [
        RoomMessage(1, 1, KIND_USER, "You", "first"),
        RoomMessage(2, 2, KIND_AGENT, slug, "here is a still"),
        RoomMessage(3, 3, KIND_USER, "You", "@Sheila show it again"),
    ]
    assert engine.remaining_thinkers_after([slug], messages) == [slug]
    messages.append(
        RoomMessage(
            4,
            4,
            KIND_SYSTEM,
            "room",
            engine.did_not_reply_notice(slug, "agent returned no reply"),
        )
    )
    assert engine.remaining_thinkers_after([slug], messages) == []


def test_cycle_abort_clears_the_whole_queue():
    waiting = ["admin", "scribe"]
    assert (
        engine.remaining_thinkers(
            waiting,
            [
                RoomMessage(
                    1, 1, KIND_SYSTEM, "room", engine.cycle_internal_error_notice()
                )
            ],
        )
        == []
    )
    assert (
        engine.remaining_thinkers(
            waiting,
            [RoomMessage(1, 1, KIND_SYSTEM, "room", engine.cycle_budget_notice(8, ["scribe"]))],
        )
        == []
    )
    assert (
        engine.remaining_thinkers(
            waiting,
            [RoomMessage(1, 1, KIND_SYSTEM, "room", engine.cycle_stopped_notice("Mark"))],
        )
        == []
    )
    drop = engine.dropped_pending(
        ["claude"], reason=engine.REASON_FOLLOWUP_ROUNDS, used=2
    )
    assert (
        engine.remaining_thinkers(
            waiting,
            [
                RoomMessage(
                    1, 1, KIND_SYSTEM, "room", engine.dropped_pending_notice(drop)
                )
            ],
        )
        == []
    )


def test_format_lines_attribution():
    msgs = [
        RoomMessage(seq=1, ts=1, kind=KIND_USER, speaker="Mark", text="hi"),
        RoomMessage(seq=2, ts=2, kind=KIND_AGENT, speaker="scout", text="hello"),
        RoomMessage(seq=3, ts=3, kind=KIND_SYSTEM, speaker="room", text="budget reached"),
    ]
    block = engine.format_lines(msgs)
    assert block.splitlines() == [
        "[Mark] hi",
        "[scout (agent)] hello",
        "[room] budget reached",
    ]


def test_delta_window_matches_invite_window():
    """Idle-turn injection and invite seeding share one bound."""
    assert engine.DELTA_TRANSCRIPT_WINDOW == engine.INVITE_TRANSCRIPT_WINDOW
    assert engine.DELTA_TRANSCRIPT_WINDOW >= 1


def test_cap_delta_keeps_newest_and_counts_omitted():
    cap = engine.DELTA_TRANSCRIPT_WINDOW
    msgs = [
        RoomMessage(seq=i, ts=float(i), kind=KIND_USER, speaker="You", text=f"line-{i}")
        for i in range(1, cap + 6)
    ]
    kept, omitted = engine.cap_delta(msgs)
    assert omitted == 5
    assert [m.text for m in kept] == [f"line-{i}" for i in range(6, cap + 6)]
    assert kept[0].seq == msgs[omitted].seq
    assert kept[-1] is msgs[-1]


def test_cap_delta_under_window_elides_nothing():
    msgs = [
        RoomMessage(seq=i, ts=float(i), kind=KIND_USER, speaker="You", text=f"line-{i}")
        for i in range(1, 4)
    ]
    kept, omitted = engine.cap_delta(msgs)
    assert omitted == 0
    assert kept == msgs


def test_format_delta_context_prepends_elision_notice():
    prior = [
        RoomMessage(seq=2, ts=2, kind=KIND_USER, speaker="Mark", text="later"),
        RoomMessage(seq=3, ts=3, kind=KIND_AGENT, speaker="editor", text="ack"),
    ]
    block = engine.format_delta_context(prior, omitted=5)
    assert block.splitlines() == [
        f"[room] {engine.omitted_delta_notice(5)}",
        "[Mark] later",
        "[editor (agent)] ack",
    ]
    assert engine.omitted_delta_notice(5) == "5 earlier messages omitted"


def test_format_delta_context_without_elision_matches_format_lines():
    prior = [RoomMessage(seq=1, ts=1, kind=KIND_USER, speaker="Mark", text="hi")]
    assert engine.format_delta_context(prior, omitted=0) == engine.format_lines(prior)
    assert engine.format_delta_context([], omitted=0) is None
    assert engine.format_delta_context([], omitted=3) == (
        f"[room] {engine.omitted_delta_notice(3)}"
    )


def test_briefing_names_room_and_members():
    room = _room(lead="scout")
    text = engine.room_briefing(room, "scout", ["Mark"])
    assert "You are scout" in text and '"Test"' in text
    assert "@editor" in text and "@critic" in text
    assert "Mark" in text
    assert "Then stop" in text
    assert "say so briefly" not in text
    assert "never stay silent" in text
    assert "Never keep calling tools until the turn times out" in text
    assert "missing permission" in text
    assert "clarify" in text
    assert "/workspace/uploads/" in text


def test_briefing_includes_principal_about():
    room = _room(lead="scout")
    text = engine.room_briefing(
        room,
        "scout",
        ["Clayton"],
        principal_about="Call me Clayton. I run this workspace.",
    )
    assert "Humans here: Clayton." in text
    assert "About Clayton: Call me Clayton. I run this workspace." in text
    assert "does not take agent turns" in text


def test_briefing_mentions_shared_folder_only_when_configured(tmp_path, monkeypatch):
    """A mount nobody is told about is a mount nobody uses."""
    room = _room(lead="scout")
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    assert "/shared" not in engine.room_briefing(room, "scout", ["Mark"])

    monkeypatch.setenv("RETINUE_SHARED_DIR", str(tmp_path))
    text = engine.room_briefing(room, "scout", ["Mark"])
    assert "/shared is a folder shared with every room" in text
    assert f"/shared/rooms/{room.id}/" in text
    assert "/shared/inbox/" in text
    assert "filing cabinet" in text
    assert "read-only" not in text


def test_briefing_shared_folder_says_readonly_when_ro(tmp_path, monkeypatch):
    monkeypatch.setenv("RETINUE_SHARED_DIR", str(tmp_path))
    text = engine.room_briefing(_room(lead="scout", shared_mode="ro"), "scout", ["Mark"])
    assert "/shared is a read-only folder shared with every room." in text
    assert "cannot write there" in text


def test_briefing_lists_room_artifacts():
    room = _room(lead="scout")
    text = engine.room_briefing(
        room,
        "scout",
        ["Mark"],
        artifacts=["/workspace/uploads/midnight_monolith.png"],
    )
    assert "Work already in this room: /workspace/uploads/midnight_monolith.png." in text
    assert "Reuse those paths" in text


def test_fallback_reply_is_spoken_not_a_crash():
    assert engine.fallback_reply("@Sheila show me that image again") == engine.FALLBACK_MEDIA
    assert engine.fallback_reply("what is the status") == engine.FALLBACK_GENERIC
    assert engine.fallback_reply("Make me an image of Herby the Lovebug") == engine.FALLBACK_GENERIC
    assert "crash" not in engine.fallback_reply("picture please").lower()


def test_failed_turn_reply_is_spoken_and_not_the_empty_answer():
    timeout = engine.failed_turn_reply("no reply within 900s")
    dispatch = engine.failed_turn_reply("dispatch failed: boom")
    assert timeout == engine.TIMEOUT_REPLY
    assert dispatch == engine.DISPATCH_REPLY
    assert timeout != engine.FALLBACK_GENERIC
    assert dispatch != engine.FALLBACK_GENERIC
    assert timeout != dispatch
    assert "ask me to continue" not in timeout.lower()


def test_failed_turn_reply_names_clarify_permission_and_missing_path():
    class _Entry:
        question = "Is this really infrastructure-5ta4.6?"

    assert "infrastructure-5ta4.6" in engine.failed_turn_reply(
        "no reply within 900s", clarify=_Entry()
    )
    assert "permission" in engine.failed_turn_reply(
        "no reply within 900s",
        last_tool={"name": "terminal", "output": "Permission denied: /workspace/x"},
    ).lower()
    assert "could not find" in engine.failed_turn_reply(
        "no reply within 900s",
        last_tool={"name": "terminal", "output": "Error: no issue found matching"},
    ).lower()


def test_briefing_roster_uses_display_handles():
    room = _room(members=["sheila-graphics-and-visual-produ", "editor"], lead="editor")
    names = {"sheila-graphics-and-visual-produ": "Sheila", "editor": "Editor"}
    text = engine.room_briefing(room, "editor", ["Mark"], names)
    assert "You are Editor" in text
    assert "@Sheila (`sheila-graphics-and-visual-produ`)" in text
    assert "@Sheila please make a 16:9 header" in text


# ── store ────────────────────────────────────────────────────────────────


def test_store_roundtrip_and_seq_assignment(tmp_path):
    store = RoomStore(base_dir=str(tmp_path))
    room = _room()
    store.create(room)

    assert store.get("r-1").name == "Test"
    m1 = store.append("r-1", RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="a"))
    m2 = store.append("r-1", RoomMessage(seq=0, ts=0, kind=KIND_AGENT, speaker="scout", text="b"))
    assert (m1.seq, m2.seq) == (1, 2)
    assert [m.text for m in store.read_since("r-1", 0)] == ["a", "b"]
    assert [m.text for m in store.read_since("r-1", 1)] == ["b"]


def test_store_seq_survives_reopen(tmp_path):
    store = RoomStore(base_dir=str(tmp_path))
    store.create(_room())
    store.append("r-1", RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="a"))

    reopened = RoomStore(base_dir=str(tmp_path))
    m2 = reopened.append("r-1", RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="b"))
    assert m2.seq == 2


def test_touch_last_seen_merges_parallel_cursors(tmp_path):
    store = RoomStore(base_dir=str(tmp_path))
    store.create(_room())
    store.touch_last_seen("r-1", "scout", 3)
    store.touch_last_seen("r-1", "editor", 5)
    store.touch_last_seen("r-1", "scout", 2)  # must not go backwards
    seen = store.get("r-1").last_seen
    assert seen["scout"] == 3
    assert seen["editor"] == 5


def test_restore_last_seen_rewinds_only_while_still_tentative(tmp_path):
    """A failed turn may rewind its own cursor, never a later advance."""
    store = RoomStore(base_dir=str(tmp_path))
    store.create(_room())
    store.touch_last_seen("r-1", "scout", 10)
    store.touch_last_seen("r-1", "editor", 8)
    store.restore_last_seen("r-1", "scout", previous=3, tentative=10)
    seen = store.get("r-1").last_seen
    assert seen["scout"] == 3
    assert seen["editor"] == 8
    store.touch_last_seen("r-1", "scout", 12)
    store.restore_last_seen("r-1", "scout", previous=3, tentative=10)
    seen = store.get("r-1").last_seen
    assert seen["scout"] == 12
    assert seen["editor"] == 8


def test_store_update_last_seen_roundtrip(tmp_path):
    store = RoomStore(base_dir=str(tmp_path))
    room = _room()
    store.create(room)
    room.last_seen["scout"] = 7
    store.update(room)
    assert RoomStore(base_dir=str(tmp_path)).get("r-1").last_seen == {"scout": 7}


def test_wait_since_wakes_on_append(tmp_path):
    import threading
    import time as _time

    store = RoomStore(base_dir=str(tmp_path))
    store.create(_room())
    got: list[str] = []

    def waiter():
        got.extend(m.text for m in store.wait_since("r-1", 0, timeout=2.0))

    t = threading.Thread(target=waiter)
    t.start()
    _time.sleep(0.05)
    store.append("r-1", RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hello"))
    t.join(3)
    assert got == ["hello"]


def test_wait_since_timeout_empty(tmp_path):
    import time as _time

    store = RoomStore(base_dir=str(tmp_path))
    store.create(_room())
    t0 = _time.time()
    assert store.wait_since("r-1", 0, timeout=0.2) == []
    assert _time.time() - t0 >= 0.15


def test_sse_stream_emits_existing_messages(tmp_path, monkeypatch):
    """GET /rooms/{id}/stream is text/event-stream and emits already-written
    lines immediately (the EventSource catch-up case)."""
    import http.client
    import json
    import threading

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter, _RoomsRequestHandler, _RoomsServer

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    adapter.store.create(_room())
    adapter.store.append(
        "r-1", RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hi")
    )
    httpd = _RoomsServer(("127.0.0.1", 0), _RoomsRequestHandler, adapter)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=3)
        conn.request("GET", "/rooms/r-1/stream?since=0")
        resp = conn.getresponse()
        assert resp.status == 200
        assert "text/event-stream" in (resp.getheader("Content-Type") or "")
        lines: list[str] = []
        while True:
            raw = resp.fp.readline()
            assert raw, "SSE stream closed before a messages event"
            line = raw.decode().rstrip("\n")
            lines.append(line.rstrip("\r"))
            if line in ("\n", "\r\n", "") and any(l.startswith("data:") for l in lines):
                break
        data_line = next(l for l in lines if l.startswith("data:"))
        payload = json.loads(data_line[len("data:") :].strip())
        assert [m["text"] for m in payload["messages"]] == ["hi"]
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_store_delete_and_corrupt_line_tolerance(tmp_path):
    store = RoomStore(base_dir=str(tmp_path))
    store.create(_room())
    store.append("r-1", RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="a"))
    # Torn write: a corrupt line must not take the room down.
    with open(tmp_path / "r-1.transcript.jsonl", "a", encoding="utf-8") as f:
        f.write("{not json\n")
    assert [m.text for m in store.read_since("r-1", 0)] == ["a"]
    assert store.delete("r-1") is True
    assert store.get("r-1") is None


def test_pass_reply_tolerates_a_markdown_fence():
    """Models routinely wrap JSON in a code fence despite instructions.
    A fenced payload is still exactly the payload; fenced JSON plus prose,
    or a fence containing anything else, is speech."""
    fenced = "```json\n{\"pass\": true}\n```"
    bare_fence = "```\n{\"pass\": true}\n```"
    assert engine.is_pass_reply(fenced)
    assert engine.is_pass_reply(bare_fence)
    assert not engine.is_pass_reply("I'll pass.\n```json\n{\"pass\": true}\n```")
    assert not engine.is_pass_reply("```json\n{\"pass\": true}\n```\nSee above.")
    assert not engine.is_pass_reply("```json\n{\"other\": 1}\n```")


def test_is_directed_recognises_who_the_user_named():
    members = ["scout", "editor", "critic"]
    assert engine.is_directed("@editor can you start?", members) is True
    assert engine.is_directed("@room status?", members) is True
    assert engine.is_directed("where are we?", members) is False
    # A mention that names nobody in the room does not direct the message.
    assert engine.is_directed("@nobody hello", members) is False
    # Mentions inside a fence are not live.
    assert engine.is_directed("```\n@editor\n```", members) is False
