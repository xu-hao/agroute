# Changelog

## Unreleased

- Discover available models and supported reasoning efforts dynamically at runtime from locally
  installed agents (`~/.codex/models_cache.json` for Codex, `~/.claude/cache/model-catalog/`
  for Claude Code, and `agy models` for Antigravity) instead of shipping hardcoded model lists
  in `default_config.toml`, and expose `agroute models [--refresh] [--json]` (`Router.models()`).
- Add `agroute tui`, `agroute list`, and `agroute show <id>` to inspect realtime and historical
  model routes across repositories, correlating `<state_root>/routes.jsonl` with `.git/howhard/`
  decisions and outcomes alongside live agent quota and eligibility state.

## 0.1.0 — 2026-11-08

- Build `agroute` (`agroute` CLI, Python `Router` API, and native plugin manifests for
  Codex, Claude Code, and Antigravity) to route delegated tasks across local coding agents
  within live usage limits.
- Track per-agent (`codex`, `claude`, `agy`) quota windows (`session_5h`, `weekly_7d`, and
  model-tier limits) and rate-limit cooldowns from native command and event-stream outputs
  (`agroute usage show|set|ingest|probe|clear`) with automatic reset-window expiration.
- Emit `howhard` 2.1+-compatible candidate lists (`agroute candidates`) and named
  `[inventories]` (`agroute inventory`) with provider-qualified `<agent>/<model>` identities,
  separating `native_candidates` (when `target == host`) from `cli_candidates` (when
  `target != host`).
- Resolve `howhard` decision receipts (`agroute plan`) into deterministic
  `agroute-execution-plan-v1` payloads (`invoke_subagent`, `Agent`, or `spawn_agent` when
  `target == host`; `codex exec`, `claude -p`, or `agy -p` when `target != host`), with
  configurable `host_affinity_epsilon` and explicit `howhard observe` feedback guidance.
