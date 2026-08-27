"""LIVE Grok Build acceptance: a real multi-step tool task (#218).

Gated behind ``RETINUE_GROKBUILD_LIVE=1`` because it needs the grok binary,
a logged-in token store, and a few minutes of wall clock. CI never runs it;
it exists so the claim "Grok Build owns the loop" can be re-verified on the
host whenever grok is updated.

What it proves — the acceptance criterion for the runtime:

* ONE ``session/prompt`` produces MANY tool operations (read → run test →
  edit → re-run), i.e. the agent loop runs inside Grok Build, with Retinue
  only observing tool lifecycle events and answering permission requests.
* The workspace-scoped approval policy allows the project-local work.
* The final message arrives as the turn's reply and the repo is actually
  fixed (the test file passes when WE re-run it).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap

import pytest

from . import grokbuild
from .grokbuild import APPROVAL_WORKSPACE, GrokBuildManager

pytestmark = pytest.mark.skipif(
    os.getenv("RETINUE_GROKBUILD_LIVE") != "1",
    reason="live Grok Build acceptance test; set RETINUE_GROKBUILD_LIVE=1",
)

_CALC = textwrap.dedent(
    """\
    def add(a, b):
        return a - b  # BUG: should add
    """
)

_TEST = textwrap.dedent(
    """\
    import sys
    from calc import add

    if add(2, 3) != 5 or add(-1, 1) != 0:
        print("FAIL")
        sys.exit(1)
    print("PASS")
    """
)


def test_grok_build_owns_a_multi_step_tool_loop(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "calc.py").write_text(_CALC, encoding="utf-8")
    (ws / "test_calc.py").write_text(_TEST, encoding="utf-8")

    manager = GrokBuildManager(str(home))
    activity: list[tuple[str, str, str]] = []

    prompt = (
        "You are fixing a tiny project in your working directory. "
        "Read calc.py and test_calc.py, run `python3 test_calc.py` to see "
        "the failure, fix the bug in calc.py, re-run the test, and keep "
        "going until it prints PASS. Then reply with exactly one line: "
        "FIXED: <what was wrong>."
    )

    async def go():
        try:
            return await manager.run_turn(
                "live-room",
                "fixer",
                str(ws),
                build_prompt=lambda fresh: prompt,
                approval=APPROVAL_WORKSPACE,
                timeout=600,
                on_activity=lambda ev, p: activity.append(
                    (ev, p.get("kind") or "", p.get("title") or "")
                ),
            )
        finally:
            await manager.shutdown()

    result = asyncio.new_event_loop().run_until_complete(go())

    # Visible with -s: the observed tool sequence + the reply, so a rerun
    # against a new grok version leaves a diagnosable record.
    print("\nGROK BUILD ACTIVITY:")
    for ev, kind, title in activity:
        print(f"  {ev:<12} {kind:<8} {title}")
    print(f"REPLY: {result.text.strip()[:200]}")
    print(f"USAGE: {result.usage}")

    assert result.stop_reason == "end_turn", result
    assert "FIXED" in result.text.upper(), result.text

    # The loop ran inside Grok Build: one prompt, several tool operations
    # of more than one kind (reads + executes and/or edits).
    starts = [a for a in activity if a[0] == "tool_start"]
    kinds = {a[1] for a in starts}
    assert len(starts) >= 3, activity
    assert len(kinds & {"read", "execute", "edit", "write"}) >= 2, activity

    # Nothing was rejected (all work was project-scoped) …
    assert not [a for a in activity if a[0] == "rejected"], activity

    # … and the fix is real: OUR rerun of the test passes.
    proc = subprocess.run(
        [sys.executable, "test_calc.py"], cwd=str(ws), capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS" in proc.stdout
