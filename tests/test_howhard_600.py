"""howhard 6.0.0 alignment: candidate agent labels, v11 receipts and credit_p80_usd (#135)."""

from __future__ import annotations

import json
import tomllib

import pytest

from agroute import AgRouteError, Router
from agroute.cli import main
from agroute.router import _decision_has_credit_cost_split
from agroute.routes import _JSON_CACHE
from agroute.usage import read_reservations

NOW = "2026-11-27T12:00:00Z"
CODEX = ("codex/gpt-6.2-sol", "high")
NATIVE = ("agy/gemini-4-carbon-high", "high")


@pytest.fixture(autouse=True)
def _own_route_json_cache():
    """The route reader's JSON cache is process-wide; drop what this module's stores put in it."""
    yield
    _JSON_CACHE.clear()


def _require_howhard():
    return pytest.importorskip("howhard", minversion="6.0.0")


def _router(tmp_path, *, epsilon: float = 0.0, separator: str | None = None) -> Router:
    routing = f"[routing]\nhost_affinity_epsilon = {epsilon}\n"
    if separator is not None:
        routing += f'namespace_separator = "{separator}"\n'
    cfg = tmp_path / "agroute.toml"
    cfg.write_text(
        routing
        + '[agents.codex]\nplan = "plus-20"\n\n'
        '[[agents.codex.cli_candidates]]\nmodel = "gpt-6.2-sol"\neffort = "high"\nusd_per_mtok = 20.0\n\n'
        '[agents.agy]\nplan = "pro-20"\n\n'
        '[[agents.agy.native_candidates]]\nmodel = "gemini-4-carbon-high"\neffort = "high"\nusd_per_mtok = 10.0\n\n'
        "[agents.claude]\nenabled = false\n",
        encoding="utf-8",
    )
    router = Router(cfg, project=tmp_path, state_directory=tmp_path / "state", include_user_config=False)
    for agent in ("codex", "agy"):
        router.usage_set(agent, session_used_pct=10.0, weekly_used_pct=10.0, now=NOW)
    return router


def _available(router: Router) -> dict[str, float]:
    return {
        cell["model"]: cell["credit_budget"]["available_credit_usd"]
        for cell in router.candidates("agy", now=NOW)["candidates"]
    }


def _cell(key, *, needed=None, p80=None, score=1.0, **extra) -> dict:
    cell = {"model": key[0], "effort": key[1], "J": score, "admissible": True, **extra}
    if needed is not None:
        cell["needed_credit_usd"] = needed
    if p80 is not None:
        cell["credit_p80_usd"] = p80
    return cell


def _decision(decision_id: str, *cells: dict, chosen: dict | None = None) -> dict:
    first = cells[0] if cells else chosen
    return {
        "format": "howhard-decision-v11",
        "decision_id": decision_id,
        "chosen": chosen or {"model": first["model"], "effort": first["effort"]},
        "cap_met": True,
        "needs_review": False,
        "cells": list(cells),
    }


@pytest.mark.parametrize("separator", ["/", ":"])
def test_candidates_and_inventories_label_each_cell_with_its_agent(tmp_path, capsys, separator):
    router = _router(tmp_path, separator=separator)
    cells = router.candidates("agy", now=NOW)["candidates"]
    expected = {f"agy{separator}gemini-4-carbon-high": "agy", f"codex{separator}gpt-6.2-sol": "codex"}
    assert {cell["model"]: cell.get("agent") for cell in cells} == expected

    assert router.inventory("agy", now=NOW)["inventories"]["router"] == cells
    toml_cells = tomllib.loads(router.format_inventory_toml("agy", now=NOW))["inventories"]["router"]
    assert toml_cells == cells

    flags = ["--host", "agy", "--config", str(tmp_path / "agroute.toml"), "--state-dir", str(tmp_path / "state"),
             "--no-user-config", "--now", NOW]
    assert main(["candidates", *flags]) == 0
    assert json.loads(capsys.readouterr().out) == cells


