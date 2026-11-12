"""Shared pytest fixtures."""

import json
import time
import pytest


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path, request):
    """Prevent ambient user configuration, state, or live host model caches from leaking into tests."""
    from agroute.adapters import get_adapter

    # Routing tests outside the refresh contract use a synthetic provider response;
    # refresh tests supply their own doubles and must never call an ambient backend.
    def synthetic_probe(cwd):
        return {"session_5h": {"used_pct": 0, "reset_at": None}}

    def unexpected_probe(cwd):
        raise AssertionError("test did not install a provider double")

    probe = unexpected_probe if request.path.name == "test_auto_quota_refresh.py" else synthetic_probe
    from agroute import price_refresh

    def unexpected_price_fetch(url, timeout):
        raise AssertionError("test did not install a price-source double")

    monkeypatch.setattr(price_refresh, "_http_get", unexpected_price_fetch)
    for agent in ("codex", "claude", "agy"):
        monkeypatch.setattr(get_adapter(agent), "probe_usage", probe)

    work_cwd = tmp_path / "cwd"
    work_cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(work_cwd)
    monkeypatch.delenv("AGROUTE_STATE_DIR", raising=False)
    monkeypatch.delenv("AGROUTE_USER_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5.4",
                        "visibility": "list",
                        "priority": 1,
                        "default_reasoning_level": "medium",
                        "supported_reasoning_levels": [{"effort": "medium"}, {"effort": "high"}],
                    },
                    {
                        "slug": "gpt-5.4-mini",
                        "visibility": "list",
                        "priority": 2,
                        "default_reasoning_level": "low",
                        "supported_reasoning_levels": [{"effort": "low"}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    claude_home = tmp_path / "claude-home"
    catalog_dir = claude_home / "cache" / "model-catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    (catalog_dir / "catalog.json").write_text(
        json.dumps(
            {
                "version": 2,
                "catalog": {
                    "config": {
                        "models": [
                            {
                                "id": "claude-opus-4-6",
                                "short_name": "Opus",
                                "section": "main",
                                "quick_select": True,
                                "runtime": {
                                    "effort_levels": ["high"],
                                    "default_effort": "high",
                                },
                            },
                            {
                                "id": "claude-sonnet-4-6",
                                "short_name": "Sonnet",
                                "section": "main",
                                "quick_select": True,
                                "runtime": {
                                    "effort_levels": ["medium", "high"],
                                    "default_effort": "medium",
                                },
                            },
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))

    spool_dir = tmp_path / "agroute-spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    (spool_dir / "agy_models_cache.json").write_text(
        json.dumps(
            {
                "format": "agroute-agy-models-cache-v1",
                "fetched_at": time.time(),
                "candidates": [
                    {"model": "gemini-2.5-pro", "effort": "high", "tier": "pro"},
                    {"model": "gemini-2.5-flash", "effort": "medium", "tier": "flash"},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGROUTE_SPOOL_DIR", str(spool_dir))
