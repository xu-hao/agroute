"""Shared pytest fixtures."""

import pytest


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Prevent ambient user configuration or state environment variables from leaking into tests."""
    monkeypatch.delenv("AGROUTE_STATE_DIR", raising=False)
    monkeypatch.delenv("AGROUTE_USER_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
