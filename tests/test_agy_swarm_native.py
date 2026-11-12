"""Antigravity native delegation through the `swarm` tool for concrete model/effort cells (#33)."""

import json
import os
from pathlib import Path
import time

import pytest

from agroute import AgRouteError, Router
from agroute.adapters import get_adapter
from agroute.cli import main
from agroute.discovery import parse_agy_models_output

NOW = "2026-11-12T12:00:00Z"

# Verbatim `agy models` listing from a host with Antigravity installed.
AGY_MODELS = """Fetching available models...
gemini-4-carbon-medium\tGemini 4 Carbon (Medium)
gemini-4-carbon-high\tGemini 4 Carbon (High)
gemini-4-carbon-low\tGemini 4 Carbon (Low)
gemini-4-argon-medium\tGemini 4 Argon (Medium)
gemini-4-argon-high\tGemini 4 Argon (High)
gemini-4-argon-low\tGemini 4 Argon (Low)
gemini-3.8-flash-high\tGemini 3.8 Flash (High)
gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)
gemini-3.8-flash-low\tGemini 3.8 Flash (Low)
claude-opus-5-5-low\tClaude Opus 5.5 (Low)
claude-opus-5-5-medium\tClaude Opus 5.5 (Medium)
claude-opus-5-5-high\tClaude Opus 5.5 (High)
claude-sonnet-5-5-low\tClaude Sonnet 5.5 (Low)
claude-sonnet-5-5-medium\tClaude Sonnet 5.5 (Medium)
claude-sonnet-5-5-high\tClaude Sonnet 5.5 (High)
gpt-oss-120b-medium\tGPT-OSS 120B (Medium)
"""

SLUGS = [line.split("\t")[0] for line in AGY_MODELS.splitlines()[1:]]


@pytest.fixture
def agy_models():
    (Path(os.environ["AGROUTE_SPOOL_DIR"]) / "agy_models_cache.json").write_text(json.dumps({
        "format": "agroute-agy-models-cache-v1", "fetched_at": time.time(),
        "candidates": parse_agy_models_output(AGY_MODELS)}), encoding="utf-8")


def agy_host_candidates(tmp_path, capsys, *extra):
    output = tmp_path / "candidates.json"
    argv = ["candidates", "--host", "agy", "--include-agents", "agy", "--no-user-config",
            "--state-dir", str(tmp_path / "state"), "--now", NOW, "--output", str(output), "--json", *extra]
    assert main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    return {cell["model"]: cell for cell in payload["candidates"]}


def test_agy_host_candidates_are_the_concrete_agy_models_cells(tmp_path, capsys, agy_models):
    cells = agy_host_candidates(tmp_path, capsys)
    assert set(cells) == {f"agy/{slug}" for slug in SLUGS}
    for slug in SLUGS:
        assert cells[f"agy/{slug}"]["effort"] == slug.rsplit("-", 1)[1]
    for tier in ("pro", "flash", "flash_lite"):
        assert f"agy/{tier}" not in cells
    for effort in ("low", "medium", "high"):
        card = cells[f"agy/gemini-4-carbon-{effort}"]["rate_card"]
        assert (card["provider"], card["model"]) == ("google", f"agy/gemini-4-carbon-{effort}")
        assert card["rates"]["input"] == pytest.approx(2.00)
        assert card["rates"]["output"] == pytest.approx(10.00)
        assert card["rates"]["cache_read"] == pytest.approx(0.10)
        assert not card["source"].startswith("guess:")
        card = cells[f"agy/claude-opus-5-5-{effort}"]["rate_card"]
        assert (card["provider"], card["rates"]["input"]) == ("anthropic", 4)


def test_agy_host_candidates_match_the_cli_surface(tmp_path, capsys, agy_models):
    native = agy_host_candidates(tmp_path, capsys)
    output = tmp_path / "cli.json"
    assert main(["candidates", "--host", "claude", "--include-agents", "agy", "--no-user-config",
                 "--state-dir", str(tmp_path / "state"), "--now", NOW, "--output", str(output), "--json"]) == 0
    cli = {cell["model"]: cell for cell in json.loads(capsys.readouterr().out)["candidates"]}
    assert native == cli


