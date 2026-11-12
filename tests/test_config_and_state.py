"""Tests for configuration validation and worktree-aware state ownership (#2)."""

import json
import subprocess
import pytest

from agroute import AgRouteError, Router, load_config


def test_default_config_loads_in_plain_directory(tmp_path):
    cfg = load_config(project=tmp_path)
    assert cfg.default_host == "agy"
    assert cfg.host_affinity_epsilon == 0.0
    assert cfg.namespace_separator == "/"
    assert cfg.capture_root == tmp_path.resolve()
    assert cfg.state_root == (tmp_path / ".agroute").resolve()
    assert set(cfg.agents) == {"codex", "claude", "agy"}


def test_layered_user_and_project_config_override(tmp_path, monkeypatch):
    user_cfg = tmp_path / "user-config.toml"
    user_cfg.write_text(
        """
[routing]
default_host = "codex"
host_affinity_epsilon = 0.25

[agents.claude]
max_session_used_pct = 80.0
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGROUTE_USER_CONFIG", str(user_cfg))

    proj_dir = tmp_path / "project"
    proj_dir.mkdir()
    proj_cfg = proj_dir / "agroute.toml"
    proj_cfg.write_text(
        """
[routing]
default_host = "claude"
namespace_separator = ":"
""",
        encoding="utf-8",
    )

    cfg = load_config(project=proj_dir)
    assert cfg.default_host == "claude"
    assert cfg.host_affinity_epsilon == 0.25
    assert cfg.namespace_separator == ":"
    assert cfg.agents["claude"]["max_session_used_pct"] == 80.0


@pytest.mark.parametrize(
    "bad_toml,match",
    [
        ("unknown_root = 1\n", "unknown top-level keys"),
        ("[routing]\nunknown_key = 1\n", "routing: unknown keys"),
        ('[routing]\ndefault_host = "aider"\n', "unsupported agent"),
        ("[routing]\nhost_affinity_epsilon = -0.1\n", "non-negative"),
        ('[routing]\nnamespace_separator = "."\n', "expected '/' or ':'"),
        ("[agents.codex]\nmax_session_used_pct = 105.0\n", "<= 100.0"),
        ("[agents.codex]\nhost_reserve_pct = 95.0\n", "cannot exceed"),
        (
            '[[agents.codex.cli_candidates]]\nmodel = "gpt-5.4"\neffort = "model_default"\n',
            "not an enforceable native effort",
        ),
        (
            '[[agents.codex.cli_candidates]]\nmodel = "codex/gpt-5.4"\neffort = "high"\n',
            "without agent prefix",
        ),
        (
            '[[agents.agy.native_candidates]]\nmodel = "Gemini 2.5 Pro"\neffort = "standard"\n',
            "Antigravity native model must be a concrete",
        ),
        (
            '[[agents.codex.cli_candidates]]\nmodel = "gpt-5.4"\neffort = "high"\n'
            '[[agents.codex.cli_candidates]]\nmodel = "gpt-5.4"\neffort = "high"\n',
            "duplicate candidate cell",
        ),
    ],
)
def test_invalid_config_is_refused(tmp_path, bad_toml, match):
    cfg_file = tmp_path / "agroute.toml"
    cfg_file.write_text(bad_toml, encoding="utf-8")
    with pytest.raises(AgRouteError, match=match):
        load_config(cfg_file)


def test_git_worktree_shares_common_state_and_identity(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True)

    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "issue-1", str(wt), "main"],
        check=True,
    )

    r_main = Router(project=repo)
    r_main.usage_set("codex", session_used_pct=55.0, now="2026-11-08T12:00:00Z")
    paths_main = r_main.paths()

    r_wt = Router(project=wt)
    paths_wt = r_wt.paths()
    assert paths_main["state_root"] == paths_wt["state_root"]
    assert paths_main["project_identity"] == paths_wt["project_identity"]
    assert paths_main["capture_root"] != paths_wt["capture_root"]

    snap = r_wt.usage_show("codex", now="2026-11-08T12:05:00Z")
    assert snap["agents"]["codex"]["session_5h"]["used_pct"] == 55.0


def test_cross_project_store_and_unrelated_directory_refused(tmp_path):
    p1 = tmp_path / "p1"
    p2 = tmp_path / "p2"
    shared_store = tmp_path / "store"
    p1.mkdir()
    p2.mkdir()

    r1 = Router(project=p1, state_directory=shared_store)
    r1.usage_set("codex", session_used_pct=20.0)

    r2 = Router(project=p2, state_directory=shared_store)
    with pytest.raises(AgRouteError, match="belongs to another project"):
        r2.usage_show()

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "random.txt").write_text("not a store\n", encoding="utf-8")
    with pytest.raises(AgRouteError, match="refusing unrelated existing directory"):
        Router(project=p1, state_directory=unrelated)

    with pytest.raises(AgRouteError, match="must not equal or contain"):
        Router(project=p1, state_directory=p1)
