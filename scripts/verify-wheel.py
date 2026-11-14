"""Check built artifacts at their packaging boundary, including the version source."""

from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import zipfile

expected = version("agroute")
wheel = Path("dist") / f"agroute-{expected}-py3-none-any.whl"
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
    assert "agroute/data/default_config.toml" in names
    assert "agroute/py.typed" in names
    assert not any(name.startswith("agroute/skills/") for name in names)
    metadata = archive.read(f"agroute-{expected}.dist-info/METADATA").decode()
    assert f"Version: {expected}\n" in metadata
    entrypoints = archive.read(f"agroute-{expected}.dist-info/entry_points.txt").decode()
    assert "agr = agroute.cli:main" in entrypoints
    assert "agroute = agroute.cli:main" not in entrypoints

sdist = Path("dist") / f"agroute-{expected}.tar.gz"
with tarfile.open(sdist) as archive:
    sdist_names = archive.getnames()
    assert f"agroute-{expected}/tests/conftest.py" in sdist_names
    for name in ("SKILL.md", "references/hosts.md", "references/workflow.md", "references/usage.md"):
        assert f"agroute-{expected}/skills/agroute/{name}" in sdist_names
    for name in ("plugin.json", ".claude-plugin/plugin.json", ".antigravity-plugin/plugin.json"):
        manifest = json.load(archive.extractfile(f"agroute-{expected}/{name}"))
        assert manifest["name"] == "agroute" and manifest["version"] == expected
    for name in (".agents/plugins/marketplace.json", ".claude-plugin/marketplace.json"):
        marketplace = json.load(archive.extractfile(f"agroute-{expected}/{name}"))
        assert marketplace["name"] == "agroute"
        assert [plugin["name"] for plugin in marketplace["plugins"]] == ["agroute"]

