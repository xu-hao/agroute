# agroute documentation

`agroute` routes delegated coding-agent tasks across Codex (`codex`), Claude Code (`claude`),
and Antigravity (`agy`) within live usage limits, integrating with `howhard` 2.1+ for
model/effort selection and learning.

```{toctree}
:maxdepth: 2

quickstart
reference
configuration
skills
contributing
releasing
session_handoff_format
changelog
```

- [Quickstart](quickstart.md) — install the Python backend and native plugin, filter candidates,
  resolve a `howhard` decision, and record feedback.
- [Routing and storage contracts](reference.md) — candidate qualification, native vs. CLI
  execution plans, quota window expiry, `host_affinity_epsilon`, and worktree state.
- [Configuration](configuration.md) — layered `agroute.toml`, `native_candidates` vs.
  `cli_candidates`, quota limits, and `howhard.toml` `[inventories]` generation.
- [Native skill installation](skills.md) — Codex, Claude Code, and Antigravity plugin
  installation and coordinating-agent instructions.
- [Contributor workflow](contributing.md) — issue-tracked Git worktrees and `agc` gates.
- [Packaging and release](releasing.md) — version synchronization, wheel verification, and PyPI
  release checklist.
- [Session handoff format](session_handoff_format.md) — four-part closing summary contract.
- [Changelog](changelog.md) — release history.
