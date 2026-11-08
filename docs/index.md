# agroute documentation

- [Quickstart](quickstart.md) — install the Python backend and native plugin, filter candidates,
  resolve a `howhard` decision, and record feedback.
- [Routing and storage contracts](reference.md) — candidate qualification, native vs. CLI
  execution plans, quota window expiry, `host_affinity_epsilon`, and worktree state.
- [Configuration](configuration.md) — layered `agroute.toml`, `native_candidates` vs.
  `cli_candidates`, quota limits, and `howhard.toml` `[inventories]` generation.
- [Reusable skill and host installation](skills.md) — Codex, Claude Code, and Antigravity
  plugin discovery, native installation, and coordinating-agent instructions.
- [Contributor workflow](contributing.md) — issue tracking, worktrees, and coordinated checks.
- [Packaging and release](releasing.md) — versions, distribution validation, and publication.
- [Changelog](changelog.md) — user-facing changes by version.
- [Session handoff](session_handoff_format.md) — reporting changed work.

```{toctree}
:hidden:

quickstart
reference
configuration
skills
contributing
releasing
changelog
session_handoff_format
```
