"""Tests for scripts/ci/evaluate_required_checks.py.

The gate branch protection trusts. Every case here is one the old inline
evaluator got wrong or could not be asked about (#176).
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "ci"
    / "evaluate_required_checks.py"
)
_spec = importlib.util.spec_from_file_location("evaluate_required_checks", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load evaluate_required_checks.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["evaluate_required_checks"] = _mod
_spec.loader.exec_module(_mod)


def _needs(**results: str) -> dict[str, dict]:
    return {name: {"result": result} for name, result in results.items()}


def _run(needs: dict[str, dict], monkeypatch, tmp_path) -> tuple[int, str]:
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(needs)))
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    code = _mod.main([])
    return code, captured.getvalue()


def test_success_and_skipped_pass(monkeypatch, tmp_path):
    code, out = _run(_needs(tests="success", lint="skipped"), monkeypatch, tmp_path)

    assert code == 0
    assert "All checks passed" in out


def test_failure_blocks(monkeypatch, tmp_path):
    code, out = _run(_needs(tests="failure", lint="success"), monkeypatch, tmp_path)

    assert code == 1
    assert "tests (failure)" in out


def test_cancelled_blocks(monkeypatch, tmp_path):
    """A cancelled job used to render ❌ and still exit 0.

    Reclaimed runners, a cancelled called-workflow, and the concurrency group's
    own cancel-in-progress all land here. None of them ran the tests, so none
    of them is evidence the tests pass.
    """
    code, out = _run(_needs(tests="cancelled", lint="success"), monkeypatch, tmp_path)

    assert code == 1
    assert "tests (cancelled)" in out


@pytest.mark.parametrize("result", ["", None, "timed_out", "neutral", "action_required"])
def test_unrecognized_result_blocks(result, monkeypatch, tmp_path):
    """An unknown conclusion is not evidence of success."""
    code, _ = _run({"tests": {"result": result}}, monkeypatch, tmp_path)

    assert code == 1


def test_missing_result_key_blocks(monkeypatch, tmp_path):
    code, _ = _run({"tests": {}}, monkeypatch, tmp_path)

    assert code == 1


def test_every_blocking_job_is_named(monkeypatch, tmp_path):
    code, out = _run(
        _needs(tests="failure", lint="cancelled", docs="success"),
        monkeypatch,
        tmp_path,
    )

    assert code == 1
    assert "2 job(s) did not pass" in out
    assert "tests (failure)" in out
    assert "lint (cancelled)" in out
    assert "docs" not in out.split("::error::")[1]


def test_needs_json_output_is_written_even_when_blocking(monkeypatch, tmp_path):
    """The comment assembler reads this; a red run is when it matters most."""
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps(_needs(tests="failure", lint="success")))
    )
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    assert _mod.main([]) == 1

    written = output.read_text(encoding="utf-8")
    assert written.startswith("needs-json=")
    assert json.loads(written.split("=", 1)[1]) == {
        "tests": "failure",
        "lint": "success",
    }


def test_no_upstream_jobs_passes(monkeypatch, tmp_path):
    """`needs` is never empty in ci.yaml, but an empty set is not a failure."""
    code, _ = _run({}, monkeypatch, tmp_path)

    assert code == 0
