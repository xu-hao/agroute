# Changelog

## Unreleased

- Refresh missing, stale, and expired known quota evidence before routing, with bounded
  attempts and provider-specific failure exclusions. Partial reports cannot refresh other
  scopes; unreported windows stay unknown (#25).

## 0.2.2 — 2026-11-09

- Fix structured Claude model/surface scopes and skip malformed limit entries with
  warnings while applying recognized entries. Honor scoped and shared rejected limits,
  including events without percentages, with visible exclusion/reset reasons (#21).
- Reconcile newer quota/accepted evidence so restored plan access works before an old
  reset, preserve genuine measured counters, and distinguish old transcript replay from
  a new identical rejection. Expose probe failure times and configurable Claude usage
  staleness warnings; unknown percentages remain null (#21).

## 0.2.1 — 2026-11-09

- Emit every supported effort for every current model, in canonical effort order, and
  remove numeric model and inventory caps. Claude main/quick-select models, visible
  newest-generation Codex models, and all Antigravity CLI models remain eligible,
  subject to existing quota filters and explicit surface overrides. Low/max cells
  can now be resolved by `plan`; quota exclusions retain their reasons (#19).

## 0.2.0 — 2026-11-09

- Provide pre-rendered non-interactive CLI fallback in `agroute plan` execution plans and skill
  instructions (`skills/agroute/SKILL.md`) for immediate recovery when native subagent delegation
  fails at launch due to client concurrency or slot limits, with raw native attribution (#17).
- Refactor agent-specific candidate discovery, tool invocation planning, live quota telemetry
  discovery, zero-token probing, and quota bucket resolution into an extensible `AgentAdapter`
  interface with dynamic dispatch via `AgentRegistry` (`agroute.adapters`, `get_adapter`,
  `register_adapter`), replacing procedural branching across router, discovery, and usage modules (#15).
- Discover available models and supported reasoning efforts dynamically at runtime from locally
  installed agents (`~/.codex/models_cache.json` for Codex, `~/.claude/cache/model-catalog/`
  for Claude Code, and `agy models` for Antigravity) instead of shipping hardcoded model lists
  in `default_config.toml`, and expose `agroute models [--refresh] [--json]` (`Router.models()`) (#11).
- Add `agroute tui`, `agroute list`, and `agroute show <id>` to inspect realtime and historical
  model routes across repositories, correlating `<state_root>/routes.jsonl` with `.git/howhard/`
  decisions and outcomes alongside live agent quota and eligibility state (#13).
- Resolve per-model quota windows (`session_5h`, `weekly_7d`, `quota_group`, `eligible`, and
  `exclusion_reason` under `agents.<name>.models` in `agroute usage show`, `agroute models`,
  `agroute candidates`, and `agroute tui`) using zero-token built-in probes (`agy --output-format
  json -p /usage`, `claude -p /usage --output-format stream-json --verbose`, and `codex app-server`
  `account/rateLimits/read`), local session log discovery, `-p <prompt>` argument ordering for
  `agy`, and a writable sandbox state-root fallback when `.git/` is mounted read-only.

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
