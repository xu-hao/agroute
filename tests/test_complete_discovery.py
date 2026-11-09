"""Complete inventories and plan eligibility at public boundaries (issue #19)."""

import json
import os
from pathlib import Path
import time
import tomllib

import pytest

from agroute import AgRouteError, Router
from agroute.cli import main
from agroute.discovery import parse_agy_models_output

EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def catalogs(claude_count=4, codex_count=6, extra_efforts=()):
    declared = ["max", "low", "xhigh", "high", "medium", *extra_efforts, "low"]
    claude = [
        {"id": f"claude-current-{i}", "section": "main", "runtime": {
            "family": "sonnet", "effort_levels": declared, "default_effort": "medium"}}
        for i in range(claude_count)
    ]
    claude.append({"id": "claude-old", "section": "overflow", "runtime": {"effort_levels": EFFORTS}})
    catalog = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "cache/model-catalog/catalog.json"
    catalog.write_text(json.dumps({"catalog": {"config": {"models": claude}}}))
    codex = [
        {"slug": f"gpt-7-current-{i}", "visibility": "list", "priority": i,
         "supported_reasoning_levels": [{"effort": effort} for effort in declared]}
        for i in reversed(range(codex_count))
    ]
    codex.extend([
        {"slug": "gpt-6-old", "visibility": "list", "priority": -1, "supported_reasoning_levels": EFFORTS},
        {"slug": "gpt-8-hidden", "visibility": "hide", "priority": -2, "supported_reasoning_levels": EFFORTS},
    ])
    (Path(os.environ["CODEX_HOME"]) / "models_cache.json").write_text(json.dumps({"models": codex}))
    agy_text = "\n".join(f"gemini-current-{i}-high Model {i} (High)" for i in range(20))
    (Path(os.environ["AGROUTE_SPOOL_DIR"]) / "agy_models_cache.json").write_text(json.dumps({
        "fetched_at": time.time(), "candidates": parse_agy_models_output(agy_text)}))
    return agy_text


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_models_emits_all_current_models_and_efforts(tmp_path, agent):
    catalogs(claude_count=6, extra_efforts=("ultra", "custom"))
    models = Router(project=tmp_path).models()["agents"][agent]["models"]
    prefix = "claude-current" if agent == "claude" else "gpt-7-current"
    assert list(models) == [f"{prefix}-{i}" for i in range(6)]
    assert all(model["efforts"] == EFFORTS + ["ultra", "custom"] for model in models.values())


def test_agy_parser_retains_every_listed_model():
    raw = catalogs()
    assert len(parse_agy_models_output(raw)) == 20


def test_cli_emits_twenty_claude_cells_and_complete_cross_agent_inventory(tmp_path, capsys):
    catalogs()
    output = tmp_path / "candidates.json"
    common = ["--host", "claude", "--no-user-config", "--state-dir", str(tmp_path / "state")]
    assert main(["candidates", *common, "--include-agents", "claude", "--output", str(output), "--json"]) == 0
    claude = json.loads(capsys.readouterr().out)["candidates"]
    assert claude == json.loads(output.read_text())
    assert len(claude) == 20
    assert [c["effort"] for c in claude[:5]] == EFFORTS
    assert main(["candidates", *common, "--output", str(output), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["candidates"]) == 70
    assert payload["excluded"] == []
    assert payload["candidates"] == json.loads(output.read_text())
    assert main(["inventory", *common]) == 0
    assert tomllib.loads(capsys.readouterr().out)["inventories"]["router"] == payload["candidates"]


@pytest.mark.parametrize("effort", ["low", "max"])
def test_plan_accepts_full_discovered_pool_and_refuses_unknown_effort(tmp_path, effort):
    catalogs()
    router = Router(project=tmp_path)
    decision = {"decision_id": f"all-efforts-{effort}", "chosen": {
        "model": "claude/claude-current-0", "effort": effort}}
    plan = router.plan("claude", decision, prompt="Review a parser")
    assert plan["invocation"]["parameters"]["effort"] == effort
    decision["chosen"]["effort"] = "undiscovered"
    with pytest.raises(AgRouteError, match="not an eligible route"):
        router.plan("claude", decision)


def test_explicit_inventory_overrides_discovery_without_size_limit(tmp_path):
    catalogs()
    cfg = tmp_path / "agroute.toml"
    cells = ",\n".join(f'{{ model = "custom-{i}", effort = "high" }}' for i in range(40))
    cfg.write_text(f"[agents.claude]\nnative_candidates = [\n{cells}\n]\ncli_candidates = [\n{cells}\n]\n")
    router = Router(cfg, project=tmp_path)
    for host in ("claude", "codex"):
        result = router.candidates(host, include_agents=["claude"])
        assert len(result["candidates"]) == 40
        assert result["candidates"][-1] == {"model": "claude/custom-39", "effort": "high"}


def test_quota_exclusions_remain_visible_for_complete_inventory(tmp_path):
    catalogs()
    router = Router(project=tmp_path)
    router.usage_set("claude", tier="sonnet", tier_used_pct=99)
    result = router.candidates("claude")
    assert len(result["candidates"]) == 50
    assert len(result["excluded"]) == 20
    assert all(entry["reason"] for entry in result["excluded"])
