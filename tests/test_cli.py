"""CLI boundary tests (#2, #3, #4, #5, #7)."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from agroute import __version__
from agroute.cli import _build_parser, main


def test_console_script_is_agr():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert project["project"]["scripts"] == {"agr": "agroute.cli:main"}


def test_cli_program_and_version(capsys):
    assert _build_parser().prog == "agr"
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"agr {__version__}"


def test_python_module_version_remains_available(tmp_path):
    source_root = Path(__file__).parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root / "src")
    result = subprocess.run(
        [sys.executable, "-m", "agroute", "--version"],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == f"agr {__version__}"


def test_cli_end_to_end_workflow(tmp_path, capsys):
    state_dir = tmp_path / "state"
    cands_out = tmp_path / "candidates.json"
    inv_out = tmp_path / "howhard-inventories.toml"
    dec_file = tmp_path / "decision.json"
    plan_out = tmp_path / "plan.json"

    # 1. paths
    assert main(["paths", "--state-dir", str(state_dir), "--no-user-config", "--json"]) == 0
    paths_payload = json.loads(capsys.readouterr().out)
    assert paths_payload["state_root"] == str(state_dir.resolve())

    # 2. usage set
    assert (
        main(
            [
                "usage",
                "set",
                "--agent",
                "codex",
                "--session-used-pct",
                "94.0",
                "--session-reset-at",
                "2026-11-08T22:00:00Z",
                "--state-dir",
                str(state_dir),
                "--no-user-config",
                "--now",
                "2026-11-08T17:00:00Z",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()

    # 3. candidates
    assert (
        main(
            [
                "candidates",
                "--host",
                "agy",
                "--output",
                str(cands_out),
                "--state-dir",
                str(state_dir),
                "--no-user-config",
                "--now",
                "2026-11-08T17:05:00Z",
                "--json",
            ]
        )
        == 0
    )
    cands_envelope = json.loads(capsys.readouterr().out)
    cands_list = json.loads(cands_out.read_text(encoding="utf-8"))
    assert cands_envelope["candidates"] == cands_list
    assert "price_request" in cands_envelope
    assert "credit_calibration" in cands_envelope
    assert not any(c["model"].startswith("codex/") for c in cands_list)

    # 3b. candidates without --json emits plain candidate JSON array for howhard --candidates
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
                "2026-11-08T17:05:00Z",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == cands_list

    # 4. inventory
    assert (
        main(
            [
                "inventory",
                "--host",
                "agy",
                "--name",
                "router",
                "--output",
                str(inv_out),
                "--state-dir",
                str(state_dir),
                "--no-user-config",
                "--now",
                "2026-11-08T17:05:00Z",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert "[inventories]" in inv_out.read_text(encoding="utf-8")

    # 5. plan
    dec_file.write_text(
        json.dumps(
            {
                "decision_id": "dec-cli-1",
                "chosen": {"model": "claude/claude-sonnet-4-6", "effort": "high"},
                "cap_met": True,
                "needs_review": False,
            }
        ),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "plan",
                "--host",
                "agy",
                "--decision",
                str(dec_file),
                "--prompt",
                "run test suite",
                "--output",
                str(plan_out),
                "--state-dir",
                str(state_dir),
                "--no-user-config",
                "--now",
                "2026-11-08T17:05:00Z",
                "--json",
            ]
        )
        == 0
    )
    plan_payload = json.loads(capsys.readouterr().out)
    assert plan_payload["execution_mode"] == "cli"
    assert json.loads(plan_out.read_text(encoding="utf-8")) == plan_payload

    # 6. Choosing a quota-blocked route in plan returns exit code 1
    dec_blocked = tmp_path / "decision-blocked.json"
    dec_blocked.write_text(
        json.dumps(
            {
                "decision_id": "dec-cli-blocked",
                "chosen": {"model": "codex/gpt-5.4", "effort": "high"},
            }
        ),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "plan",
                "--host",
                "agy",
                "--decision",
                str(dec_blocked),
                "--state-dir",
                str(state_dir),
                "--no-user-config",
                "--now",
                "2026-11-08T17:05:00Z",
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "not an eligible route" in err
