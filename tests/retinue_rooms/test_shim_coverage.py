"""Anti-drift meta-test for issue #145 — every rooms test module must have a shim here.

The real rooms suite lives at `plugins/platforms/retinue_rooms/test_*.py`.
`pyproject.toml`'s `testpaths = ["tests"]` is upstream and must not be edited
(fork policy), so each of those modules needs a thin collector shim in this
directory or it is invisible to anything running via `tests/`. Between the
original shim batch (issue #6) and the #135 sync, 13 modules drifted out of
coverage unnoticed.

This module is NOT a shim. It derives the expected shim set dynamically from
whatever `test_*.py` files actually exist in the plugin directory, so a newly
added rooms test module fails here on the next run instead of silently going
uncollected. Never replace this with a hardcoded list — a hardcoded list is
exactly the thing that drifted.

Note on placement: the shims deliberately live at `tests/retinue_rooms/`, not
`tests/plugins/`. A shim package named `plugins` under `tests/` can shadow the
real top-level `plugins` package once `tests/` lands on `sys.path`.
"""

from __future__ import annotations

from pathlib import Path

SHIM_DIR = Path(__file__).resolve().parent
REPO_ROOT = SHIM_DIR.parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins" / "platforms" / "retinue_rooms"

#: Files in this directory that are not shims for a plugin test module.
NON_SHIM_MODULES = frozenset({Path(__file__).name})


def _plugin_test_modules() -> set[str]:
    return {p.name for p in PLUGIN_DIR.glob("test_*.py")}


def _shim_modules() -> set[str]:
    return {p.name for p in SHIM_DIR.glob("test_*.py")} - NON_SHIM_MODULES


def test_plugin_suite_is_discoverable() -> None:
    """Guard the path derivation — an empty scan must fail, not vacuously pass."""
    assert PLUGIN_DIR.is_dir(), f"rooms plugin dir not found at {PLUGIN_DIR}"
    assert _plugin_test_modules(), f"no test_*.py modules found under {PLUGIN_DIR}"


def test_every_rooms_test_module_has_a_shim() -> None:
    missing = sorted(_plugin_test_modules() - _shim_modules())
    assert not missing, (
        f"{len(missing)} rooms test module(s) have no collector shim in {SHIM_DIR} "
        f"and are invisible to a `tests/` run: {', '.join(missing)}. "
        "Add one shim per module (see any existing shim for the exact shape)."
    )


def test_no_orphan_shims() -> None:
    orphans = sorted(_shim_modules() - _plugin_test_modules())
    assert not orphans, (
        f"shim(s) in {SHIM_DIR} with no matching module in {PLUGIN_DIR}: "
        f"{', '.join(orphans)} — the real module was renamed or removed."
    )


def test_every_shim_reexports_its_own_module() -> None:
    """A shim that exists but re-exports the wrong module is still a coverage hole."""
    wrong = []
    for name in sorted(_shim_modules()):
        expected = f"from plugins.platforms.retinue_rooms.{Path(name).stem} import *"
        if expected not in (SHIM_DIR / name).read_text(encoding="utf-8"):
            wrong.append(name)
    assert not wrong, (
        "shim(s) do not re-export their own plugin module "
        f"(expected `from plugins.platforms.retinue_rooms.<stem> import *`): {', '.join(wrong)}"
    )
