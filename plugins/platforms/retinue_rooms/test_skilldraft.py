"""Generated routine skill drafts obey the runtime authoring contract."""

from __future__ import annotations

import os
import re

import pytest

from . import cronjobs, skilldraft


def _home(tmp_path, monkeypatch):
    profile = tmp_path / "profiles" / "sally"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cronjobs, "_gateway_config", lambda: object())
    monkeypatch.setattr(cronjobs, "_served_pairs", lambda _cfg: [("sally", str(profile))])
    return profile


def test_write_skill_draft_is_per_retainer(tmp_path, monkeypatch):
    profile = _home(tmp_path, monkeypatch)
    result = skilldraft.write_skill_draft(
        str(tmp_path), "sally", slug="daily-brief", name="Daily brief",
        steps=["Collect updates", "Summarize them"], expected_output="A short brief",
        source_room="room-a",
    )
    path = profile / "skills" / "daily-brief" / "SKILL.md"
    assert result["path"] == str(path)
    assert path.is_file() and not path.is_symlink()
    assert not (tmp_path / "skills" / "daily-brief" / "SKILL.md").exists()
    with pytest.raises(FileExistsError):
        skilldraft.write_skill_draft(
            str(tmp_path), "sally", slug="daily-brief", name="Daily brief",
            steps=["x"], expected_output="", source_room="room-a",
        )


def test_write_skill_draft_rejects_traversal(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        skilldraft.write_skill_draft(
            str(tmp_path), "sally", slug="../escape", name="Escape",
            steps=["x"], expected_output="", source_room="room-a",
        )
    assert not (tmp_path / "escape").exists()


def test_generated_skill_meets_the_authoring_contract():
    slug = "morning-brief"
    rendered = skilldraft.render_skill_draft(
        slug=slug,
        name="N" * 200 + "\nignored",
        steps=[f"step {index}" for index in range(12)],
        expected_output="result " * 300,
        source_room="r" * 64,
    )
    description = next(
        line.removeprefix("description:").strip()
        for line in rendered.splitlines()
        if line.startswith("description:")
    )
    assert len(description) <= 60 and description.endswith(".")
    assert slug not in description
    assert not {"powerful", "comprehensive", "seamless", "advanced"}.intersection(
        description.lower().split()
    )
    for key in ("name:", "description:", "version:", "author:", "license:", "metadata:"):
        assert key in rendered
    assert len(slug) <= 48
    headings = [line[3:].strip() for line in rendered.splitlines() if line.startswith("## ")]
    assert headings == [
        "When to Use", "Prerequisites", "How to Run", "Quick Reference",
        "Procedure", "Pitfalls", "Verification",
    ]
    title = next(line for line in rendered.splitlines() if line.startswith("# "))
    assert title.endswith(" Skill")
    assert not re.search(r"[\x00-\x1f\x7f]", title)
    fixed = rendered.split("## Procedure", 1)[0]
    assert not any(token in fixed for token in ("grep", "cat ", "sed ", "awk ", "find "))
