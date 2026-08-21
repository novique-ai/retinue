#!/usr/bin/env python3
"""Decide whether the ``All required checks pass`` gate in ci.yaml passes.

Reads ``toJSON(needs)`` on stdin — ``{job_name: {"result": ..., ...}}`` — and
exits non-zero if any upstream job did not reach an acceptable conclusion.
Also writes ``needs-json``, a compact ``{job_name: result}`` dict, to
``$GITHUB_OUTPUT`` for the live comment poller.

Why this is a script and not a heredoc in the workflow: it is the single
decision that branch protection trusts, and inline it could not be tested. The
gate had been silently permissive — see ``PASSING`` below — for exactly as long
as nobody could write a test for it (#176).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Iterable

# Conclusions that do NOT block a merge.
#
# ``skipped`` is deliberate: most lanes in ci.yaml are gated on the change
# classifier, so a docs-only PR legitimately skips the Python lanes.
#
# Everything else blocks — and that is the fix. The previous evaluator failed
# only on the literal string ``failure``, so a ``cancelled`` job (a runner
# reclaimed mid-run, a called workflow cancelled, an in-flight cancel from the
# concurrency group) printed a ❌ next to its name and then exited 0. The gate
# reported success with a red job listed in its own log. Same for an empty or
# unrecognized result: an unknown conclusion is not evidence of success, and a
# required gate must not infer one.
PASSING = frozenset({"success", "skipped"})


def evaluate(needs: dict[str, dict]) -> tuple[list[str], list[tuple[str, str]]]:
    """Return ``(blocking, rendered)`` for a ``needs`` context.

    ``blocking`` names every job whose result is not in ``PASSING``.
    ``rendered`` is ``(name, result)`` sorted for display.
    """
    rendered = sorted(
        (name, str(info.get("result") or "")) for name, info in needs.items()
    )
    blocking = [name for name, result in rendered if result not in PASSING]
    return blocking, rendered


def compact(needs: dict[str, dict]) -> dict[str, str]:
    """``{job_name: result}`` for the comment assembler."""
    return {name: str(info.get("result") or "") for name, info in needs.items()}


def _write_output(pairs: Iterable[tuple[str, str]]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in pairs:
            handle.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    needs = json.load(sys.stdin)
    blocking, rendered = evaluate(needs)

    _write_output([("needs-json", json.dumps(compact(needs)))])

    for name, result in rendered:
        icon = "✅" if result in PASSING else "❌"
        print(f"{icon} {name}: {result or '(no result)'}")

    if blocking:
        detail = ", ".join(
            f"{name} ({result or 'no result'})"
            for name, result in rendered
            if name in set(blocking)
        )
        print(f"::error::{len(blocking)} job(s) did not pass: {detail}")
        return 1

    print("All checks passed (or were skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