def plan(tmp_path, model, effort, *, config=None, role="Parser Builder", prompt="implement parser", workspace="inherit"):
    router = Router(config, project=tmp_path, state_directory=tmp_path / "state", include_user_config=False)
    return router.plan(
        "agy",
        {"decision_id": "dec-swarm", "chosen": {"model": model, "effort": effort},
         "cap_met": True, "needs_review": False},
        prompt=prompt,
        role=role,
        workspace=workspace,
    )


def test_concrete_slug_plans_a_swarm_worker(tmp_path, agy_models):
    result = plan(tmp_path, "agy/claude-opus-5-5-high", "high", workspace="share")
    assert result["execution_mode"] == "native"
    assert result["invocation"] == {
        "kind": "native_tool",
        "tool_name": "swarm",
        "parameters": {
            "action": "add",
            "name": "parser-builder",
            "model": "claude-opus-5-5-high",
            "workspace": "share",
            "brief": "implement parser",
        },
    }
    assert result["fallback"]["argv"][:7] == [
        "agy", "--output-format", "stream-json", "--model", "claude-opus-5-5-high", "--effort", "high",
    ]


def test_carbon_slug_plans_a_swarm_worker(tmp_path, agy_models):
    result = plan(tmp_path, "agy/gemini-4-carbon-medium", "medium", role="Doc Writer")
    assert result["invocation"]["tool_name"] == "swarm"
    assert result["invocation"]["parameters"]["model"] == "gemini-4-carbon-medium"
    assert result["invocation"]["parameters"]["name"] == "doc-writer"


@pytest.mark.parametrize("tier", ["pro", "flash", "flash_lite", "inherit"])
def test_configured_legacy_tier_plans_invoke_subagent(tmp_path, tier):
    config = tmp_path / "agroute.toml"
    config.write_text(f'[[agents.agy.native_candidates]]\nmodel = "{tier}"\neffort = "standard"\n', encoding="utf-8")
    result = plan(tmp_path, f"agy/{tier}", "standard", config=config)
    assert result["invocation"] == {
        "kind": "native_tool",
        "tool_name": "invoke_subagent",
        "parameters": {
            "TypeName": "self",
            "Model": tier,
            "Role": "Parser Builder",
            "Workspace": "inherit",
            "Prompt": "implement parser",
        },
    }


def test_configured_concrete_native_candidate_is_accepted(tmp_path):
    config = tmp_path / "agroute.toml"
    config.write_text('[[agents.agy.native_candidates]]\nmodel = "gemini-4-carbon-high"\neffort = "high"\n',
                      encoding="utf-8")
    result = plan(tmp_path, "agy/gemini-4-carbon-high", "high", config=config)
    assert result["invocation"]["tool_name"] == "swarm"


def test_configured_native_candidate_rejects_a_non_slug(tmp_path):
    config = tmp_path / "agroute.toml"
    config.write_text('[[agents.agy.native_candidates]]\nmodel = "Gemini Carbon!"\neffort = "high"\n',
                      encoding="utf-8")
    with pytest.raises(AgRouteError, match="Antigravity native"):
        Router(config, project=tmp_path, include_user_config=False)


@pytest.mark.parametrize(
    "role,name",
    [
        ("Parser Builder", "parser-builder"),
        ("  Review / QA  v2 ", "review-qa-v2"),
        ("docs_writer", "docs-writer"),
        ("", "task-worker"),
        ("!!!", "task-worker"),
        ("all", "task-worker"),
        ("Coordinator", "task-worker"),
    ],
)
def test_swarm_worker_name_is_a_clean_roster_slug(role, name):
    invocation = get_adapter("agy").build_native_invocation("gemini-4-argon-high", "high", role=role)
    assert invocation["parameters"]["name"] == name
    assert "brief" not in invocation["parameters"]
