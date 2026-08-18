"""Cheap source tripwires around the executed scheduled-jobs UI suite."""

from __future__ import annotations

import os
import re
import subprocess

import pytest


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


def test_ui_runner_exists_and_package_manifest_is_unchanged():
    runner = _read("retinue-web/test/run-ui-tests.mjs")
    suite = _read("retinue-web/test/cron.ui.test.tsx")
    assert "esbuild" in runner and '"--test"' in runner and "cron.ui.test.tsx" in runner
    for marker in (
        "node:test", "node:assert", "../src/cron", "fetch", "cron-last-run",
        "cron-next-run", "cron-filter-owner", "cron-action-delete",
        "submitSaveRoutine", "formFromRow", "roomless", "prompt", "skill",
        "cron-form-submit",
    ):
        assert marker in suite
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
