"""Cheap source tripwires around the scheduled-jobs UI suite, and the wiring
that makes it execute.

The substring checks here are tripwires, not coverage. `test/cron.ui.test.tsx`
is the coverage — and until #180 nothing in CI ran it, which is why these
tripwires had grown to stand in for it.
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest
import yaml


ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)


def _read(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def test_api_declares_complete_cron_surface():
    source = _read("retinue-web/src/api.ts")
    assert "interface CronJobRow" in source
    assert "last_run_at" in source and "next_run_at" in source
    assert "registration_error" in source
    assert "owner_profile" not in source
    for name in (
        "listCronJobs", "listRoomCronJobs", "createCronJob", "patchCronJob",
        "pauseCronJob", "resumeCronJob", "runCronJob", "deleteCronJob",
    ):
        assert name in source
    assert "/cron/jobs" in source


def test_actions_are_table_driven_through_one_dispatch_site():
    source = _read("retinue-web/src/cron.tsx")
    actions = source[source.index("CRON_ACTIONS") : source.index("export function actionsForJob")]
    for key, call in (
        ("pause", "api.pauseCronJob("), ("resume", "api.resumeCronJob("),
        ("run", "api.runCronJob("), ("delete", "api.deleteCronJob("),
    ):
        start = actions.index(f'key: "{key}"')
        following = actions.find('key: "', start + 8)
        entry = actions[start : following if following >= 0 else len(actions)]
        assert call in entry
        if key == "delete":
            assert "confirm:" in entry
    assert source.count("await action.call(") == 1
    dispatch = source.index("await action.call(")
    assert "props.onChanged()" in source[dispatch : dispatch + 400]


def test_cron_components_are_hook_free_and_keep_stable_testids():
    source = _read("retinue-web/src/cron.tsx")
    for hook in ("useState(", "useEffect(", "useMemo(", "useCallback(", "useRef("):
        assert hook not in source
    testids = (
        "cron-section", "cron-row", "cron-owner", "cron-name", "cron-schedule",
        "cron-state", "cron-kind", "cron-room", "cron-next-run", "cron-last-run",
        "cron-error", "cron-filter-owner", "cron-filter-room", "cron-new",
        "cron-edit", "cron-action-pause", "cron-action-resume", "cron-action-run",
        "cron-action-delete", "cron-timezone", "cron-form", "cron-form-owner",
        "cron-form-mode", "cron-form-room", "cron-form-submit", "save-routine-form",
        "save-routine-owner", "save-routine-scheduled",
    )
    for testid in testids:
        assert testid in source


def test_submit_preserves_clear_and_roomless_edit_contracts():
    source = _read("retinue-web/src/cron.tsx")
    start = source.index("export async function submitCronJob")
    segment = source[start : start + 1200]
    assert "api.patchCronJob(" in segment and "api.createCronJob(" in segment
    assert "if (f.room)" in segment
    assert "prompt: f.prompt" in segment and "skill: f.skill" in segment
    assert "f.prompt ||" not in segment and "f.skill ||" not in segment
    compact = re.sub(r"\s+", "", source)
    assert "constsubmitDisabled=props.form.jobId===null&&" in compact
    assert "disabled={submitDisabled}" in compact


def test_app_wires_scheduled_and_save_routine_modals():
    source = _read("retinue-web/src/App.tsx")
    for marker in (
        'from "./cron"', "<ScheduledSection", "<CronJobModal", "<SaveRoutineModal",
        "onChanged={", "api.listCronJobs().catch(", '"cron-job"', '"save-routine"',
    ):
        assert marker in source
    assert "rt.messages.length" not in source
    assert "as a routine named:" not in source
    assert source.count("window.prompt(") == 4


def _web_build_run_block() -> str:
    """The shell body of retinue.yml's `web-build` job."""
    workflow = yaml.safe_load(_read(".github/workflows/retinue.yml"))
    steps = workflow["jobs"]["web-build"]["steps"]
    return "\n".join(str(step.get("run") or "") for step in steps)


def test_ci_runs_the_ui_suite_runner_rather_than_naming_entry_points():
    """#180: naming entry points is how a whole suite went unexecuted.

    `test/cron.ui.test.tsx` was never run by CI from the day it was written —
    `retinue-web` is not an npm workspace, so `js-tests.yml` never sees it, and
    the web job listed individual files. Its only "coverage" was the substring
    matching in this module, which cannot fail on a broken assertion.
    """
    run = _web_build_run_block()

    assert "test/run-ui-tests.mjs" in run, (
        "retinue.yml's web-build job must invoke the UI runner, which discovers "
        "every suite, instead of naming entry points that drift out of coverage"
    )
    named = [
        line
        for line in run.splitlines()
        if "--test" in line and not line.lstrip().startswith("#")
    ]
    assert not named, (
        "web-build names test entry points directly: "
        f"{named} — add the suite where the runner walks instead"
    )


def test_ui_runner_walks_both_suite_homes():
    """An empty or half scan must be impossible to ship quietly."""
    runner = _read("retinue-web/test/run-ui-tests.mjs")
    assert "esbuild" in runner and '"--test"' in runner
    assert "readdirSync" in runner and r"\.test\.(ts|tsx)$" in runner
    assert 'join(root, "test")' in runner and 'join(root, "src")' in runner
    assert "throw new Error" in runner


def test_package_manifest_is_unchanged():
    """Fork policy: retinue-web/package.json stays byte-identical to its base."""
    result = subprocess.run(
        ["git", "show", "56516ec7faa075bab1b1c321962bde38ff79f292:retinue-web/package.json"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("base package manifest is unavailable")
    with open(os.path.join(ROOT, "retinue-web/package.json"), "rb") as handle:
        assert handle.read() == result.stdout
