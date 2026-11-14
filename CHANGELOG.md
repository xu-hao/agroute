# Changelog

## Unreleased

- Rename the installed console command to `agr`, while keeping the Python package/module
  and plugin name `agroute`. Issue #58 tracks this installed-command rename and the
  compatibility boundaries that remain unchanged.

- Remove the routing logo's center dot while preserving its branches and endpoint
  dots (#56).

- Restore the routing logo's three first-level branches and split only the middle
  branch into three more, retaining its abstract black-and-white style (#54).

- Extend the branching-route logo to two levels: one input, two routing nodes and
  four destinations, retaining its geometric black-and-white styling (#52).

- Separate subscription cash costs (`monthly_cost_usd`, amortized `weekly_cost_usd`, and
  `cost_per_credit`) from quota-credit ceilings and budgets; add Codex `pro-500` / `promax`,
  emit the `howhard-decision-v7` credit budget schema, and migrate calibration state to explicit
  credit names. Issue #50 tracks this cost-versus-credit contract across configuration, routing,
  calibration, user interfaces, and documentation.
- Add a geometric black-and-white branching-route logo to the README and docs, with
  asset provenance. Group documentation into Getting started, Technical deep dive,
  Reference and Project; add a technical report and real-work evaluation guide,
  clarify overlapping quota pools, and update the quickstart's howhard command and
  price-resolution guidance (#48).

## 0.4.0 — 2026-11-13

- Enforce all overlapping quota pools generically via `AgentAdapter.resolve_quota_pools`
  (returning primary plus overlapping `shared` pool on `ClaudeAdapter`), enforcing
  `exclude_quota_groups`, window rejections, and session/weekly usage caps across every
  consumed pool in `resolve_model_quota` and capping `credit_budget` to the tightest
  `remaining_usd` across all consumed pools in `resolve_calibrated_quota_credit` (#43).
- Replace third-party network price snapshots with a 100% offline calling-agent pricing
  callback (`price_request` in `candidates --json`, `inventory --json`, and
  `agroute prices pending`, fulfilled via `agroute prices resolve --file <path>` and inspected
  via `agroute prices show` in `<state_root>/prices.json` `agroute-price-snapshot-v2` with
  `"priced"` [30d TTL], `"alias"` [30d TTL], and `"unlisted"` [7d TTL]), preserve model
  `display_name` during discovery, emit a plain JSON candidate list on `agroute candidates`
  without `--json` for `howhard --candidates`, add built-in `gemini-4-hydrogen`
  (`$1.50 / $7.50 / $0.075` per 1M tokens) and `openai-oss` `gpt-oss-120b` pricing, and clamp
  Antigravity `swarm` worker `name` slugs to at most 24 characters without trailing hyphens (#41).
- Calibrate 5-hour (`session_5h`) and weekly (`weekly_7d`) subscription credit-pool ceilings
  empirically per `(agent, effective_plan, pool_group, window)` in
  `<state_root>/credit_calibration.json` by matching non-resetting quota intervals
  (`delta_used_pct >= 0.25`, `<600s` `reset_at` jitter tolerance) against completed `howhard`
  observations (`cost_usd > 0`, no straddling in-flight routes) using empirical Bayes shrinkage
  (`F0 = 0.20`, `[0.1 * C_prior, 10.0 * C_prior]` outlier filter, `F_max = 2.0` forgetting cap)
  while keeping explicit `[agents.<name>].credit` overrides exact and surfacing calibration
  metadata in `usage show`, `candidates`, and `inventory` (#41).
- Auto-detect native subscription plans (`plan = "auto"` default across `agy`, `claude`, and
  `codex`), expand `agy` presets with `"ultra-100"`, `"plus-8"`, and `"free"`, persist
  `detected_plan` in `usage.json` and the shared probe cache (from Antigravity `statusLine`
  `plan_tier`, Claude `claude auth status` `planTier` / `subscriptionType`, and Codex
  `rateLimits.planType`), expose `detected_plan` and `effective_plan` in `usage show` and
  `models`, and add `agroute config show` and `agroute config set-plan <agent> <plan> [--user | --project]` (#39).
- Add subscription plan presets (`plan = "pro-20" | "ultra-200"` on `agy`,
  `"pro-20" | "max-5x" | "max-20x"` on `claude`, and `"plus-20" | "pro-200"` on `codex`,
  with aliases), optional `[agents.<name>.credit]` overrides, and `exclude_quota_groups`
  (such as `["3p"]`) to `[agents.<name>]` configuration (#37).
- Compute remaining schedulable credit across both 5-hour (`session_5h`) and weekly
  (`weekly_7d`) quota windows (`available_usd = min(rem_5h, rem_7d)` after `host_reserve_pct`),
  attach `credit_budget` to `resolve_model_quota`, `usage show`, `models`, `candidates`, and
  `inventory`, exclude exhausted (`available_usd <= 0`) or unpriced models when `credit_budget`
  is active, and verify `needed_credit_usd <= available_usd` against live quota state in
  `agroute plan` (#37).

## 0.3.1 — 2026-11-12

- Delegate Antigravity native routes through the `swarm` tool at the selected model and
  effort (#33). With `--host agy`, `candidates` now offers every `agy models` cell natively
  (for example `agy/claude-opus-5-5-high` at `high`) instead of the abstract `agy/pro`,
  `agy/flash` and `agy/flash_lite` tiers, and `plan` emits
  `swarm{action: "add", name, model, workspace, brief}` for a concrete slug. A tier listed
  explicitly in `native_candidates` still plans `invoke_subagent`, which has no effort
  control. Explicit Antigravity `native_candidates` may now be concrete model slugs.
- Replace an Antigravity agent's stored quota-window set when a complete `/usage` report
  arrives (`status: "SUCCESS"` with quota evidence on every group and bucket), dropping
  removed plan tiers such as `argon`, `argon-weekly`, and `gemini-argon-wtu-weekly` across a
  plan change so Argon models resolve against the combined Gemini quota (`gemini-weekly` /
  `gemini-5h`) when no separate Argon bucket is reported, while partial or failed reports
  preserve existing valid tiers (#35).
- Clear the shared live usage cache (`$XDG_DATA_HOME/agroute/live_usage_cache.json`) for the
  cleared agent(s) on `agroute usage clear [--agent X]` so cleared tiers cannot repopulate on
  subsequent reads (#35).
- Add built-in catalog rate cards for `gemini-4-carbon` (#33) and for `claude-opus-4-5`,
  `claude-sonnet-4-5`, `gemini-3.7-flash`, `gemini-3.6-flash`, and `gemini-3.1-pro` (#35),
  verified against official Anthropic and Google Cloud pricing.
- Document how to read a native `swarm` worker's actual token usage from its conversation
  transcript (#33).

## 0.3.0 — 2026-11-12

- Price emitted candidates without configuration (#30). `candidates` and `inventory` attach
  a per-token `howhard-rate-card-v1` `rate_card` to every cell they can price, so `howhard`
  can compare worker cost across agents.
  - **Built-in catalog.** A hardcoded catalog of Anthropic, OpenAI and Google list prices,
    verified at 2026-11-12T02:18:20Z and cited to each provider's pricing page. It covers
    Claude models, Codex `gpt-6*` models and Antigravity CLI models. An Antigravity model
    resolves by stripping exactly its `-<effort>` suffix, and `claude-*` through Antigravity
    reuses the Anthropic card. Published scheduled price changes take effect on their dates.
  - **Guesses and third-party rates.** Antigravity's native tiers carry labelled guesses:
    a `source` starting `guess:` and `version = "agroute-guess-2026-11-12"`. `gpt-oss-120b`
    uses OpenRouter's third-party listed rate.
- Add per-model user prices, `[agents.<agent>.prices."<model>"]`, alongside discovery (#30).
  An entry is `usd_per_mtok` alone or all six rates; agroute refuses partial, unknown or
  negative entries at config load. Precedence, highest first:
  1. a candidate's own price;
  2. a per-model price;
  3. the later-effective of the refreshed snapshot and the built-in card, with the built-in
     card winning a tie;
  4. unpriced.
- Add `agroute prices refresh` (#30). It fetches OpenRouter's model listing through an
  explicit ID map and applies the built-in category rules. It writes an atomic snapshot to
  the state directory and reports per-model drift. It makes three bounded attempts and then
  fails clearly, keeping the previous snapshot. Routing never fetches.
- `agroute inventory` TOML output keeps a cell's `rate_card` instead of dropping it (#30).

## 0.2.3 — 2026-11-10

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