def test_howhard_6_accepts_labelled_json_and_toml_inventories(tmp_path):
    howhard = _require_howhard()
    from howhard.native import validate

    router = _router(tmp_path)
    cells = router.candidates("agy", now=NOW)["candidates"]
    toml_cells = tomllib.loads(router.format_inventory_toml("agy", now=NOW))["inventories"]["router"]
    for inventory in (cells, toml_cells):
        assert {c["model"]: c.get("agent") for c in validate(inventory)} == {NATIVE[0]: "agy", CODEX[0]: "codex"}

    engine = howhard.Engine(directory=tmp_path / "hh-project")
    pending = engine.prepare("task", "Label the routes", risk="routine", native_candidates=cells, host="agy")
    assert pending["host"] == "agy"


def test_v11_format_never_reads_legacy_c_run_as_credit(tmp_path):
    assert _decision_has_credit_cost_split({"format": "howhard-decision-v11"}) is True

    router = _router(tmp_path)
    decision = {
        "format": "howhard-decision-v11",
        "decision_id": "dec-v11-bare",
        "chosen": {"model": CODEX[0], "effort": CODEX[1], "C_run": 999.0},
    }
    plan = router.plan("agy", decision, prompt="no legacy credit", now=NOW)
    assert "needed_credit_usd" not in plan
    assert "credit_p80_usd" not in plan


@pytest.mark.parametrize("location", ["cells", "selection", "chosen"])
def test_plan_refuses_when_credit_p80_exceeds_available_credit(tmp_path, location):
    router = _router(tmp_path)
    available = _available(router)[CODEX[0]]
    cell = _cell(CODEX, needed=available * 0.5, p80=available + 1.0)
    decision = _decision("dec-p80-refused", cell)
    if location == "selection":
        decision["selection"] = {"cells": decision.pop("cells")}
    elif location == "chosen":
        decision["chosen"] = dict(cell)
        del decision["cells"]

    with pytest.raises(AgRouteError) as refused:
        router.plan("agy", decision, prompt="too wide", now=NOW)
    message = str(refused.value)
    assert f"requires {available + 1.0:.6g} USD credit (credit_p80_usd)" in message
    assert f"exceeding remaining credit {available:.6g} USD" in message
    assert read_reservations(router.config) == []
    assert router.list_routes(all_repos=False, now=NOW)["routes"] == []


@pytest.mark.parametrize("p80_factor", [None, 1.0])
def test_plan_keeps_needed_credit_message_when_the_average_binds(tmp_path, p80_factor):
    router = _router(tmp_path)
    available = _available(router)[CODEX[0]]
    needed = available + 2.0
    cell = _cell(CODEX, needed=needed, p80=None if p80_factor is None else needed * p80_factor)

    with pytest.raises(AgRouteError) as refused:
        router.plan("agy", _decision("dec-mean-refused", cell), prompt="too big", now=NOW)
    message = str(refused.value)
    assert f"requires {needed:.6g} USD credit, exceeding remaining credit" in message
    assert "credit_p80_usd" not in message


def test_plan_checks_a_p80_that_arrives_without_needed_credit_and_reserves_it(tmp_path):
    router = _router(tmp_path)
    available = _available(router)[CODEX[0]]
    with pytest.raises(AgRouteError, match=r"\(credit_p80_usd\)"):
        router.plan("agy", _decision("dec-p80-only-big", _cell(CODEX, p80=available + 1.0)), prompt="a", now=NOW)

    plan = router.plan("agy", _decision("dec-p80-only", _cell(CODEX, p80=available * 0.25)), prompt="b", now=NOW)
    assert "needed_credit_usd" not in plan
    assert plan["credit_p80_usd"] == pytest.approx(available * 0.25)
    [reservation] = read_reservations(router.config)
    assert reservation["reserved_credit_usd"] == pytest.approx(available * 0.25)


@pytest.mark.parametrize("invalid", [None, -1.0, float("inf"), float("nan"), True, "9999"])
def test_plan_ignores_a_credit_p80_that_is_not_a_finite_nonnegative_number(tmp_path, invalid):
    router = _router(tmp_path)
    cell = _cell(CODEX, needed=0.5)
    cell["credit_p80_usd"] = invalid

    plan = router.plan("agy", _decision("dec-p80-invalid", cell), prompt="ignore", now=NOW)
    assert plan["needed_credit_usd"] == 0.5
    assert "credit_p80_usd" not in plan


