"""Plan-time rate cards and `agr close` credit reconciliation with howhard observations (#128)."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from agroute import Router
from agroute.adapters import get_adapter
from agroute.calibration import _collect_telemetry_sources, _load_hh_observations
from agroute.cli import main
from agroute.route_store import read_route_records

PLAN_AT = "2026-11-21T01:00:00Z"

PRICED_CONFIG = """
[agents.codex]
plan = "plus-20"
host_reserve_pct = 0.0
[[agents.codex.native_candidates]]
model = "gpt-6.2-sol"
effort = "high"
[[agents.codex.cli_candidates]]
model = "gpt-6.2-sol"
effort = "high"
[[agents.codex.cli_candidates]]
model = "blended-codex"
effort = "high"
usd_per_mtok = 4.25

[[agents.agy.cli_candidates]]
model = "gemini-4-argon-high"
effort = "high"
[[agents.agy.cli_candidates]]
model = "blended-model"
effort = "high"
usd_per_mtok = 3.5
[[agents.agy.cli_candidates]]
model = "carded-model"
effort = "high"
[agents.agy.cli_candidates.rate_card]
format = "howhard-rate-card-v1"
source = "fixture://approved-contract"
version = "fixture-v1"
effective_at = "2026-11-07T00:00:00+00:00"
provider = "fixture-provider"
model = "agy/carded-model"
service_tier = "standard"
basis = "estimate"
[agents.agy.cli_candidates.rate_card.rates]
input = 2.0
output = 8.0
cache_read = 0.2
cache_write = 2.5
cache_write_5m = 2.5
cache_write_1h = 4.0
[[agents.agy.cli_candidates]]
model = "mystery-model"
effort = "high"
[[agents.agy.native_candidates]]
model = "claude-opus-5-5-medium"
effort = "medium"
[[agents.agy.native_candidates]]
model = "gemini-4-argon-high"
effort = "high"
[agents.agy.prices."gemini-4-neon-high"]
input = 1.0
output = 6.0
cache_read = 0.1
cache_write = 1.0
cache_write_5m = 1.0
cache_write_1h = 1.0
"""


@pytest.fixture(autouse=True)
def _no_ambient_howhard_store(monkeypatch):
    monkeypatch.delenv("HOWHARD_STATE_DIR", raising=False)


def _seed_repo(tmp_path: Path, config_text: str) -> tuple[Router, Path, Path, Path]:
    repo = tmp_path / "repo"
    hh_dir = repo / ".git" / "howhard"
    hh_dir.mkdir(parents=True)
    state_dir = repo / ".git" / "agroute"
    cfg = repo / "agroute.toml"
    cfg.write_text(config_text, encoding="utf-8")
    router = Router(cfg, project=repo, state_directory=state_dir, include_user_config=False)
    return router, cfg, state_dir, hh_dir


def _append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _decision(decision_id: str, model: str, effort: str = "high") -> dict:
    return {
        "decision_id": decision_id,
        "chosen": {"model": model, "effort": effort},
        "cap_met": True,
        "needs_review": False,
    }


def _stored_route(state_dir: Path, route_id: str) -> dict:
    (record,) = [
        r
        for r in read_route_records(state_dir)
        if r["format"] == "agroute-route-v1" and r["route_id"] == route_id
    ]
    return record


def _cli_json(capsys, cfg: Path, state_dir: Path, *argv: str) -> dict:
    capsys.readouterr()
    assert (
        main([*argv, "--config", str(cfg), "--state-dir", str(state_dir), "--no-user-config", "--json"])
        == 0
    )
    return json.loads(capsys.readouterr().out)


def _price_fields(mapping: dict) -> dict:
    return {key: mapping[key] for key in ("rate_card", "usd_per_mtok") if mapping.get(key) is not None}


@pytest.mark.parametrize(
    "model, priced_by",
    [
        ("agy/gemini-4-argon-high", "rate_card"),
        ("agy/carded-model", "rate_card"),
        ("agy/blended-model", "usd_per_mtok"),
        ("agy/mystery-model", None),
    ],
)
def test_plan_emits_and_persists_the_candidate_cell_price(tmp_path, capsys, model, priced_by):
    router, cfg, state_dir, _ = _seed_repo(tmp_path, PRICED_CONFIG)
    cell = next(
        c for c in router.candidates("codex", now=PLAN_AT)["candidates"] if c["model"] == model
    )
    assert list(_price_fields(cell)) == ([priced_by] if priced_by else [])

    decision_file = tmp_path / "decision.json"
    decision_file.write_text(json.dumps(_decision("d" * 64, model)), encoding="utf-8")
    plan = _cli_json(
        capsys, cfg, state_dir,
        "plan", "--host", "codex", "--decision", str(decision_file), "--now", PLAN_AT,
    )

    # `rate_card` is always present on a plan (null when the cell has no card).
    assert plan["rate_card"] == cell.get("rate_card")
    assert ("usd_per_mtok" in plan) == (priced_by == "usd_per_mtok")
    assert plan.get("usd_per_mtok") == cell.get("usd_per_mtok")
    stored = _stored_route(state_dir, plan["route_id"])
    assert {k: stored[k] for k in ("rate_card", "usd_per_mtok") if k in stored} == _price_fields(cell)
    shown = _cli_json(
        capsys, cfg, state_dir, "show", plan["route_id"], "--current-repo", "--now", PLAN_AT
    )
    assert _price_fields(shown) == _price_fields(cell)


def test_plan_rate_card_is_a_deep_copy_of_the_configured_candidate_card(tmp_path):
    router, _, state_dir, _ = _seed_repo(tmp_path, PRICED_CONFIG)
    configured = deepcopy(router.config.agents["agy"]["cli_candidates"])

    plan = router.plan("codex", _decision("a" * 64, "agy/carded-model"), now=PLAN_AT)
    plan["rate_card"]["rates"]["input"] = 999.0

    assert router.config.agents["agy"]["cli_candidates"] == configured
    assert _stored_route(state_dir, plan["route_id"])["rate_card"]["rates"]["input"] == 2.0
    again = router.plan("codex", _decision("b" * 64, "agy/carded-model"), now="2026-11-21T01:01:00Z")
    assert again["rate_card"]["rates"]["input"] == 2.0


def test_plan_freezes_the_rate_card_across_a_scheduled_catalog_cutover(tmp_path):
    router, _, _, _ = _seed_repo(tmp_path, PRICED_CONFIG)

    before = router.plan(
        "codex", _decision("a" * 64, "agy/gemini-4-argon-high"), now="2026-11-30T23:00:00Z"
    )
    after = router.plan(
        "codex", _decision("b" * 64, "agy/gemini-4-argon-high"), now="2026-12-02T00:00:00Z"
    )

    assert before["rate_card"]["rates"]["input"] == 2.0
    assert after["rate_card"]["rates"]["input"] == 4.0
    shown = router.show_route(before["route_id"], all_repos=False, now="2026-12-02T00:00:00Z")
    assert shown["rate_card"] == before["rate_card"]


def test_plan_with_credit_reservation_persists_the_rate_card(tmp_path):
    router, _, state_dir, _ = _seed_repo(tmp_path, PRICED_CONFIG)

    decision = _decision("a" * 64, "codex/gpt-6.2-sol")
    decision["chosen"]["needed_credit_usd"] = 0.5
    plan = router.plan("codex", decision, now=PLAN_AT)

    assert (state_dir / "reservations.json").is_file()
    assert plan["rate_card"]["model"] == "codex/gpt-6.2-sol"
    assert plan["rate_card"]["provider"] == "openai"
    assert _stored_route(state_dir, plan["route_id"])["rate_card"] == plan["rate_card"]


def test_unreadable_price_snapshot_never_blocks_planning_or_closing(tmp_path):
    router, _, state_dir, _ = _seed_repo(tmp_path, PRICED_CONFIG)
    first = router.plan("codex", _decision("a" * 64, "agy/gemini-4-argon-high"), now=PLAN_AT)
    (state_dir / "prices.json").write_text("{corrupt json", encoding="utf-8")

    # A catalog price depends on the unreadable snapshot, so the cell stays unpriced and says why.
    plan = router.plan("codex", _decision("b" * 64, "agy/gemini-4-argon-high"), now=PLAN_AT)
    assert plan["rate_card"] is None
    assert any("rate card unavailable" in w and "prices.json" in w for w in plan["warnings"])

    # A configured price does not depend on the snapshot.
    carded = router.plan("codex", _decision("c" * 64, "agy/carded-model"), now=PLAN_AT)
    assert carded["rate_card"]["version"] == "fixture-v1"
    assert not any("rate card unavailable" in w for w in carded["warnings"])

    closed = router.close_route(
        first["route_id"], switched_to="codex/gpt-6.2-sol", effort="high", now="2026-11-21T01:05:00Z"
    )
    assert any("rate card unavailable" in w for w in closed["warnings"])
    continuation = _stored_route(state_dir, closed["continuation_route_id"])
    assert "rate_card" not in continuation and "usd_per_mtok" not in continuation


def test_switched_continuation_routes_freeze_offline_prices_without_probing(
    tmp_path, capsys, monkeypatch
):
    router, cfg, state_dir, _ = _seed_repo(tmp_path, PRICED_CONFIG)
    first = router.plan(
        "agy", _decision("a" * 64, "agy/claude-opus-5-5-medium", "medium"), now=PLAN_AT
    )
    router.prices_resolve(
        {
            "models": {
                "resolved-model": {
                    "status": "priced",
                    "provider": "openai",
                    "source": "https://example.invalid/pricing",
                    "rates": {
                        "input": 7.0,
                        "output": 9.0,
                        "cache_read": 0.7,
                        "cache_write": 7.0,
                        "cache_write_5m": 7.0,
                        "cache_write_1h": 7.0,
                    },
                }
            }
        },
        now=PLAN_AT,
    )

    def offline_only(*args, **kwargs):
        raise AssertionError("closing a route must not probe usage or discover models")

    route_id = first["route_id"]
    expectations = [
        # (--switched-to, expected `(rate_card version prefix, input rate)`, expected blended price)
        ("agy/gemini-4-argon-high", ("google-cloud-list-2026-11-12", 2.0), None),
        ("agy/gemini-4-neon-high", ("user-config", 1.0), None),
        ("codex/gpt-6.2-sol", ("openai-list-2026-11-21", 2.0), None),
        ("codex/blended-codex", None, 4.25),
        ("codex/resolved-model", ("resolved-", 7.0), None),
        ("codex/unknown-model", None, None),
    ]
    for minute, (target, card, blended) in enumerate(expectations, start=1):
        # A close this long after any probe would be stale for routing; it stays offline.
        with monkeypatch.context() as offline:
            for agent in ("codex", "claude", "agy"):
                for method in ("probe_usage", "probe_plan_tier", "discover_live_usage_patch", "discover_candidates"):
                    offline.setattr(get_adapter(agent), method, offline_only)
            closed = router.close_route(
                route_id, switched_to=target, effort="high", now=f"2026-11-21T03:{minute:02d}:00Z"
            )
        route_id = closed["continuation_route_id"]
        assert "warnings" not in closed
        stored = _stored_route(state_dir, route_id)
        if card is None:
            assert "rate_card" not in stored, target
        else:
            assert stored["rate_card"]["version"].startswith(card[0]), target
            assert stored["rate_card"]["rates"]["input"] == card[1], target
            assert stored["rate_card"]["model"] == target == stored["howhard_cell"]["model"]
        assert stored.get("usd_per_mtok") == blended, target
        shown = _cli_json(
            capsys, cfg, state_dir,
            "show", route_id, "--current-repo", "--now", f"2026-11-21T03:{minute:02d}:30Z",
        )
        assert shown.get("rate_card") == stored.get("rate_card")
        assert shown.get("usd_per_mtok") == stored.get("usd_per_mtok")


def test_continuation_rate_card_model_follows_the_namespace_separator(tmp_path):
    router, _, state_dir, _ = _seed_repo(
        tmp_path, '[routing]\nnamespace_separator = ":"\n' + PRICED_CONFIG.replace("agy/carded-model", "agy:carded-model")
    )
    first = router.plan("codex", _decision("a" * 64, "agy:gemini-4-argon-high"), now=PLAN_AT)

    closed = router.close_route(
        first["route_id"], switched_to="codex:gpt-6.2-sol", effort="high", now="2026-11-21T01:05:00Z"
    )

    stored = _stored_route(state_dir, closed["continuation_route_id"])
    assert stored["howhard_cell"]["model"] == "codex:gpt-6.2-sol"
    assert stored["rate_card"]["model"] == "codex:gpt-6.2-sol"


# --- `agr close --credit-usd` reconciliation with howhard observations ---------------------

CODEX_CONFIG = """
[agents.codex]
plan = "plus-20"
host_reserve_pct = 0.0
[[agents.codex.native_candidates]]
model = "gpt-6.1-sol"
effort = "high"
"""
SESSION_PRIOR_USD = 20.0


def _calibration_interval(tmp_path: Path):
    """Anchor a codex 5h snapshot and plan one route inside the next interval."""
    router, cfg, state_dir, hh_dir = _seed_repo(tmp_path, CODEX_CONFIG)
    router.usage_set(
        "codex",
        session_used_pct=10.0,
        session_reset_at="2026-11-21T17:00:00Z",
        now="2026-11-21T12:00:00Z",
    )
    plan = router.plan(
        "codex", _decision("e" * 64, "codex/gpt-6.1-sol"), now="2026-11-21T12:02:00Z"
    )
    return router, cfg, state_dir, hh_dir, plan


def _observation(execution_id: str, decision_id: str | None, **measurements) -> dict:
    return {
        "id": execution_id,
        "decision_id": decision_id,
        "model": "codex/gpt-6.1-sol",
        "effort": "high",
        "outcome": "clean",
        "observed_at": "2026-11-21T12:06:00+00:00",
        **measurements,
    }


def _finish_interval(router: Router) -> dict:
    """Probe +10% 5h usage (`delta_frac = 0.10`) and return the calibrated 5h window."""
    router.usage_set(
        "codex",
        session_used_pct=20.0,
        session_reset_at="2026-11-21T17:00:00Z",
        now="2026-11-21T12:10:00Z",
    )
    agent = router.usage_show(agent="codex", now="2026-11-21T12:10:00Z")["agents"]["codex"]
    return agent["credit_calibration"]["shared"]["session_5h"]


def _expected_ceiling(interval_credit_usd: float) -> float:
    return (0.20 * SESSION_PRIOR_USD + interval_credit_usd) / (0.20 + 0.10)


@pytest.mark.parametrize("unpriced", [{}, {"credit_usd": 0.0}, {"credit_usd": None}])
def test_close_credit_backfills_an_unpriced_linked_native_observation(tmp_path, capsys, unpriced):
    router, cfg, state_dir, hh_dir, plan = _calibration_interval(tmp_path)
    _append_jsonl(hh_dir / "pending.jsonl", _observation("native-run", None, **unpriced))

    _cli_json(
        capsys, cfg, state_dir,
        "close", plan["route_id"], "--obs", "native-run", "--credit-usd", "1.5",
        "--now", "2026-11-21T12:07:00Z",
    )
    window = _finish_interval(router)

    assert window["matched_intervals"] == 1
    assert window["calibrated_credit_usd"] == pytest.approx(_expected_ceiling(1.5))


@pytest.mark.parametrize("status", ["done", "switched"])
def test_close_credit_backfills_an_unpriced_receipt_observation_without_a_duplicate(tmp_path, status):
    router, _, _, hh_dir, plan = _calibration_interval(tmp_path)
    _append_jsonl(hh_dir / "pending.jsonl", _observation("receipt-run", "e" * 64))

    router.close_route(plan["route_id"], status=status, credit_usd=1.5, now="2026-11-21T12:07:00Z")
    _, observations, _ = _collect_telemetry_sources(router.config)
    window = _finish_interval(router)

    assert [(o["id"], o["credit_usd"]) for o in observations] == [("receipt-run", 1.5)]
    assert window["matched_intervals"] == 1
    assert window["calibrated_credit_usd"] == pytest.approx(_expected_ceiling(1.5))


@pytest.mark.parametrize("link", [False, True], ids=["receipt", "linked"])
def test_measured_howhard_credit_is_preserved_and_never_double_counted(tmp_path, link):
    router, _, _, hh_dir, plan = _calibration_interval(tmp_path)
    _append_jsonl(
        hh_dir / "pending.jsonl",
        _observation("measured-run", None if link else "e" * 64, credit_usd=1.0),
    )

    router.close_route(
        plan["route_id"],
        obs="measured-run" if link else None,
        credit_usd=3.0,
        now="2026-11-21T12:07:00Z",
    )
    window = _finish_interval(router)

    assert window["matched_intervals"] == 1
    assert window["calibrated_credit_usd"] == pytest.approx(_expected_ceiling(1.0))


def test_close_credit_backfill_never_mutates_cached_howhard_observations(tmp_path):
    router, _, _, hh_dir, plan = _calibration_interval(tmp_path)
    _append_jsonl(hh_dir / "pending.jsonl", _observation("receipt-run", "e" * 64))

    for credit, minute in ((1.5, 7), (2.5, 8)):
        # A later close corrects the credit while the howhard files stay unchanged.
        router.close_route(plan["route_id"], credit_usd=credit, now=f"2026-11-21T12:0{minute}:00Z")
        by_decision, observations, _ = _collect_telemetry_sources(router.config)
        assert [o["credit_usd"] for o in observations] == [credit]
        assert by_decision["e" * 64]["credit_usd"] == credit
        cached_by_decision, cached = _load_hh_observations(hh_dir.resolve())
        assert [o["credit_usd"] for o in cached] == [None]
        assert cached_by_decision["e" * 64]["credit_usd"] is None


def test_cancelled_close_credit_is_not_reconciled(tmp_path):
    router, _, _, hh_dir, plan = _calibration_interval(tmp_path)
    _append_jsonl(hh_dir / "pending.jsonl", _observation("receipt-run", "e" * 64))

    router.close_route(
        plan["route_id"], status="cancelled", credit_usd=1.5, now="2026-11-21T12:07:00Z"
    )
    _, observations, _ = _collect_telemetry_sources(router.config)

    assert [(o["id"], o["credit_usd"]) for o in observations] == [("receipt-run", None)]


@pytest.mark.parametrize("measured, counted", [({}, 1.5), ({"credit_usd": 1.0}, 1.0)])
def test_execution_id_reused_by_another_howhard_store_keeps_the_receipt_observation(
    tmp_path, monkeypatch, measured, counted
):
    router, _, _, hh_dir, plan = _calibration_interval(tmp_path)
    other_store = tmp_path / "shared-howhard"
    other_store.mkdir()
    monkeypatch.setenv("HOWHARD_STATE_DIR", str(other_store))  # read before the repository's store
    _append_jsonl(other_store / "pending.jsonl", _observation("reused-id", "e" * 64, **measured))
    # An unrelated execution on another agent reuses the human-readable `--id`.
    _append_jsonl(
        hh_dir / "pending.jsonl",
        {**_observation("reused-id", "f" * 64, credit_usd=0.4), "model": "claude/claude-sonnet-5-5"},
    )

    router.close_route(plan["route_id"], credit_usd=1.5, now="2026-11-21T12:07:00Z")
    window = _finish_interval(router)

    assert window["matched_intervals"] == 1
    assert window["calibrated_credit_usd"] == pytest.approx(_expected_ceiling(counted))
