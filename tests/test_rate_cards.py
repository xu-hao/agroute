"""Built-in per-token rate cards on emitted candidates (#30).

Expected prices are copied independently from each provider's published pricing table so
a catalog edit that drifts from the source fails here.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import time
import tomllib

import pytest

from agroute import AgRouteError, Router, load_config
from agroute.cli import main
from agroute.discovery import parse_agy_models_output

NOW = "2026-11-12T12:00:00Z"
# The catalog's recorded verification time against the providers' pricing pages.
VERIFIED = "2026-11-12T02:18:20+00:00"
CATEGORIES = {"input", "output", "cache_read", "cache_write", "cache_write_5m", "cache_write_1h"}
ANTHROPIC = ("anthropic", "https://platform.claude.com/docs/en/about-claude/pricing", "anthropic-list-2026-11-12")
OPENAI = ("openai", "https://developers.openai.com/api/docs/pricing", "openai-list-2026-11-12")
GOOGLE = ("google", "https://cloud.google.com/vertex-ai/generative-ai/pricing", "google-cloud-list-2026-11-12")
OPENROUTER = ("openrouter", "https://openrouter.ai/api/v1/models (openrouter/openai/gpt-oss-120b)",
              "openrouter-listing-2026-11-12")

# Anthropic model pricing table, in its column order:
# base input, 5m cache write, 1h cache write, cache hit, output ($/MTok).
ANTHROPIC_ROWS = {
    "claude-fable-5-2": (8, 10, 16, 0.20, 40),
    "claude-mythos-5-2": (8, 10, 16, 0.20, 40),
    "claude-fable-5-1": (10, 12.50, 20, 0.25, 50),
    "claude-mythos-5-1": (10, 12.50, 20, 0.25, 50),
    "claude-fable-5": (10, 12.50, 20, 1, 50),
    "claude-mythos-5": (10, 12.50, 20, 1, 50),
    "claude-opus-5-5": (4, 5, 8, 0.20, 20),
    "claude-opus-5": (5, 6.25, 10, 0.50, 25),
    "claude-opus-4-8": (5, 6.25, 10, 0.50, 25),
    "claude-opus-4-7": (5, 6.25, 10, 0.50, 25),
    "claude-opus-4-6": (5, 6.25, 10, 0.50, 25),
    "claude-sonnet-5-5": (2, 2.50, 4, 0.10, 10),
    "claude-sonnet-5": (2, 2.50, 4, 0.20, 10),
    "claude-sonnet-4-6": (3, 3.75, 6, 0.30, 15),
    # The "prompts over 100,000 tokens" row: agent sessions routinely exceed it.
    "claude-haiku-5-5": (0.50, 0.625, 1, 0.05, 2.50),
    "claude-haiku-4-5": (1, 1.25, 2, 0.10, 5),
    "claude-haiku-4-5-20251001": (1, 1.25, 2, 0.10, 5),
}

# OpenAI standard short-context rows: input, output, cached input, 30m write ($/MTok).
OPENAI_ROWS = {
    "gpt-6-astra": (10.00, 50.00, 1.00, 12.50),
    "gpt-6-sol": (2.00, 10.00, 0.20, 2.50),
    "gpt-6.1-sol": (2.00, 10.00, 0.10, 2.50),
    "gpt-6.1-terra": (1.00, 5.00, 0.05, 1.25),
    "gpt-6-luna": (0.10, 0.50, 0.01, 0.125),
}

# Google Cloud global rows: (effective_at, input, output, cached input) ($/MTok).
GOOGLE_PERIODS = {
    "gemini-4-carbon": [(VERIFIED, 2.00, 10.00, 0.10), ("2026-12-01T00:00:00+00:00", 4.00, 20.00, 0.20)],
    "gemini-4-argon": [(VERIFIED, 2.00, 10.00, 0.10), ("2026-12-01T00:00:00+00:00", 4.00, 20.00, 0.20)],
    "gemini-3.8-flash": [(VERIFIED, 0.75, 3.75, 0.075), ("2027-01-01T00:00:00+00:00", 1.50, 7.50, 0.15)],
    "gemini-3.5-flash-lite": [(VERIFIED, 0.30, 2.50, 0.03)],
}

# Guessed cards for agy cells with no published price, labelled so they cannot pass as list prices.
GUESS_VERSION = "agroute-guess-2026-11-12"
AGY_TIER_GUESSES = {"pro": "gemini-4-argon", "flash": "gemini-3.8-flash", "flash_lite": "gemini-3.5-flash-lite"}
# `invoke_subagent` tiers are native candidates only when configured explicitly.
AGY_TIER_CONFIG = "".join(f'[[agents.agy.native_candidates]]\nmodel = "{tier}"\neffort = "standard"\n\n'
                          for tier in AGY_TIER_GUESSES)
# A third-party listing, not an official price: prompt, completion and cache read; writes at input.
GPT_OSS_LISTED = {"input": 0.15, "output": 0.60, "cache_read": 0.075, "cache_write": 0.15,
                  "cache_write_5m": 0.15, "cache_write_1h": 0.15}

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


def anthropic_rates(model):
    base, write_5m, write_1h, hit, output = ANTHROPIC_ROWS[model]
    return {"input": base, "output": output, "cache_read": hit, "cache_write": write_5m,
            "cache_write_5m": write_5m, "cache_write_1h": write_1h}


def openai_rates(model):
    base, output, cached, write_30m = OPENAI_ROWS[model]
    return {"input": base, "output": output, "cache_read": cached, "cache_write": write_30m,
            "cache_write_5m": write_30m, "cache_write_1h": write_30m}


def google_card_fields(model, at):
    period = [p for p in GOOGLE_PERIODS[model] if datetime.fromisoformat(p[0]) <= datetime.fromisoformat(at)][-1]
    effective_at, base, output, cached = period
    return effective_at, {"input": base, "output": output, "cache_read": cached, "cache_write": base,
                          "cache_write_5m": base, "cache_write_1h": base}


def assert_howhard_rate_card(cell):
    """Apply the howhard-rate-card-v1 shape rules without importing howhard."""
    card = cell["rate_card"]
    assert set(card) == {"format", "source", "version", "effective_at", "provider", "model",
                         "service_tier", "basis", "rates"}
    assert card["format"] == "howhard-rate-card-v1"
    assert card["basis"] == "estimate"
    for key in ("source", "version", "provider", "model", "service_tier"):
        assert isinstance(card[key], str) and card[key].strip()
    assert datetime.fromisoformat(card["effective_at"]).utcoffset() is not None
    assert set(card["rates"]) == CATEGORIES
    assert all(type(rate) in (int, float) and rate >= 0 for rate in card["rates"].values())
    # howhard's native preflight rejects a card whose model differs from the cell's.
    assert card["model"] == cell["model"]
    assert card["service_tier"] == "standard"
    assert "usd_per_mtok" not in cell
    return card


def assert_card(cell, provider, rates, effective_at=VERIFIED):
    card = assert_howhard_rate_card(cell)
    assert (card["provider"], card["source"], card["version"]) == provider
    assert card["effective_at"] == effective_at
    assert card["rates"] == pytest.approx(rates)


def assert_guess(cell, provider, rates, priced_as=None, effective_at=VERIFIED):
    card = assert_howhard_rate_card(cell)
    assert card["source"].startswith("guess: ")
    if priced_as is not None:
        assert f"; priced as {priced_as} from " in card["source"]
    assert (card["provider"], card["version"]) == (provider, GUESS_VERSION)
    assert card["effective_at"] == effective_at
    assert card["rates"] == pytest.approx(rates)


def assert_unpriced(cell):
    assert "rate_card" not in cell and "usd_per_mtok" not in cell


def write_claude_catalog(models, efforts=("low", "high")):
    entries = [{"id": model, "section": "main", "runtime": {"effort_levels": list(efforts)}} for model in models]
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "cache/model-catalog/catalog.json"
    path.write_text(json.dumps({"catalog": {"config": {"models": entries}}}), encoding="utf-8")


def write_codex_cache(slugs, efforts=("low", "xhigh")):
    entries = [{"slug": slug, "visibility": "list", "priority": i,
                "supported_reasoning_levels": [{"effort": e} for e in efforts]} for i, slug in enumerate(slugs)]
    (Path(os.environ["CODEX_HOME"]) / "models_cache.json").write_text(json.dumps({"models": entries}), encoding="utf-8")


def write_agy_cache(candidates):
    (Path(os.environ["AGROUTE_SPOOL_DIR"]) / "agy_models_cache.json").write_text(json.dumps({
        "format": "agroute-agy-models-cache-v1", "fetched_at": time.time(), "candidates": candidates}),
        encoding="utf-8")


def cli_candidates(tmp_path, capsys, host, agents, *extra, now=NOW):
    output = tmp_path / f"candidates-{host}.json"
    argv = ["candidates", "--host", host, "--include-agents", agents, "--no-user-config",
            "--state-dir", str(tmp_path / "state"), "--now", now, "--output", str(output), "--json", *extra]
    assert main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidates"] == json.loads(output.read_text())
    return {cell["model"]: cell for cell in payload["candidates"]}, payload


def test_discovered_claude_cells_carry_builtin_cards_on_cli_and_api(tmp_path, capsys):
    write_claude_catalog(ANTHROPIC_ROWS)
    cells, payload = cli_candidates(tmp_path, capsys, "claude", "claude")
    assert len(payload["candidates"]) == 2 * len(ANTHROPIC_ROWS)
    for cell in payload["candidates"]:
        model = cell["model"].removeprefix("claude/")
        assert_card(cell, ANTHROPIC, anthropic_rates(model))
    # Same capture root (the working directory) and state as the CLI run.
    api = Router(state_directory=tmp_path / "state", include_user_config=False)
    assert api.candidates("claude", include_agents=["claude"], now=NOW)["candidates"] == payload["candidates"]


def test_claude_cli_surface_cells_carry_the_same_cards(tmp_path, capsys):
    write_claude_catalog(["claude-opus-5-5", "claude-haiku-5-5"])
    cells, _ = cli_candidates(tmp_path, capsys, "codex", "claude")
    assert set(cells) == {"claude/claude-opus-5-5", "claude/claude-haiku-5-5"}
    for qualified, cell in cells.items():
        assert_card(cell, ANTHROPIC, anthropic_rates(qualified.removeprefix("claude/")))


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_codex_cells_use_short_context_rows_and_the_default_30m_write(tmp_path, capsys, host):
    write_codex_cache(OPENAI_ROWS)
    cells, payload = cli_candidates(tmp_path, capsys, host, "codex")
    assert len(payload["candidates"]) == 2 * len(OPENAI_ROWS)
    for cell in payload["candidates"]:
        assert_card(cell, OPENAI, openai_rates(cell["model"].removeprefix("codex/")))


def test_agy_cli_cells_strip_the_effort_suffix_and_reuse_anthropic_cards(tmp_path, capsys):
    write_agy_cache(parse_agy_models_output(AGY_MODELS))
    cells, payload = cli_candidates(tmp_path, capsys, "claude", "agy")
    assert len(payload["candidates"]) == 16
    for effort in ("low", "medium", "high"):
        for model in ("gemini-4-carbon", "gemini-4-argon", "gemini-3.8-flash"):
            effective_at, rates = google_card_fields(model, NOW)
            assert_card(cells[f"agy/{model}-{effort}"], GOOGLE, rates, effective_at)
        for model in ("claude-opus-5-5", "claude-sonnet-5-5"):
            assert_card(cells[f"agy/{model}-{effort}"], ANTHROPIC, anthropic_rates(model))
    # The open-weight model has no first-party list price; it carries OpenRouter's listed rate.
    assert_card(cells["agy/gpt-oss-120b-medium"], OPENROUTER, GPT_OSS_LISTED)


@pytest.mark.parametrize(
    "candidate,priced",
    [
        ({"model": "gemini-4-argon-high", "effort": "high"}, True),
        ({"model": "gemini-4-argon", "effort": "medium"}, True),
        # Only the cell's own effort is stripped, and only as an exact suffix.
        ({"model": "gemini-4-argon-high", "effort": "low"}, False),
        ({"model": "gemini-4-argon-preview-high", "effort": "high"}, False),
        ({"model": "gemini-4-argonhigh", "effort": "high"}, False),
        ({"model": "xgemini-4-argon-high", "effort": "high"}, False),
    ],
)
def test_agy_effort_suffix_rule_is_exact(tmp_path, capsys, candidate, priced):
    write_agy_cache([candidate])
    cells, _ = cli_candidates(tmp_path, capsys, "claude", "agy")
    cell = cells[f"agy/{candidate['model']}"]
    if priced:
        assert_card(cell, GOOGLE, google_card_fields("gemini-4-argon", NOW)[1])
    else:
        assert_unpriced(cell)


def test_agy_native_tier_aliases_carry_labelled_guesses(tmp_path, capsys):
    config = tmp_path / "agroute.toml"
    config.write_text(AGY_TIER_CONFIG, encoding="utf-8")
    cells, payload = cli_candidates(tmp_path, capsys, "agy", "agy", "--config", str(config))
    assert set(cells) == {"agy/pro", "agy/flash", "agy/flash_lite"}
    for alias, model in AGY_TIER_GUESSES.items():
        effective_at, rates = google_card_fields(model, NOW)
        assert_guess(cells[f"agy/{alias}"], "google", rates, model, effective_at)


def test_tier_alias_table_is_exact(tmp_path, capsys):
    config = tmp_path / "agroute.toml"
    config.write_text(
        '[[agents.agy.native_candidates]]\nmodel = "pro"\neffort = "high"\n\n'
        '[[agents.agy.native_candidates]]\nmodel = "inherit"\neffort = "standard"\n',
        encoding="utf-8",
    )
    cells, _ = cli_candidates(tmp_path, capsys, "agy", "agy", "--config", str(config))
    assert_guess(cells["agy/pro"], "google", google_card_fields("gemini-4-argon", NOW)[1], "gemini-4-argon")
    # `inherit` runs whatever model the caller runs, so no table entry can price it.
    assert_unpriced(cells["agy/inherit"])
    # Tier names are aliases only on the native surface; a CLI model named like a tier is not one.
    write_agy_cache([{"model": "pro", "effort": "standard"}, {"model": "flash-high", "effort": "high"}])
    cells, _ = cli_candidates(tmp_path, capsys, "claude", "agy", "--config", str(config))
    assert_unpriced(cells["agy/pro"])
    assert_unpriced(cells["agy/flash-high"])


@pytest.mark.parametrize(
    "model,alias,before,after",
    [
        ("gemini-4-carbon", None, "2026-11-30T23:59:59Z", "2026-12-01T00:00:00Z"),
        ("gemini-4-argon", "pro", "2026-11-30T23:59:59Z", "2026-12-01T00:00:00Z"),
        ("gemini-3.8-flash", "flash", "2026-12-31T23:59:59Z", "2027-01-01T00:00:00Z"),
    ],
)
def test_google_cards_follow_published_price_changes(tmp_path, capsys, model, alias, before, after):
    write_agy_cache([{"model": f"{model}-high", "effort": "high"}])
    # A separate config directory is a separate capture root, so it gets its own state.
    tiers = tmp_path / "tiers"
    tiers.mkdir()
    config = tiers / "agroute.toml"
    config.write_text(AGY_TIER_CONFIG, encoding="utf-8")
    for at, period in ((before, 0), (after, 1)):
        effective_at, base, output, cached = GOOGLE_PERIODS[model][period]
        rates = {"input": base, "output": output, "cache_read": cached, "cache_write": base,
                 "cache_write_5m": base, "cache_write_1h": base}
        cells, payload = cli_candidates(tmp_path, capsys, "claude", "agy", now=at)
        assert payload["evaluated_at"] == at
        assert_card(cells[f"agy/{model}-high"], GOOGLE, rates, effective_at)
        # The agy host prices the same discovered slug identically on the native surface.
        cells, _ = cli_candidates(tmp_path, capsys, "agy", "agy", now=at)
        assert_card(cells[f"agy/{model}-high"], GOOGLE, rates, effective_at)
        if alias is not None:
            cells, _ = cli_candidates(tiers, capsys, "agy", "agy", "--config", str(config), now=at)
            assert_guess(cells[f"agy/{alias}"], "google", rates, model, effective_at)


def test_no_card_is_attached_before_catalog_verification(tmp_path, capsys):
    write_claude_catalog(["claude-opus-5-5"])
    cells, _ = cli_candidates(tmp_path, capsys, "claude", "claude", now="2026-11-12T02:18:19Z")
    assert all("rate_card" not in cell for cell in cells.values())
    cells, _ = cli_candidates(tmp_path, capsys, "claude", "claude", now="2026-11-12T02:18:20Z")
    assert_card(cells["claude/claude-opus-5-5"], ANTHROPIC, anthropic_rates("claude-opus-5-5"))


def test_unknown_models_pass_through_unpriced(tmp_path, capsys):
    write_claude_catalog(["claude-current-0", "claude-opus-5-5-preview"])
    write_codex_cache(["gpt-6-unlisted"])
    cells, payload = cli_candidates(tmp_path, capsys, "claude", "claude,codex")
    assert set(cells) == {"claude/claude-current-0", "claude/claude-opus-5-5-preview", "codex/gpt-6-unlisted"}
    assert len(payload["candidates"]) == 6
    for cell in payload["candidates"]:
        assert_unpriced(cell)


def test_configured_prices_win_and_unpriced_configured_cells_get_builtin_cards(tmp_path, capsys):
    user_card = {
        "format": "howhard-rate-card-v1", "source": "https://example.invalid/negotiated", "version": "contract-1",
        "effective_at": "2026-01-01T00:00:00+00:00", "provider": "anthropic", "model": "claude/claude-sonnet-5-5",
        "service_tier": "standard", "basis": "charge",
        "rates": {"input": 1.0, "output": 5.0, "cache_read": 0.05, "cache_write": 1.25,
                  "cache_write_5m": 1.25, "cache_write_1h": 2.0},
    }
    rates = ", ".join(f"{k} = {v}" for k, v in user_card["rates"].items())
    fields = ", ".join(f"{k} = {json.dumps(v)}" for k, v in user_card.items() if k != "rates")
    config = tmp_path / "agroute.toml"
    config.write_text(
        '[[agents.claude.native_candidates]]\nmodel = "claude-opus-5-5"\neffort = "high"\nusd_per_mtok = 1.67\n\n'
        '[[agents.claude.native_candidates]]\nmodel = "claude-sonnet-5-5"\neffort = "high"\n'
        f"rate_card = {{ {fields}, rates = {{ {rates} }} }}\n\n"
        '[[agents.claude.native_candidates]]\nmodel = "claude-haiku-5-5"\neffort = "low"\n',
        encoding="utf-8",
    )
    cells, _ = cli_candidates(tmp_path, capsys, "claude", "claude", "--config", str(config))
    assert cells["claude/claude-opus-5-5"] == {"model": "claude/claude-opus-5-5", "effort": "high", "usd_per_mtok": 1.67}
    assert cells["claude/claude-sonnet-5-5"] == {"model": "claude/claude-sonnet-5-5", "effort": "high", "rate_card": user_card}
    assert_card(cells["claude/claude-haiku-5-5"], ANTHROPIC, anthropic_rates("claude-haiku-5-5"))


def test_configured_prices_win_over_guessed_cards(tmp_path, capsys):
    config = tmp_path / "agroute.toml"
    config.write_text(
        '[[agents.agy.native_candidates]]\nmodel = "pro"\neffort = "standard"\nusd_per_mtok = 0.9\n\n'
        '[[agents.agy.native_candidates]]\nmodel = "flash"\neffort = "standard"\n\n'
        '[[agents.agy.cli_candidates]]\nmodel = "gpt-oss-120b-medium"\neffort = "medium"\nusd_per_mtok = 0.2\n',
        encoding="utf-8",
    )
    cells, _ = cli_candidates(tmp_path, capsys, "agy", "agy", "--config", str(config))
    assert cells["agy/pro"] == {"model": "agy/pro", "effort": "standard", "usd_per_mtok": 0.9}
    assert_guess(cells["agy/flash"], "google", google_card_fields("gemini-3.8-flash", NOW)[1], "gemini-3.8-flash")
    cells, _ = cli_candidates(tmp_path, capsys, "claude", "agy", "--config", str(config))
    assert cells["agy/gpt-oss-120b-medium"] == {"model": "agy/gpt-oss-120b-medium", "effort": "medium", "usd_per_mtok": 0.2}


def test_card_model_follows_the_namespace_separator(tmp_path, capsys):
    write_claude_catalog(["claude-opus-5-5"])
    config = tmp_path / "agroute.toml"
    config.write_text('[routing]\nnamespace_separator = ":"\n', encoding="utf-8")
    cells, _ = cli_candidates(tmp_path, capsys, "claude", "claude", "--config", str(config))
    assert set(cells) == {"claude:claude-opus-5-5"}
    assert_card(cells["claude:claude-opus-5-5"], ANTHROPIC, anthropic_rates("claude-opus-5-5"))


def test_toml_inventory_keeps_rate_cards(tmp_path, capsys):
    write_claude_catalog(["claude-opus-5-5", "claude-current-0"])
    write_codex_cache(["gpt-6.1-sol"])
    write_agy_cache(parse_agy_models_output(AGY_MODELS))
    _, payload = cli_candidates(tmp_path, capsys, "claude", "claude,codex,agy")
    assert any("rate_card" in cell for cell in payload["candidates"])
    common = ["--host", "claude", "--no-user-config", "--state-dir", str(tmp_path / "state"), "--now", NOW]
    assert main(["inventory", *common]) == 0
    assert tomllib.loads(capsys.readouterr().out)["inventories"]["router"] == payload["candidates"]


USER_RATES = {"input": 4.5, "output": 21.0, "cache_read": 0.25, "cache_write": 5.5,
              "cache_write_5m": 5.5, "cache_write_1h": 9.0}


def price_entry(agent, model, entry):
    body = "\n".join(f"{key} = {value}" for key, value in entry.items())
    return f'[agents.{agent}.prices."{model}"]\n{body}\n'


def assert_user_card(cell, provider, rates=USER_RATES):
    card = assert_howhard_rate_card(cell)
    assert (card["source"], card["version"], card["provider"]) == ("user config", "user-config", provider)
    assert datetime.fromisoformat(card["effective_at"]) == datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    assert card["rates"] == rates


def test_user_price_overrides_a_discovered_model_with_discovery_on(tmp_path):
    write_claude_catalog(["claude-opus-5-5", "claude-sonnet-5-5"])
    user_config = Path(os.environ["XDG_CONFIG_HOME"]) / "agroute/config.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text(price_entry("claude", "claude-opus-5-5", USER_RATES), encoding="utf-8")
    router = Router(project=tmp_path, state_directory=tmp_path / "state")
    cells = {(c["model"], c["effort"]): c for c in router.candidates("claude", include_agents=["claude"], now=NOW)["candidates"]}
    assert len(cells) == 4
    for effort in ("low", "high"):
        assert_user_card(cells["claude/claude-opus-5-5", effort], "claude")
        assert_card(cells["claude/claude-sonnet-5-5", effort], ANTHROPIC, anthropic_rates("claude-sonnet-5-5"))


def test_user_price_replaces_a_guess_and_prices_an_unknown_model(tmp_path, capsys):
    write_claude_catalog(["claude-current-0"])
    config = tmp_path / "agroute.toml"
    config.write_text(AGY_TIER_CONFIG + price_entry("agy", "flash", {"usd_per_mtok": 0.9})
                      + price_entry("claude", "claude-current-0", USER_RATES), encoding="utf-8")
    cells, _ = cli_candidates(tmp_path, capsys, "agy", "agy", "--config", str(config))
    assert cells["agy/flash"] == {"model": "agy/flash", "effort": "standard", "usd_per_mtok": 0.9}
    assert_guess(cells["agy/pro"], "google", google_card_fields("gemini-4-argon", NOW)[1], "gemini-4-argon")
    cells, _ = cli_candidates(tmp_path, capsys, "claude", "claude", "--config", str(config))
    assert_user_card(cells["claude/claude-current-0"], "claude")


def test_price_precedence_levels(tmp_path, capsys):
    config = tmp_path / "agroute.toml"
    config.write_text(
        '[[agents.claude.native_candidates]]\nmodel = "claude-opus-5-5"\neffort = "high"\nusd_per_mtok = 1.67\n\n'
        + "".join(f'[[agents.claude.native_candidates]]\nmodel = "{model}"\neffort = "high"\n\n'
                  for model in ("claude-sonnet-5-5", "claude-haiku-5-5", "claude-current-0"))
        + price_entry("claude", "claude-opus-5-5", USER_RATES)
        + price_entry("claude", "claude-sonnet-5-5", USER_RATES),
        encoding="utf-8",
    )
    cells, _ = cli_candidates(tmp_path, capsys, "claude", "claude", "--config", str(config))
    # 1. A per-candidate price beats a per-model price.
    assert cells["claude/claude-opus-5-5"] == {"model": "claude/claude-opus-5-5", "effort": "high", "usd_per_mtok": 1.67}
    # 2. A per-model price beats the built-in card.
    assert_user_card(cells["claude/claude-sonnet-5-5"], "claude")
    # 3. The built-in card applies when no configured price exists.
    assert_card(cells["claude/claude-haiku-5-5"], ANTHROPIC, anthropic_rates("claude-haiku-5-5"))
    # 4. Otherwise the cell is unpriced.
    assert_unpriced(cells["claude/claude-current-0"])


def test_later_config_layers_replace_prices_per_model(tmp_path):
    write_claude_catalog(["claude-opus-5-5", "claude-sonnet-5-5"])
    user_config = Path(os.environ["XDG_CONFIG_HOME"]) / "agroute/config.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text(price_entry("claude", "claude-opus-5-5", {"usd_per_mtok": 3.0})
                           + price_entry("claude", "claude-sonnet-5-5", {"usd_per_mtok": 1.0}), encoding="utf-8")
    (tmp_path / "agroute.toml").write_text(price_entry("claude", "claude-sonnet-5-5", USER_RATES), encoding="utf-8")
    router = Router(project=tmp_path, state_directory=tmp_path / "state")
    cells = {(c["model"], c["effort"]): c for c in router.candidates("claude", include_agents=["claude"], now=NOW)["candidates"]}
    assert cells["claude/claude-opus-5-5", "high"]["usd_per_mtok"] == 3.0
    assert_user_card(cells["claude/claude-sonnet-5-5", "high"], "claude")


@pytest.mark.parametrize(
    "entry,match",
    [
        ({k: v for k, v in USER_RATES.items() if k != "cache_write_1h"}, "missing keys \\['cache_write_1h'\\]"),
        ({**USER_RATES, "storage": 1.0}, "unknown keys \\['storage'\\]"),
        ({**USER_RATES, "usd_per_mtok": 1.0}, "usd_per_mtok cannot be combined with \\['cache_read'"),
        ({"usd_per_mtok": 1.0, "input": 1.0}, "usd_per_mtok cannot be combined with \\['input'\\]"),
        ({**USER_RATES, "output": -1.0}, "\\.output: expected a non-negative number"),
        ({"usd_per_mtok": 0}, "usd_per_mtok: expected a positive number"),
        ({"usd_per_mtok": -2.0}, "usd_per_mtok: expected a positive number"),
        ({"usd_per_mtok": '"cheap"'}, "usd_per_mtok: expected a finite number"),
    ],
)
def test_invalid_price_entries_refuse_at_config_load(tmp_path, entry, match):
    config = tmp_path / "agroute.toml"
    config.write_text(price_entry("claude", "claude-opus-5-5", entry), encoding="utf-8")
    with pytest.raises(AgRouteError, match=f'agents\\.claude\\.prices\\."claude-opus-5-5".*{match}'):
        load_config(config, include_user_config=False)


@pytest.mark.parametrize(
    "body,match",
    [
        ('[agents.claude.prices]\n"claude-opus-5-5" = 1.0\n', 'prices\\."claude-opus-5-5": expected a table'),
        ('[agents.claude.prices."claude/claude-opus-5-5"]\nusd_per_mtok = 1.0\n', "bare model ID"),
        ('[agents.claude]\nprices = 1\n', "agents\\.claude\\.prices: expected a table"),
    ],
)
def test_malformed_price_tables_refuse_at_config_load(tmp_path, body, match):
    config = tmp_path / "agroute.toml"
    config.write_text(body, encoding="utf-8")
    with pytest.raises(AgRouteError, match=match):
        load_config(config, include_user_config=False)


def test_toml_inventory_round_trips_configured_cards_with_any_text(tmp_path, capsys):
    config = tmp_path / "agroute.toml"
    config.write_text(
        '[[agents.claude.native_candidates]]\nmodel = "claude-opus-5-5"\neffort = "high"\n\n'
        '[agents.claude.native_candidates.rate_card]\nformat = "howhard-rate-card-v1"\n'
        'source = "https://example.invalid/price \U0001F4B8 \\"quoted\\" back\\\\slash tab\\t del\\u007f"\n'
        'version = "v1"\neffective_at = "2026-01-01T00:00:00+00:00"\nprovider = "anthropic"\n'
        'model = "claude/claude-opus-5-5"\nservice_tier = "standard"\nbasis = "estimate"\n\n'
        '[agents.claude.native_candidates.rate_card.rates]\n'
        + "".join(f"{key} = {value}\n" for key, value in USER_RATES.items()),
        encoding="utf-8",
    )
    _, payload = cli_candidates(tmp_path, capsys, "claude", "claude", "--config", str(config))
    assert "\U0001F4B8" in payload["candidates"][0]["rate_card"]["source"]
    common = ["--host", "claude", "--include-agents", "claude", "--no-user-config", "--state-dir",
              str(tmp_path / "state"), "--now", NOW, "--config", str(config)]
    assert main(["inventory", *common]) == 0
    assert tomllib.loads(capsys.readouterr().out)["inventories"]["router"] == payload["candidates"]