def test_admitted_plan_reserves_the_average_and_records_the_p80(tmp_path):
    router = _router(tmp_path)
    available = _available(router)[CODEX[0]]
    needed, p80 = available * 0.25, available * 0.75

    plan = router.plan("agy", _decision("dec-admitted", _cell(CODEX, needed=needed, p80=p80)), prompt="fits", now=NOW)

    assert plan["format"] == "agroute-execution-plan-v1"
    assert plan["needed_credit_usd"] == pytest.approx(needed)
    assert plan["credit_p80_usd"] == pytest.approx(p80)
    [reservation] = read_reservations(router.config)
    assert reservation["reserved_credit_usd"] == pytest.approx(needed, abs=1e-6)
    assert _available(router)[CODEX[0]] == pytest.approx(available - needed, abs=1e-5)

    from agroute.route_store import read_route_records

    [record] = read_route_records(router.config.state_root)
    assert record["format"] == "agroute-route-v1"
    assert record["credit_p80_usd"] == pytest.approx(p80)
    [listed] = router.list_routes(all_repos=False, now=NOW)["routes"]
    assert listed["credit_p80_usd"] == pytest.approx(p80)
    assert router.show_route(plan["route_id"], all_repos=False, now=NOW)["credit_p80_usd"] == pytest.approx(p80)

    # The live recheck counts the in-flight average: a second route whose average still fits
    # is refused once its 80% point no longer does.
    with pytest.raises(AgRouteError, match=r"\(credit_p80_usd\).*reserved_credit_usd"):
        router.plan("agy", _decision("dec-second", _cell(CODEX, needed=needed, p80=p80 + 0.01)),
                    prompt="no longer fits", now=NOW)


def test_host_affinity_skips_a_native_cell_whose_credit_p80_does_not_fit(tmp_path):
    router = _router(tmp_path, epsilon=0.5)
    available = _available(router)
    decision = _decision(
        "dec-affinity-blocked",
        _cell(CODEX, needed=0.5, p80=0.75, score=1.0),
        _cell(NATIVE, needed=available[NATIVE[0]] * 0.5, p80=available[NATIVE[0]] + 1.0, score=1.1),
    )

    plan = router.plan("agy", decision, prompt="keep the cross-agent route", now=NOW)

    assert plan["host_affinity_applied"] is False
    assert plan["target_agent"] == "codex"
    assert plan["needed_credit_usd"] == 0.5
    assert plan["credit_p80_usd"] == 0.75
    assert plan["feedback"]["mode"] == "matched_receipt"


@pytest.mark.parametrize("native_p80", [1.25, None])
def test_host_affinity_override_carries_the_native_cell_credit_p80(tmp_path, native_p80):
    router = _router(tmp_path, epsilon=0.5)
    decision = _decision(
        "dec-affinity-applied",
        _cell(CODEX, needed=0.5, p80=0.75, score=1.0),
        _cell(NATIVE, needed=1.0, p80=native_p80, score=1.1),
    )

    plan = router.plan("agy", decision, prompt="use the native route", now=NOW)

    assert plan["host_affinity_applied"] is True
    assert plan["target_agent"] == "agy"
    assert plan["needed_credit_usd"] == 1.0
    assert plan.get("credit_p80_usd") == native_p80
    [reservation] = read_reservations(router.config)
    assert reservation["reserved_credit_usd"] == 1.0


class _ForecastLLM:
    """Deterministic stand-in for the reader model behind a real howhard 6 engine."""

    identity = "howhard-600-routing-fixture"

    def complete(self, request):
        if request["kind"] == "filter":
            order = sorted(cell["key"] for cell in request["cells"])
            return {
                "filter": {"order": order, "lower": order[0], "upper": order[-1]},
                "memory_filters": [],
                "reason": "synthetic inventory",
            }
        if request["kind"] == "learn":
            return {"operations": [], "reason": "no memory updates"}
        if request["phase"] == "baseline":
            return {
                "forecasts": [
                    {"family": c["family"], "effort": c["effort"], "clean": 0.98, "loud": 0.01, "silent": 0.01,
                     "tokens": 10_000.0, "seconds": 10.0}
                    for c in request["cells"]
                ],
                "reason": "identical synthetic forecasts",
            }
        return {"memory_forecasts": [], "reason": "no memories"}


