"""Tests for GitHub workflow invariants."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _load_workflow(name: str) -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text())
    return workflow


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML parses the bare `on:` key as boolean True.
    return workflow.get("on", workflow.get(True, {}))


def test_release_deploy_is_gated_on_tests() -> None:
    """The PyPI publish job must not run unless the full test suite passed."""
    jobs = _load_workflow("release.yml")["jobs"]
    assert jobs["test"]["uses"] == "./.github/workflows/pytest.yml"
    needs = jobs["deploy"]["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert "test" in needs


def test_pytest_workflow_is_callable_from_release() -> None:
    """The test workflow must stay reusable so the release gate can call it."""
    assert "workflow_call" in _triggers(_load_workflow("pytest.yml"))