# Exercise an installed wheel outside the source checkout. No editable install or
# inherited PYTHONPATH can supply its resources or entry point.
with tempfile.TemporaryDirectory(prefix="agroute-wheel-") as directory:
    root = Path(directory)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("AGROUTE_STATE_DIR", None)
    environment.pop("AGROUTE_USER_CONFIG", None)
    codex_home = root / "codex-home"
    codex_home.mkdir()
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
                    }
                ]
            }
        )
    )
    claude_home = root / "claude-home"
    (claude_home / "cache" / "model-catalog").mkdir(parents=True)
    (claude_home / "cache" / "model-catalog" / "catalog.json").write_text(
        json.dumps(
            {
                "version": 2,
                "catalog": {
                    "config": {
                        "models": [
                            {
                                "id": "claude-sonnet-4-6",
                                "short_name": "Sonnet",
                                "section": "main",
                                "quick_select": True,
                                "runtime": {
                                    "effort_levels": ["medium", "high"],
                                    "default_effort": "medium",
                                },
                            }
                        ]
                    }
                },
            }
        )
    )
    spool_dir = root / "agroute-spool"
    spool_dir.mkdir()
    (spool_dir / "agy_models_cache.json").write_text(
        json.dumps(
            {
                "format": "agroute-agy-models-cache-v1",
                "fetched_at": 9999999999.0,
                "candidates": [
                    {"model": "gemini-2.5-pro", "effort": "high", "tier": "pro"},
                ],
            }
        )
    )
    environment["CODEX_HOME"] = str(codex_home)
    environment["CLAUDE_CONFIG_DIR"] = str(claude_home)
    environment["AGROUTE_SPOOL_DIR"] = str(spool_dir)
    subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(root / "venv")], check=True)
    python = root / "venv/bin/python"
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "--no-deps", str(wheel.resolve())],
        cwd=root,
        env=environment,
        check=True,
    )
    code = (
        "from importlib.resources import files; import agroute; "
        "assert files('agroute').joinpath('data/default_config.toml').is_file(); "
        "print(agroute.__version__)"
    )
    installed_version = subprocess.check_output(
        [str(python), "-c", code], cwd=root, env=environment, text=True
    ).strip()
    assert installed_version == expected

    command = root / "venv/bin/agr"
    assert command.is_file()
    assert not (root / "venv/bin/agroute").exists()
    assert (
        subprocess.check_output(
            [str(command), "--version"], cwd=root, env=environment, text=True
        ).strip()
        == f"agr {expected}"
    )
    assert (
        subprocess.check_output(
            [str(python), "-m", "agroute", "--version"], cwd=root, env=environment, text=True
        ).strip()
        == f"agr {expected}"
    )

    paths = json.loads(
        subprocess.check_output(
            [str(command), "paths", "--no-user-config", "--json"],
            cwd=root,
            env=environment,
            text=True,
        )
    )
    assert Path(paths["capture_root"]) == root.resolve()

    # Keep automatic quota refresh inside this owned fixture, including when an
    # installed provider CLI exists on the host. The marker files demonstrate that
    # the installed wheel really probed the missing AGY and Claude evidence.
    probe = root / "quota-probe.py"
    probe.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path(__file__).with_name('probe-' + sys.argv[1]).write_text('called')\n"
        "print('5h limit: 10% used (resets 2099-01-01T00:00:00Z)')\n"
    )
    (root / "agroute.toml").write_text(
        "\n".join(
            f"[agents.{agent}]\nusage_probe_cmd = "
            + json.dumps([str(python), str(probe), agent])
            for agent in ("agy", "claude", "codex")
        )
        + "\n"
    )

    # Ingest Codex rate_limits JSONL and verify candidate filtering + plan resolution
    codex_event = root / "codex.jsonl"
    codex_event.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {
                            "used_percent": 95.0,
                            "window_minutes": 300,
                            "resets_at": "2099-01-01T00:00:00Z",
                        }
                    },
                },
            }
        )
        + "\n"
    )
    subprocess.check_output(
        [
            str(command),
            "usage",
            "ingest",
            "--agent",
            "codex",
            "--file",
            str(codex_event),
            "--no-user-config",
            "--json",
        ],
        cwd=root,
        env=environment,
        text=True,
    )

    candidates_file = root / "candidates.json"
    cands = json.loads(
        subprocess.check_output(
            [
                str(command),
                "candidates",
                "--host",
                "agy",
                "--output",
                str(candidates_file),
                "--no-user-config",
                "--json",
            ],
            cwd=root,
            env=environment,
            text=True,
        )
    )
    models = [c["model"] for c in cands["candidates"]]
    assert "agy/gemini-2.5-pro" in models
    assert "claude/claude-sonnet-4-6" in models
    assert not any(m.startswith("codex/") for m in models)
    assert (root / "probe-agy").read_text() == "called"
    assert (root / "probe-claude").read_text() == "called"
    assert not (root / "probe-codex").exists()  # Fresh, exhausted evidence is reused.

    # Resolve both a native swarm plan (agy/gemini-2.5-pro) and a cross-agent CLI plan
    # (claude/claude-sonnet-4-6)
    decision_native = root / "decision-native.json"
    decision_native.write_text(
        json.dumps(
            {
                "decision_id": "dec-wheel-1",
                "chosen": {"model": "agy/gemini-2.5-pro", "effort": "high"},
                "cap_met": True,
                "needs_review": False,
            }
        )
    )
    plan_native = json.loads(
        subprocess.check_output(
            [
                str(command),
                "plan",
                "--host",
                "agy",
                "--decision",
                str(decision_native),
                "--prompt",
                "verify wheel",
                "--no-user-config",
                "--json",
            ],
            cwd=root,
            env=environment,
            text=True,
        )
    )
    assert plan_native["execution_mode"] == "native"
    assert plan_native["invocation"]["tool_name"] == "swarm"
    assert plan_native["invocation"]["parameters"]["action"] == "add"
    assert plan_native["invocation"]["parameters"]["model"] == "gemini-2.5-pro"
    assert plan_native["invocation"]["parameters"]["brief"] == "verify wheel"

    decision_cli = root / "decision-cli.json"
    decision_cli.write_text(
        json.dumps(
            {
                "decision_id": "dec-wheel-2",
                "chosen": {"model": "claude/claude-sonnet-4-6", "effort": "high"},
                "cap_met": True,
                "needs_review": False,
            }
        )
    )
    plan_cli = json.loads(
        subprocess.check_output(
            [
                str(command),
                "plan",
                "--host",
                "agy",
                "--decision",
                str(decision_cli),
                "--prompt",
                "verify cli",
                "--no-user-config",
                "--json",
            ],
            cwd=root,
            env=environment,
            text=True,
        )
    )
    assert plan_cli["execution_mode"] == "cli"
    assert plan_cli["invocation"]["argv"][:3] == ["claude", "-p", "--verbose"]

    listed = json.loads(
        subprocess.check_output(
            [str(command), "list", "--no-user-config", "--json"],
            cwd=root,
            env=environment,
            text=True,
        )
    )
    assert len(listed["routes"]) == 2
    shown = json.loads(
        subprocess.check_output(
            [str(command), "show", plan_cli["route_id"], "--no-user-config", "--json"],
            cwd=root,
            env=environment,
            text=True,
        )
    )
    assert shown["route_id"] == plan_cli["route_id"]

print(f"Verified agroute {expected} source distribution and clean installed wheel")