def test_real_howhard_v11_receipt_loads_from_payloads_sqlite(tmp_path, capsys):
    howhard = _require_howhard()
    router = _router(tmp_path)
    cells = router.candidates("agy", now=NOW)["candidates"]
    # howhard keeps its store in <project>/.howhard, where agroute looks it up.
    engine = howhard.Engine(directory=tmp_path, llm=_ForecastLLM())
    result = engine.pick("task", task_text="Route with howhard 6", risk="routine", native_candidates=cells,
                         host="agy", seed=7)
    decision_id = result["decision_id"]
    assert (tmp_path / ".howhard" / "payloads.sqlite3").is_file()

    from howhard.decisions import load

    receipt = load(engine.config, decision_id)
    assert receipt["format"] == "howhard-decision-v11"
    chosen = receipt["chosen"]
    [row] = [r for r in receipt["selection"]["cells"]
             if (r["model"], r["effort"]) == (chosen["model"], chosen["effort"])]

    flags = ["--config", str(tmp_path / "agroute.toml"), "--state-dir", str(tmp_path / "state"),
             "--no-user-config", "--now", NOW, "--json"]
    assert main(["plan", "--host", "agy", "--decision", decision_id, "--prompt", "run it", *flags]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["decision_id"] == decision_id
    assert plan["needed_credit_usd"] == pytest.approx(row["needed_credit_usd"])
    assert plan["credit_p80_usd"] == pytest.approx(row["credit_p80_usd"])
    [reservation] = read_reservations(router.config)
    assert reservation["reserved_credit_usd"] == pytest.approx(row["needed_credit_usd"], abs=1e-6)

    for command in (["show", plan["route_id"]], ["list"]):
        assert main([*command, "--current-repo", *flags]) == 0
        output = json.loads(capsys.readouterr().out)
        [shown] = output["routes"] if command == ["list"] else [output]
        assert shown["credit_p80_usd"] == pytest.approx(row["credit_p80_usd"])
        [forecast] = [f for f in shown["forecasts"]
                      if (f["model"], f["effort"]) == (chosen["model"], chosen["effort"])]
        assert forecast["credit_p80_usd"] == pytest.approx(row["credit_p80_usd"])
        assert forecast["tier"] == row["tier"]
        assert forecast["pi"] == pytest.approx(row["pi"])


def test_direct_howhard_receipt_surfaces_p80_and_summary_keeps_host_and_agent(tmp_path):
    from conftest import write_howhard_payload_decision

    from agroute.routes import _summarize_decision

    router = _router(tmp_path)
    cell = _cell(CODEX, needed=1.0, p80=1.5, agent="codex", tier="on-trial", pi=0.4)
    document = _decision("d" * 64, cell)
    document["host"] = "agy"

    summary = _summarize_decision(document)
    assert summary["host"] == "agy"
    for kept in (summary["cells"][0], summary["forecasts"][0]):
        assert (kept["agent"], kept["credit_p80_usd"], kept["tier"], kept["pi"]) == ("codex", 1.5, "on-trial", 0.4)
    # Receipts with no named host add no host key.
    del document["host"]
    assert "host" not in _summarize_decision(document)

    write_howhard_payload_decision(tmp_path / ".howhard", "d" * 64, document, created_at=NOW)
    (tmp_path / ".howhard" / "decision-events.jsonl").write_text(
        json.dumps({"at_utc": NOW, "decision_id": "d" * 64, "task_type": "task"}) + "\n", encoding="utf-8"
    )
    [route] = router.list_routes(all_repos=False, now=NOW)["routes"]
    assert route["route_id"] == "hh-" + "d" * 12
    assert route["credit_p80_usd"] == 1.5
