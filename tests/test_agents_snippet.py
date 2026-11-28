"""Public contracts for the root `AGENTS.snippet.md` delegation trigger."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

import agroute

ROOT = Path(__file__).resolve().parents[1]
SNIPPET = ROOT / "AGENTS.snippet.md"


def _child_environment(home: Path) -> dict[str, str]:
    """Return an environment whose subprocesses cannot reach the real user home."""
    home.mkdir()
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("DIST_EXTRA_CONFIG", None)
    environment["HOME"] = str(home)
    return environment


def test_snippet_is_a_concise_trigger_without_delimiters():
    text = SNIPPET.read_text(encoding="utf-8")
    lines = text.splitlines()

    assert 3 <= len(lines) <= 4
    assert all(line.strip() and len(line) <= 120 for line in lines)
    assert text.endswith("\n")
    # People and tooling paste or splice the whole file, so it must not bring its
    # own fences, headings, front matter or begin/end comment markers.
    for line in lines:
        assert not line.lstrip().startswith(("```", "~~~", "---", "+++", "#"))
        assert "<!--" not in line and "-->" not in line
    # Trigger only: exact commands and flags stay in skills/agroute/SKILL.md.
    assert not re.search(r"(?<![\w-])--[a-z]", text)
    assert not re.search(r"\bagr [a-z]", text)


def test_source_distribution_ships_the_snippet_verbatim(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(
        ROOT,
        source,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".git",
            ".worktrees",
            ".venv*",
            ".agroute",
            ".pytest_cache",
            "__pycache__",
            "*.egg-info",
            "build",
            "dist",
            "_build",
        ),
    )
    outdir = tmp_path / "dist"

    result = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--no-isolation", "--outdir", str(outdir), str(source)],
        cwd=tmp_path,
        env=_child_environment(tmp_path / "home"),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    top = f"agroute-{agroute.__version__}"
    with tarfile.open(outdir / f"{top}.tar.gz") as archive:
        assert f"{top}/AGENTS.snippet.md" in archive.getnames()
        assert archive.extractfile(f"{top}/AGENTS.snippet.md").read() == SNIPPET.read_bytes()


def test_strict_antigravity_payload_includes_the_snippet(tmp_path):
    # Stand-ins are the only executables on PATH, so no installed host CLI can run.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    recorded = tmp_path / "strict-payload.json"
    validator = (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[0].endswith('agy') and '--strict' in sys.argv:\n"
        "    snippet = Path(sys.argv[3]) / 'AGENTS.snippet.md'\n"
        "    content = snippet.read_text(encoding='utf-8') if snippet.is_file() else None\n"
        "    Path(os.environ['RECORDED_PAYLOAD']).write_text(json.dumps({'snippet': content}))\n"
        "print(json.dumps({'findings': [], 'components': {'skills': 1}}))\n"
    )
    for name in ("claude", "agy"):
        executable = fake_bin / name
        executable.write_text(validator, encoding="utf-8")
        executable.chmod(0o755)
    environment = _child_environment(tmp_path / "home")
    environment["PATH"] = str(fake_bin)
    environment["TMPDIR"] = str(tmp_path)
    environment["RECORDED_PAYLOAD"] = str(recorded)

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check-plugins.py")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(recorded.read_text(encoding="utf-8"))
    assert payload["snippet"] == SNIPPET.read_text(encoding="utf-8")
