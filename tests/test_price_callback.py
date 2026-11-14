"""Calling-agent offline pricing callback (`price_request` and `agr prices`) (#41)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

import pytest

from agroute import AgRouteError, Router
from agroute.cli import main
from agroute.discovery import parse_agy_models_output

NOW = "2026-11-12T12:00:00Z"


def _write_claude_catalog(entries: list[dict]) -> None:
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "cache/model-catalog/catalog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"catalog": {"config": {"models": entries}}}), encoding="utf-8")


def _write_codex_cache(entries: list[dict]) -> None:
    path = Path(os.environ["CODEX_HOME"]) / "models_cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"models": entries}), encoding="utf-8")


def _write_agy_cache(raw_output: str) -> None:
    path = Path(os.environ["AGROUTE_SPOOL_DIR"]) / "agy_models_cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "format": "agroute-agy-models-cache-v1",
                "fetched_at": time.time(),
                "candidates": parse_agy_models_output(raw_output),
            }
        ),
        encoding="utf-8",
    )


def _seed_mixed_discovery() -> None:
    _write_agy_cache(
        "Fetching available models...\n"
        "gemini-4-hydrogen-high\tGemini 4 Hydrogen (High)\n"
        "gemini-pro-orchestrator-high\tOrchestrator (Carbon)\n"
        "gemini-pro-orchestrator-medium\tOrchestrator (Carbon)\n"
    )
    _write_claude_catalog(
        [
            {
                "id": "claude-opus-5-5",
                "short_name": "Claude Opus 5.5",
                "section": "main",
                "runtime": {"effort_levels": ["high"]},
            },
            {
                "id": "claude-next-6-0",
                "short_name": "Claude Next 6.0",
                "section": "main",
                "runtime": {"effort_levels": ["medium", "high"]},
            },
        ]
    )
    _write_codex_cache(
        [
            {
                "slug": "gpt-7-nova",
                "display_name": "GPT-7 Nova",
                "visibility": "list",
                "priority": 1,
                "supported_reasoning_levels": [{"effort": "high"}],
            }
        ]
    )


def test_price_request_surfaces_unpriced_models_and_display_names(tmp_path, capsys):
    _seed_mixed_discovery()
    state_dir = tmp_path / "state"
    cands_file = tmp_path / "candidates.json"

    # 1. `candidates --json` emits envelope with `price_request`; `--output` writes plain array
    assert (
        main(
            [
                "candidates",
                "--host",
                "agy",
                "--state-dir",
                str(state_dir),
                "--no-user-config",
                "--now",
                NOW,
                "--output",
                str(cands_file),
                "--json",
            ]
        )
        == 0
    )
    cands_env = json.loads(capsys.readouterr().out)
    cands_arr = json.loads(cands_file.read_text(encoding="utf-8"))
    assert cands_env["candidates"] == cands_arr

    # `candidates` without `--json` prints the plain JSON array for `howhard --candidates`
    assert (
        main(
            [
                "candidates",
                "--host",
                "agy",
                "--state-dir",
                str(state_dir),
                "--no-user-config",
                "--now",
                NOW,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == cands_arr

    # `gemini-4-hydrogen` and `claude-opus-5-5` are priced from `_CATALOG`, so only the 3 unknown keys appear
    req = cands_env["price_request"]
    assert req is not None
    assert req["format"] == "agroute-price-request-v1"
    assert req["resolve_command"] == "agr prices resolve --file <path> --json"
    assert "priced" in req["contract"] and "alias" in req["contract"] and "unlisted" in req["contract"]
    by_key = {row["key"]: row for row in req["unpriced_models"]}
    assert set(by_key) == {"claude-next-6-0", "gemini-pro-orchestrator", "gpt-7-nova"}
    assert by_key["gemini-pro-orchestrator"] == {
        "key": "gemini-pro-orchestrator",
        "agents": ["agy"],
        "sample_models": ["agy/gemini-pro-orchestrator-high", "agy/gemini-pro-orchestrator-medium"],
        "display_names": ["Orchestrator (Carbon)"],
    }
    assert by_key["claude-next-6-0"]["display_names"] == ["Claude Next 6.0"]
    assert by_key["gpt-7-nova"]["display_names"] == ["GPT-7 Nova"]

    # 2. `inventory --json` and `agr prices pending --json` emit the same pending keys
    assert (
        main(
            [
                "inventory",
                "--host",
                "agy",
                "--state-dir",
                str(state_dir),
                "--no-user-config",
                "--now",
                NOW,
                "--json",
            ]
        )
        == 0
    )
    inv_env = json.loads(capsys.readouterr().out)
    assert inv_env["price_request"] == req

    assert (
        main(
            [
                "prices",
                "pending",
                "--host",
                "agy",
                "--state-dir",
                str(state_dir),
                "--no-user-config",
                "--now",
                NOW,
                "--json",
            ]
        )
        == 0
    )
    pend_env = json.loads(capsys.readouterr().out)
    assert pend_env["format"] == "agroute-price-pending-v1"
    assert pend_env["pending_count"] == 3
    assert pend_env["price_request"] == req


def test_prices_resolve_supports_priced_alias_and_unlisted_with_ttls(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_mixed_discovery()
    state_dir = tmp_path / "state"
    res_file = tmp_path / "resolutions.json"
    res_file.write_text(
        json.dumps(
            {
                "models": {
                    "claude-next-6-0": {
                        "status": "priced",
                        "provider": "anthropic",
                        "source": "https://platform.claude.com/docs/en/about-claude/pricing",
                        "rates": {
                            "input": 6.0,
                            "output": 30.0,
                            "cache_read": 0.30,
                            "cache_write": 7.5,
                            "cache_write_5m": 7.5,
                            "cache_write_1h": 12.0,
                        },
                    },
                    "gemini-pro-orchestrator": {
                        "status": "alias",
                        "alias_of": "gemini-4-carbon",
                        "reason": "Antigravity orchestrator role backed by Gemini 4 Carbon",
                    },
                    "gpt-7-nova": {
                        "status": "unlisted",
                        "reason": "Internal preview slug with no published API token rates",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "prices",
                "resolve",
                "--file",
                str(res_file),
                "--state-dir",
                str(state_dir),
                "--no-user-config",
                "--now",
                NOW,
                "--json",
            ]
        )
        == 0
    )
    resolve_report = json.loads(capsys.readouterr().out)
    assert resolve_report["format"] == "agroute-price-resolve-v1"
    assert resolve_report["resolved_models"] == ["claude-next-6-0", "gemini-pro-orchestrator", "gpt-7-nova"]
    assert resolve_report["models"]["claude-next-6-0"]["expires_at"] == "2026-12-12T12:00:00Z"
    assert resolve_report["models"]["gemini-pro-orchestrator"]["expires_at"] == "2026-12-12T12:00:00Z"
    assert resolve_report["models"]["gpt-7-nova"]["expires_at"] == "2026-11-19T12:00:00Z"

    snap_disk = json.loads((state_dir / "prices.json").read_text(encoding="utf-8"))
    assert snap_disk["format"] == "agroute-price-snapshot-v2"

    # `prices show --json` inspects active entries
    assert (
        main(
            [
                "prices",
                "show",
                "--state-dir",
                str(state_dir),
                "--no-user-config",
                "--now",
                NOW,
                "--json",
            ]
        )
        == 0
    )
    shown = json.loads(capsys.readouterr().out)
    assert shown["exists"] is True
    assert all(shown["models"][k]["active"] is True for k in shown["models"])

    # `candidates --json` now has `price_request == None` and applies `priced`, `alias`, and `unlisted`
    router = Router(project=tmp_path, state_directory=state_dir, include_user_config=False)
    res = router.candidates("agy", now=NOW)
    assert res["price_request"] is None
    by_model = {(c["model"], c["effort"]): c for c in res["candidates"]}

    next_card = by_model[("claude/claude-next-6-0", "high")]["rate_card"]
    assert next_card["provider"] == "anthropic"
    assert next_card["version"] == f"resolved-{NOW}"
    assert next_card["rates"]["input"] == pytest.approx(6.0)
    assert next_card["rates"]["output"] == pytest.approx(30.0)

    orch_card = by_model[("agy/gemini-pro-orchestrator-high", "high")]["rate_card"]
    assert orch_card["provider"] == "google"
    assert orch_card["version"] == "agroute-guess-2026-11-12"
    assert orch_card["source"] == (
        "guess: Antigravity orchestrator role backed by Gemini 4 Carbon; "
        "priced as gemini-4-carbon from google-cloud-list-2026-11-12"
    )
    assert orch_card["rates"]["input"] == pytest.approx(2.0)
    assert orch_card["rates"]["output"] == pytest.approx(10.0)

    # `unlisted` stays unpriced without re-triggering `price_request` before expiry
    assert "rate_card" not in by_model[("codex/gpt-7-nova", "high")]

    # After 8 days (`2026-11-20`), 7-day `unlisted` expires and re-prompts; 30-day `priced`/`alias` stay active
    later_8d = "2026-11-20T12:00:01Z"
    res_8d = router.candidates("agy", now=later_8d)
    req_8d = res_8d["price_request"]
    assert req_8d is not None
    assert [m["key"] for m in req_8d["unpriced_models"]] == ["gpt-7-nova"]
    assert req_8d["unpriced_models"][0]["expired_at"] == "2026-11-19T12:00:00Z"

    # On `2026-12-02`, the `gemini-pro-orchestrator -> gemini-4-carbon` alias automatically tracks Carbon's scheduled price change
    res_dec = router.candidates("agy", include_agents=["agy"], now="2026-12-02T00:00:00Z")
    dec_cells = {c["model"]: c for c in res_dec["candidates"]}
    assert dec_cells["agy/gemini-pro-orchestrator-high"]["rate_card"]["rates"]["input"] == pytest.approx(4.0)

    # After 31 days (`2026-12-13`), all 3 entries have expired and re-appear in `price_request`
    res_31d = router.candidates("agy", now="2026-12-13T12:00:01Z")
    assert [m["key"] for m in res_31d["price_request"]["unpriced_models"]] == [
        "claude-next-6-0",
        "gemini-pro-orchestrator",
        "gpt-7-nova",
    ]


def test_resolved_price_precedence_against_builtin_and_user_config(tmp_path):
    _write_agy_cache("Fetching available models...\ngemini-4-argon-high\tGemini 4 Argon (High)\n")
    state_dir = tmp_path / "state"
    router = Router(project=tmp_path, state_directory=state_dir, include_user_config=False)

    # Resolve `gemini-4-argon` on 2026-11-20 (newer than 2026-11-12 builtin, older than 2026-12-01 scheduled change)
    router.prices_resolve(
        {
            "models": {
                "gemini-4-argon": {
                    "status": "priced",
                    "provider": "google",
                    "source": "https://cloud.google.com/vertex-ai/generative-ai/pricing",
                    "resolved_at": "2026-11-20T00:00:00Z",
                    "expires_at": "2026-12-20T00:00:00Z",
                    "rates": {
                        "input": 2.5,
                        "output": 12.5,
                        "cache_read": 0.125,
                        "cache_write": 2.5,
                        "cache_write_5m": 2.5,
                        "cache_write_1h": 2.5,
                    },
                }
            }
        },
        now="2026-11-20T00:00:00Z",
    )

    # On 2026-11-25, the resolved snapshot (`2026-11-20`) wins over the `2026-11-12` built-in period
    c_nov = router.candidates("agy", include_agents=["agy"], now="2026-11-25T00:00:00Z")["candidates"][0]
    assert c_nov["rate_card"]["rates"]["input"] == pytest.approx(2.5)
    assert c_nov["rate_card"]["version"] == "resolved-2026-11-20T00:00:00Z"

    # User `[agents.agy.prices]` beats both on the same timestamp
    cfg_path = tmp_path / "agroute.toml"
    cfg_path.write_text(
        '[agents.agy.prices."gemini-4-argon-high"]\n'
        "input = 1.25\noutput = 6.0\ncache_read = 0.05\n"
        "cache_write = 1.25\ncache_write_5m = 1.25\ncache_write_1h = 1.25\n",
        encoding="utf-8",
    )
    router_cfg = Router(cfg_path, project=tmp_path, state_directory=state_dir, include_user_config=False)
    c_user = router_cfg.candidates("agy", include_agents=["agy"], now="2026-11-25T00:00:00Z")["candidates"][0]
    assert c_user["rate_card"]["rates"]["input"] == pytest.approx(1.25)
    assert c_user["rate_card"]["version"] == "user-config"

    # On 2026-12-02, the scheduled built-in period (`2026-12-01`, $4.00) is newer than `2026-11-20` and wins
    c_dec = router.candidates("agy", include_agents=["agy"], now="2026-12-02T00:00:00Z")["candidates"][0]
    assert c_dec["rate_card"]["rates"]["input"] == pytest.approx(4.0)
    assert c_dec["rate_card"]["version"] == "google-cloud-list-2026-11-12"


@pytest.mark.parametrize(
    "payload,match",
    [
        ({"models": {}}, "expected at least one model resolution"),
        (
            {"models": {"claude/foo": {"status": "unlisted", "reason": "bad key"}}},
            "bare catalog key",
        ),
        (
            {"models": {"self-model": {"status": "alias", "alias_of": "self-model", "reason": "loop"}}},
            "cannot alias itself",
        ),
        (
            {
                "models": {
                    "cycle-a": {"status": "alias", "alias_of": "cycle-b", "reason": "a->b"},
                    "cycle-b": {"status": "alias", "alias_of": "cycle-a", "reason": "b->a"},
                }
            },
            "circular alias chain",
        ),
        (
            {"models": {"dangling": {"status": "alias", "alias_of": "nonexistent-key", "reason": "missing"}}},
            "unknown target",
        ),
        (
            {
                "models": {
                    "neg-target": {"status": "unlisted", "reason": "no price"},
                    "bad-alias": {"status": "alias", "alias_of": "neg-target", "reason": "points to unlisted"},
                }
            },
            "marked 'unlisted'",
        ),
        (
            {
                "models": {
                    "bad-exp": {
                        "status": "unlisted",
                        "reason": "expired before resolved",
                        "resolved_at": "2026-11-12T12:00:00Z",
                        "expires_at": "2026-11-12T12:00:00Z",
                    }
                }
            },
            "strictly after resolved_at",
        ),
    ],
)
def test_prices_resolve_rejects_invalid_entries(tmp_path, payload, match):
    router = Router(project=tmp_path, state_directory=tmp_path / "state", include_user_config=False)
    with pytest.raises(AgRouteError, match=match):
        router.prices_resolve(payload, now=NOW)


def test_corrupt_or_unclaimed_prices_snapshot_refusal_and_repair(tmp_path):
    _write_agy_cache("Fetching available models...\ngemini-4-argon-high\tGemini 4 Argon (High)\n")

    # 1. Unclaimed `prices.json` without `store.json` is refused
    unclaimed = tmp_path / "unclaimed"
    unclaimed.mkdir()
    (unclaimed / "prices.json").write_text("{}", encoding="utf-8")
    with pytest.raises(AgRouteError, match="unclaimed"):
        Router(project=tmp_path, state_directory=unclaimed, include_user_config=False)

    # 2. Claimed store with corrupt `prices.json` refuses `candidates`, then repairs cleanly on `prices_resolve`
    state_dir = tmp_path / "state"
    router = Router(project=tmp_path, state_directory=state_dir, include_user_config=False)
    router.prices_resolve(
        {"models": {"temp-model": {"status": "unlisted", "reason": "initial"}}},
        now=NOW,
    )
    (state_dir / "prices.json").write_text("{corrupt json", encoding="utf-8")
    with pytest.raises(AgRouteError, match="agr prices resolve --file"):
        router.candidates("agy", include_agents=["agy"], now=NOW)

    repaired = router.prices_resolve(
        {"models": {"temp-model": {"status": "unlisted", "reason": "repaired"}}},
        now=NOW,
    )
    assert repaired["previous_snapshot_error"] is not None
    assert router.candidates("agy", include_agents=["agy"], now=NOW)["candidates"]
