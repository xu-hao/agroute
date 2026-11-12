"""`agroute prices refresh` and refreshed-snapshot precedence (#30).

The price source is a recorded OpenRouter model listing; no test reaches the network.
"""

import hashlib
import json
import os
from pathlib import Path

import pytest

from agroute import AgRouteError, Router, price_refresh
from agroute.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "openrouter_models.json"
VERIFIED = "2026-11-12T02:18:20+00:00"
SOURCE = "https://openrouter.ai/api/v1/models"


class Source:
    """A recorded price source: each call returns or raises the next scripted response."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(price_refresh, "_sleep", recorded.append)
    return recorded


def listing(edit=None):
    data = json.loads(FIXTURE.read_text())
    if edit is not None:
        edit({row["id"]: row for row in data["data"]}, data)
    return json.dumps(data).encode()


def refresh(tmp_path, capsys, monkeypatch, source, at, *, as_json=True):
    monkeypatch.setattr(price_refresh, "_http_get", source)
    argv = ["prices", "refresh", "--no-user-config", "--state-dir", str(tmp_path / "state"), "--now", at]
    code = main(argv + (["--json"] if as_json else []))
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if as_json and code == 0 else captured.out), captured.err


def candidates(tmp_path, capsys, host, agents, at, *extra):
    argv = ["candidates", "--host", host, "--include-agents", agents, "--no-user-config",
            "--state-dir", str(tmp_path / "state"), "--now", at, "--json", *extra]
    assert main(argv) == 0
    return {cell["model"]: cell for cell in json.loads(capsys.readouterr().out)["candidates"]}


def write_claude_catalog(models):
    entries = [{"id": model, "section": "main", "runtime": {"effort_levels": ["high"]}} for model in models]
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "cache/model-catalog/catalog.json"
    path.write_text(json.dumps({"catalog": {"config": {"models": entries}}}), encoding="utf-8")


def write_agy_cache(models):
    import time
    cells = [{"model": f"{model}-high", "effort": "high"} for model in models]
    (Path(os.environ["AGROUTE_SPOOL_DIR"]) / "agy_models_cache.json").write_text(json.dumps({
        "format": "agroute-agy-models-cache-v1", "fetched_at": time.time(), "candidates": cells}), encoding="utf-8")


def test_refresh_maps_ids_units_and_categories_into_a_snapshot(tmp_path, capsys, monkeypatch, sleeps):
    body = listing()
    code, report, _ = refresh(tmp_path, capsys, monkeypatch, Source(body), "2026-11-20T00:00:00Z")
    assert code == 0
    # The snapshot is replaced in one rename; no temporary file is left behind.
    assert sorted(p.name for p in (tmp_path / "state").iterdir() if p.name.startswith("prices")) == [
        "prices.json", "prices.lock"]
    snapshot = json.loads((tmp_path / "state/prices.json").read_text())
    assert snapshot["format"] == "agroute-price-snapshot-v1"
    assert snapshot["source"] == SOURCE
    assert snapshot["fetched_at"] == "2026-11-20T00:00:00+00:00"
    assert snapshot["version"] == report["version"] == "openrouter-2026-11-20T00:00:00Z"
    assert snapshot["content_sha256"] == hashlib.sha256(body).hexdigest()
    models = snapshot["models"]
    # Every mapped OpenRouter ID, two agroute IDs for the dated Haiku 4.5 alias; batch rows ignored.
    assert len(models) == 23 and report["models"] == 23
    assert models["claude-opus-5-5"] == {"openrouter_id": "anthropic/claude-opus-5.5", "rates": {
        "input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0, "cache_write_5m": 5.0, "cache_write_1h": 8.0}}
    assert models["claude-haiku-4-5-20251001"] == models["claude-haiku-4-5"]
    # Haiku 5.5 uses the over-100K tier from the listing's overrides.
    assert models["claude-haiku-5-5"]["rates"] == {
        "input": 0.5, "output": 2.5, "cache_read": 0.05, "cache_write": 0.625, "cache_write_5m": 0.625, "cache_write_1h": 1.0}
    # OpenAI: short-context row; every write is the default 30-minute write.
    assert models["gpt-6.1-sol"]["rates"] == {
        "input": 2.0, "output": 10.0, "cache_read": 0.1, "cache_write": 2.5, "cache_write_5m": 2.5, "cache_write_1h": 2.5}
    # Google: writes at input; the storage-derived input_cache_write is ignored.
    assert models["gemini-3.8-flash"]["rates"] == {
        "input": 0.825, "output": 4.125, "cache_read": 0.0825, "cache_write": 0.825, "cache_write_5m": 0.825,
        "cache_write_1h": 0.825}
    # gpt-oss lists no write price: writes at input.
    assert models["gpt-oss-120b"]["rates"] == {
        "input": 0.15, "output": 0.6, "cache_read": 0.075, "cache_write": 0.15, "cache_write_5m": 0.15, "cache_write_1h": 0.15}
    assert sleeps == []


def test_refresh_reports_drift_against_the_prices_in_use(tmp_path, capsys, monkeypatch, sleeps):
    code, report, _ = refresh(tmp_path, capsys, monkeypatch, Source(listing()), "2026-11-20T00:00:00Z")
    assert code == 0 and report["previous_fetched_at"] is None
    changes = {row["model"]: row for row in report["changes"]}
    flash = changes["gemini-3.8-flash"]
    assert flash["status"] == "changed"
    assert (flash["old_version"], flash["new_version"]) == ("google-cloud-list-2026-11-12", "openrouter-2026-11-20T00:00:00Z")
    assert flash["categories"]["input"] == {"old": 0.75, "new": 0.825}
    assert flash["categories"]["output"] == {"old": 3.75, "new": 4.125}
    assert "claude-opus-5-5" in report["unchanged"]
    # A second refresh compares against the first snapshot and shows its fetch time.
    def cheaper(rows, _):
        rows["anthropic/claude-opus-5.5"]["pricing"]["prompt"] = "0.0000035"
    code, text, _ = refresh(tmp_path, capsys, monkeypatch, Source(listing(cheaper)), "2026-11-21T00:00:00Z", as_json=False)
    assert code == 0
    assert "previous fetch 2026-11-20T00:00:00+00:00" in text
    assert ("changed claude-opus-5-5: input 4.0 -> 3.5 "
            "(openrouter-2026-11-20T00:00:00Z -> openrouter-2026-11-21T00:00:00Z)") in text


@pytest.mark.parametrize("overrides", [None, 5, [{"min_prompt_tokens": 200000}]])
def test_missing_haiku_tier_keeps_the_built_in_card_and_says_so(tmp_path, capsys, monkeypatch, sleeps, overrides):
    def untiered(rows, _):
        pricing = rows["anthropic/claude-haiku-5.5"]["pricing"]
        if overrides is None:
            del pricing["overrides"]
        else:
            pricing["overrides"] = overrides
    code, report, _ = refresh(tmp_path, capsys, monkeypatch, Source(listing(untiered)), "2026-11-20T00:00:00Z")
    assert code == 0
    assert "claude-haiku-5-5" not in json.loads((tmp_path / "state/prices.json").read_text())["models"]
    assert report["kept_builtin"] == [{"model": "claude-haiku-5-5", "reason":
                                       "anthropic/claude-haiku-5.5 lists no single over-100K prompt tier; "
                                       "the built-in card stays in use"}]


@pytest.mark.parametrize(
    "failure,cause",
    [
        (OSError("connection refused"), "connection refused"),
        (b"<html>busy</html>", "response is not JSON"),
        (json.dumps({"models": []}).encode(), "lacks a 'data' model list"),
        (listing(lambda rows, data: data["data"].remove(rows["google/gemini-4-argon"])),
         "mapped models missing from the response: ['google/gemini-4-argon']"),
        (listing(lambda rows, _: rows["openai/gpt-6-sol"]["pricing"].pop("input_cache_write")),
         "openai/gpt-6-sol: missing price categories ['input_cache_write']"),
        (listing(lambda rows, _: rows["openai/gpt-6-sol"]["pricing"].update(prompt="-0.000001")),
         "openai/gpt-6-sol.prompt: expected a non-negative per-token USD price"),
        (listing(lambda rows, _: rows["openai/gpt-6-sol"]["pricing"].update(prompt="1e999999")),
         "openai/gpt-6-sol.prompt: invalid per-token USD price"),
    ],
)
def test_failed_refresh_retries_three_times_and_keeps_the_old_snapshot(
        tmp_path, capsys, monkeypatch, sleeps, failure, cause):
    assert refresh(tmp_path, capsys, monkeypatch, Source(listing()), "2026-11-20T00:00:00Z")[0] == 0
    before = (tmp_path / "state/prices.json").read_bytes()
    source = Source(failure)
    code, _, err = refresh(tmp_path, capsys, monkeypatch, source, "2026-11-21T00:00:00Z")
    assert code == 1
    assert len(source.calls) == 3 and sleeps == [1.0, 2.0]
    assert "prices refresh: failed after 3 attempts" in err and "previous snapshot is unchanged" in err
    assert cause in err
    assert (tmp_path / "state/prices.json").read_bytes() == before


def test_refresh_succeeds_on_a_retry(tmp_path, capsys, monkeypatch, sleeps):
    source = Source(OSError("timed out"), listing())
    code, report, _ = refresh(tmp_path, capsys, monkeypatch, source, "2026-11-20T00:00:00Z")
    assert code == 0 and len(source.calls) == 2 and sleeps == [1.0]
    assert source.calls[0] == (SOURCE, 15.0)


@pytest.mark.parametrize(
    "fetched,evaluated,winner",
    [
        # A refresh fetched after the verified catalog wins ...
        ("2026-11-20T00:00:00Z", "2026-11-25T00:00:00Z", "snapshot"),
        # ... until a later scheduled built-in period starts.
        ("2026-11-20T00:00:00Z", "2026-12-02T00:00:00Z", "builtin-2026-12-01"),
        # A refresh fetched before verification loses to the verified catalog.
        ("2026-11-12T01:00:00Z", "2026-11-25T00:00:00Z", "builtin"),
        # An exact tie goes to the built-in card.
        (VERIFIED, "2026-11-25T00:00:00Z", "builtin"),
        # A snapshot takes effect at its fetch time.
        ("2026-11-20T00:00:00Z", "2026-11-19T00:00:00Z", "builtin"),
    ],
)
def test_snapshot_and_built_in_compare_by_effective_at(tmp_path, capsys, monkeypatch, sleeps, fetched, evaluated, winner):
    assert refresh(tmp_path, capsys, monkeypatch, Source(listing()), fetched)[0] == 0
    write_agy_cache(["gemini-4-argon"])
    card = candidates(tmp_path, capsys, "claude", "agy", evaluated)["agy/gemini-4-argon-high"]["rate_card"]
    assert set(card) == {"format", "source", "version", "effective_at", "provider", "model", "service_tier",
                         "basis", "rates"}
    assert set(card["rates"]) == {"input", "output", "cache_read", "cache_write", "cache_write_5m", "cache_write_1h"}
    assert card["format"] == "howhard-rate-card-v1" and card["service_tier"] == "standard"
    if winner == "snapshot":
        assert (card["provider"], card["version"]) == ("openrouter", f"openrouter-{fetched}")
        assert card["source"] == f"{SOURCE} (openrouter/google/gemini-4-argon)"
        assert card["effective_at"] == fetched.replace("Z", "+00:00")
        assert card["rates"]["input"] == 2.0
    else:
        assert (card["provider"], card["version"]) == ("google", "google-cloud-list-2026-11-12")
        expected = ("2026-12-01T00:00:00+00:00", 4.0) if winner == "builtin-2026-12-01" else (VERIFIED, 2.0)
        assert (card["effective_at"], card["rates"]["input"]) == expected
    assert card["model"] == "agy/gemini-4-argon-high" and card["basis"] == "estimate"


def test_guessed_tiers_follow_their_model_through_the_snapshot(tmp_path, capsys, monkeypatch, sleeps):
    assert refresh(tmp_path, capsys, monkeypatch, Source(listing()), "2026-11-20T00:00:00Z")[0] == 0
    # The working directory's agroute.toml, so refresh and candidates share one capture root.
    (Path.cwd() / "agroute.toml").write_text(
        '[[agents.agy.native_candidates]]\nmodel = "flash"\neffort = "standard"\n', encoding="utf-8")
    card = candidates(tmp_path, capsys, "agy", "agy", "2026-11-25T00:00:00Z")["agy/flash"]["rate_card"]
    assert card["version"] == "agroute-guess-2026-11-12"
    assert card["source"].endswith("; priced as gemini-3.8-flash from openrouter-2026-11-20T00:00:00Z")
    assert card["rates"]["input"] == 0.825


def test_user_prices_beat_the_snapshot_and_unmapped_models_keep_built_in_cards(tmp_path, capsys, monkeypatch, sleeps):
    assert refresh(tmp_path, capsys, monkeypatch, Source(listing()), "2026-11-20T00:00:00Z")[0] == 0
    write_claude_catalog(["claude-opus-5-5", "claude-sonnet-5-5", "claude-mythos-5-2"])
    # The working directory's agroute.toml, so refresh and candidates share one capture root.
    config = Path.cwd() / "agroute.toml"
    config.write_text(
        '[[agents.claude.native_candidates]]\nmodel = "claude-opus-5-5"\neffort = "high"\nusd_per_mtok = 1.67\n\n'
        '[[agents.claude.native_candidates]]\nmodel = "claude-sonnet-5-5"\neffort = "high"\n\n'
        '[[agents.claude.native_candidates]]\nmodel = "claude-mythos-5-2"\neffort = "high"\n\n'
        '[agents.claude.prices."claude-sonnet-5-5"]\nusd_per_mtok = 0.9\n',
        encoding="utf-8",
    )
    cells = candidates(tmp_path, capsys, "claude", "claude", "2026-11-25T00:00:00Z")
    assert cells["claude/claude-opus-5-5"] == {"model": "claude/claude-opus-5-5", "effort": "high", "usd_per_mtok": 1.67}
    assert cells["claude/claude-sonnet-5-5"] == {"model": "claude/claude-sonnet-5-5", "effort": "high", "usd_per_mtok": 0.9}
    # OpenRouter lists no Mythos model, so its built-in card stays in use.
    assert cells["claude/claude-mythos-5-2"]["rate_card"]["version"] == "anthropic-list-2026-11-12"


def test_routing_never_fetches_and_refuses_a_corrupt_snapshot(tmp_path, capsys):
    write_claude_catalog(["claude-opus-5-5"])
    # The autouse network guard would fail this test if candidates fetched prices.
    assert candidates(tmp_path, capsys, "claude", "claude", "2026-11-25T00:00:00Z")
    (tmp_path / "state/prices.json").write_text('{"format": "agroute-price-snapshot-v1"}', encoding="utf-8")
    router = Router(state_directory=tmp_path / "state", include_user_config=False)
    with pytest.raises(AgRouteError, match="price snapshot"):
        router.candidates("claude", include_agents=["claude"], now="2026-11-25T00:00:00Z")



def test_refresh_replaces_an_unreadable_snapshot(tmp_path, capsys, monkeypatch, sleeps):
    assert refresh(tmp_path, capsys, monkeypatch, Source(listing()), "2026-11-19T00:00:00Z")[0] == 0
    (tmp_path / "state/prices.json").write_text("{", encoding="utf-8")
    code, report, _ = refresh(tmp_path, capsys, monkeypatch, Source(listing()), "2026-11-20T00:00:00Z")
    assert code == 0 and "invalid JSON" in report["previous_snapshot_error"]
    assert json.loads((tmp_path / "state/prices.json").read_text())["fetched_at"] == "2026-11-20T00:00:00+00:00"


def test_an_unclaimed_price_snapshot_is_refused(tmp_path, capsys):
    (tmp_path / "state").mkdir()
    (tmp_path / "state/prices.json").write_text("{}", encoding="utf-8")
    assert main(["candidates", "--host", "claude", "--no-user-config", "--state-dir", str(tmp_path / "state"),
                 "--now", "2026-11-25T00:00:00Z", "--json"]) == 1
    assert "refusing unclaimed store ['prices.json']" in capsys.readouterr().err


def test_plan_ignores_prices_and_candidates_name_the_repair_for_a_bad_snapshot(tmp_path, capsys, monkeypatch, sleeps):
    write_claude_catalog(["claude-opus-5-5"])
    assert refresh(tmp_path, capsys, monkeypatch, Source(listing()), "2026-11-20T00:00:00Z")[0] == 0
    (tmp_path / "state/prices.json").write_text('{"format": "agroute-price-snapshot-v1"}', encoding="utf-8")
    router = Router(state_directory=tmp_path / "state", include_user_config=False)
    decision = {"decision_id": "bad-snapshot", "chosen": {"model": "claude/claude-opus-5-5", "effort": "high"}}
    plan = router.plan("claude", decision, include_agents=["claude"], now="2026-11-25T00:00:00Z")
    assert plan["resolved_target"]["model"] == "claude-opus-5-5"
    with pytest.raises(AgRouteError, match="missing keys.*rerun agroute prices refresh"):
        router.candidates("claude", include_agents=["claude"], now="2026-11-25T00:00:00Z")
