# Routing and storage contracts

`agroute` is a plan-only router that connects native agent quota state to `howhard` 2.1+
candidate selection and host execution surfaces.

(candidate-qualification)=
## Candidate qualification, dynamic model discovery, and surface separation

An agent's native delegation tool (used when `target_agent == host_agent`) exposes different
controls from its non-interactive CLI (used when `target_agent != host_agent`). Each agent
supports two distinct candidate surfaces in `agroute.toml`:

- `native_candidates`: candidate `(model, effort)` cells enforceable through the host's
  native delegation tool (`invoke_subagent` in Antigravity, `Agent` in Claude Code, or
  `spawn_agent` in Codex).
- `cli_candidates`: candidate `(model, effort)` cells enforceable through the target's
  non-interactive CLI (`codex exec`, `claude -p`, or `agy -p`).

By default, `agroute` leaves `native_candidates` and `cli_candidates` empty in its packaged
defaults and automatically discovers the currently available models and reasoning efforts
from the locally installed agents at runtime:

- **Codex (`codex`)**: reads `${CODEX_HOME:-~/.codex}/models_cache.json`, keeps visible models
  from the highest available numeric generation in model slugs (opaque unversioned IDs
  are retained), ordered by `priority`, and emits every supported reasoning level
  (`supported_reasoning_levels`).
- **Claude Code (`claude`)**: reads the newest catalog in
  `${CLAUDE_CONFIG_DIR:-~/.claude}/cache/model-catalog/*.json`, selects `main` (`quick_select`)
  models (falling back to all catalog models only when none are marked), infers quota tier
  (`opus`, `fable`, `sonnet`, `haiku`), and emits every `runtime.effort_levels` value.
- **Antigravity (`agy`)**: uses the host `invoke_subagent` schema tiers (`pro`, `flash`,
  `flash_lite` at `standard` effort) for `native_candidates`, and queries `agy models` (cached
  in `${AGROUTE_SPOOL_DIR:-~/.local/share/agroute}/agy_models_cache.json`, refreshable via
  `agroute models --refresh`) for `cli_candidates`, retaining every listed model.

Inspect discovered or configured models per agent and surface with `agroute models [--refresh] [--json]`.
Explicit `[[agents.<name>.native_candidates]]` or `[[agents.<name>.cli_candidates]]` entries in
`agroute.toml` or `~/.config/agroute/config.toml` override runtime discovery for that surface.

`agroute candidates --host HOST` evaluates enabled agents, selects `native_candidates` for
`agent == HOST` and `cli_candidates` for `agent != HOST`, filters out quota-blocked agents
and model tiers, and prefixes every bare model ID with `<agent>/` (or `<agent>:` when
`namespace_separator = ":"`). `model_default`, `null`, and empty effort controls are refused.
The emitted list is the full eligible inventory with no model-count or cell-count cap.
Efforts appear in canonical order (`low`, `medium`, `high`, `xhigh`, `max`), followed by
additional efforts in their declared order. Missing effort catalogs retain the declared
default effort (or `medium`). Use `--include-agents` to restrict agents by policy.
Quota exclusions remain visible in `excluded` with their reasons. If all routes are
exhausted, `agroute` fails closed with an `AgRouteError` naming each exclusion reason.
With `candidates --json`, the failing command also emits an empty `candidates` list,
structured `excluded` reasons, and `warnings` before exiting with status 1.
Consumer limits are validated by the consumer; agroute never drops cells to fit them.

(native-quota-tracking)=
## Native quota and rate-limit tracking

`agroute` tracks pre-task subscription quota windows in `<state_root>/usage.json`:

- `session_5h`: 5-hour rolling session window (`used_pct` in `0.0..100.0`, `reset_at` ISO-8601 UTC).
- `weekly_7d`: 7-day rolling weekly window (`used_pct` in `0.0..100.0`, `reset_at` ISO-8601 UTC).
- `tiers`: optional per-model or per-tier windows (such as `opus`, `sonnet`, `pro`, `flash`,
  `flash_lite`, `argon`, `3p-5h`, `3p-weekly`) with `used_pct` and `reset_at`.
- `models`: resolved per-model quota windows and eligibility (`session_5h`, `weekly_7d`,
  `quota_group`, `eligible`, `exclusion_reason`) for every available native and CLI model.
- `rate_limited` and `cooldown_until`: active rate-limit state and expiry timestamp.

Window evaluation is time-aware: any window or cooldown with `now >= reset_at` expires
automatically at evaluation time. Unknown windows remain `null` and are never treated as `0.0`.
Post-execution token and cost accounting belongs to `howhard` (`howhard usage collect` and
`howhard usage attach`).

### Claude availability evidence and freshness

Claude reports may name a limit with a string scope or a structured
`scope.model.display_name` (falling back to `id` or `name`). Names are lowercased and
sanitized to letters, digits, underscores and hyphens. A meaningful `surface` is appended;
for example Fable on Claude Code becomes `fable-claude-code`. The base tier and its
`-5h` or `-weekly` variant are retained. Model matching uses the existing model/tier
tokens, so a Fable limit affects Fable cells. Each malformed or unknown limit is skipped
individually; recognized limits still apply and probe JSON includes `warnings`.

A `rejected`, `rate_limited`, `exhausted`, or numeric 429 rate-limit status is availability
evidence even without a percentage. `seven_day` names the weekly quota and `five_hour`
the session quota; model scopes affect only that model. Unscoped credit/entitlement
rejections affect the whole agent. `overageStatus: rejected` supplies no overage fallback.
Active rejections exclude routes with a visible reason and reset time. Unknown usage
remains `null`; rejection does not manufacture a 100% counter.

Availability is reconciled separately for each model/surface and window. Newer authoritative
quota reports or accepted/allowed events supersede rejection for the refreshed quota,
including access restored by a plan upgrade before the old reset. A plan change alone does
not establish remaining quota. An unrelated successful request or partial report cannot
clear another rejected model/window. Recovery discards an obsolete rejection-era counter
when no replacement is measured, while current measured usage and configured quota
thresholds still apply. Ordinary accepted events preserve genuine measured counters.
`now >= reset_at` expires the affected evidence. A successful probe command with no
recognized quota/availability data is a failed probe and cannot restore access.

Window metadata records `status`, `observed_at`, and, for measured percentages,
`measured_at`. Recent transcript events are compared with persisted evidence even when a
snapshot already exists. Explicit event timestamps determine ordering; untimestamped
transcript events use their file time and a hashed source/line identity. A current probe
supersedes previously observed identities for exactly the quotas it refreshes. Touching an
old transcript cannot replay a rejection, while a newly recorded identical rejection is
new evidence and can exclude the route again. Discovery reads the last 256 KiB of the five
most recently modified transcript files.

The snapshot's `updated_at` describes quota evidence time. `probe.attempted_at`,
`probe.succeeded_at`, `probe.failed_at`, and `probe.error` track probe operations separately;
failed probes preserve the data time. Old cached data is never counted as a fresh probe.
`usage show`, `candidates`, `inventory`, and `plan` include top-level `warnings` when Claude
evidence or the last successful probe is stale, or the latest probe failed. The configured
age bound defaults to 1,800 seconds. After a failed probe, stale percentages are displayed
as `null`; fresh measured counters and active rejection evidence remain effective.
Older persisted Claude telemetry cooldowns are migrated into recoverable availability
evidence. An unambiguous matching stored window/reset identifies the old quota; a new
report may replace both the old rejection and reset after a plan change. Ambiguous old
cooldowns remain access rejections, cleared by explicit unscoped accepted/allowed evidence
or expiry, rather than by an unrelated partial quota counter.
Unknown usage can remain eligible with a warning, subject to configured policy. This
freshness policy applies to Claude; Codex and Antigravity retain their existing behavior.


(execution-plan-resolution)=
## Execution plan resolution and host affinity

`agroute plan --host HOST --decision FILE` validates a `howhard` decision JSON (`decision_id`,
`chosen.model`, `chosen.effort`, `cap_met`, `needs_review`) against the current host's active
eligible candidate pool and emits an `agroute-execution-plan-v1` object:

- **`execution_mode = "native"` (`target_agent == host_agent`)**:
  - `agy`: `tool_name = "invoke_subagent"` with `TypeName`, `Model`, `Role`, `Workspace`, and
    optional `Prompt`.
  - `claude`: `tool_name = "Agent"` with `subagent_type`, `description`, `model`, `effort`,
    and optional `prompt`.
  - `codex`: `tool_name = "spawn_agent"` with `model`, `reasoning_effort`, optional
    `agent_type`, and optional `prompt`.
- **`execution_mode = "cli"` (`target_agent != host_agent`)**:
  - `codex`: `codex exec --json --model <model> -c model_reasoning_effort=<effort>`,
    `output_capture_adapter = "codex-exec-v1"`.
  - `claude`: `claude -p --verbose --output-format stream-json --model <model> --effort <effort>`,
    `output_capture_adapter = "claude-sdk-v1"`.
  - `agy`: `agy --output-format stream-json --model <model> --effort <effort> -p <prompt>`,
    `output_capture_adapter = "agy-print-v1"`.

When `execution_mode = "native"`, `agroute plan` also attaches a `fallback` CLI command object
(`argv`, `output_capture_adapter`, `prompt_delivery`) for the target model and effort. If native
tool delegation fails at launch (for example, if a host client reaches its local concurrent subagent
limit), the host agent can immediately execute `fallback` via CLI and record the outcome as raw native
feedback (`howhard observe ... --native`).

When `routing.host_affinity_epsilon > 0.0` and `howhard` chose a cross-agent route, `agroute plan`
checks whether an eligible native host route in the decision's scored panel (`candidates`,
`cells`, or `ranking`) has `cap_met != false` and expected cost `J_native <= J_chosen + epsilon`.
When that condition holds, `agroute plan` selects the native host cell, sets
`host_affinity_applied = true`, and sets `feedback.mode = "raw_native"` so the caller records
`howhard observe ... --native` instead of attaching the unexecuted cross-agent receipt.

(worktree-state-contract)=
## Worktree-aware state ownership

`agroute paths --json` reports `capture_root`, `state_root`, `identity_path`, `project_identity`,
and `config_path`. Fresh zero-config Git worktrees share `<git-common-dir>/agroute` while
capturing the current checkout root. Plain folders use local `.agroute`. `--state-dir PATH`
(or `AGROUTE_STATE_DIR`) selects an intentional store and enforces `agroute-project-v1` and
`agroute-store-v1` identity checks so another project's store is refused.

(agent-adapters-and-dynamic-dispatch)=
## Agent adapters and dynamic dispatch

Agent-specific behaviors are decoupled from routing and usage logic through the `AgentAdapter`
interface and dynamic dispatch registry (`agroute.adapters`). Each supported agent (`agy`, `claude`,
`codex`) implements an `AgentAdapter` that encapsulates:

- `name` & `display_name`: canonical identifier and human-readable label.
- `capture_adapter`: CLI stream output capture protocol name (`agy-print-v1`, `claude-sdk-v1`, `codex-exec-v1`).
- `validate_native_candidate`: agent-specific constraints on native subagent model parameters (such as the `invoke_subagent` enum in Antigravity).
- `discover_candidates`: surface-aware local model discovery (`native` vs `cli`).
- `build_native_invocation`: formats the native tool call directive (`invoke_subagent`, `Agent`, `spawn_agent`).
- `build_cli_invocation`: constructs the non-interactive CLI argv and prompt delivery mechanism.
- `discover_live_usage_patch`: discovers rate-limit telemetry from local session files and probe caches.
- `probe_usage`: performs zero-token or RPC rate-limit probing.
- `resolve_quota_group`: resolves model-specific or multi-provider quota buckets (such as `3p` and `gemini` pools).

New agent integrations or mock adapters can be registered dynamically via `register_adapter(adapter)`
and queried with `get_adapter(name)`.

(realtime-route-monitoring)=
## Realtime route monitoring (`tui`, `list`, `show`)

Every `agroute plan` resolution appends a record to `<state_root>/routes.jsonl` and registers
the repository in `${AGROUTE_SPOOL_DIR:-~/.local/share/agroute}/repositories.json`. `agroute`
correlates `<state_root>/routes.jsonl` with `.git/howhard/` (`decision-events.jsonl`,
`decisions/*.json`, and `pending.jsonl`, including `amends` chains) so both `agroute plan`
resolutions and direct `howhard` routing decisions appear in reverse-chronological order with
their live outcome state (`pending`, `clean`, `loud`, `silent`, `review`, `planned`) and
task summary.

- `agroute tui [--all] [--repo NAME] [--agent AGENT] [--model MODEL] [--state STATE] [--refresh-seconds SEC]`:
  interactive Textual terminal dashboard showing live agent quota/eligibility badges, active and
  historical model routes, `Enter` route detail modal, `u` live usage & eligibility modal,
  `h` history toggle, and filter pickers for repository (`p`), target agent (`a`), model (`m`),
  and state (`s`).
- `agroute list [--all] [--repo NAME] [--agent AGENT] [--model MODEL] [--state STATE] [--limit N] [--json]`:
  scriptable tabular or JSON view of the same route records.
- `agroute show <id> [--json]`: full route record, invocation details, and `howhard` decision
  for a route ID, decision ID, or execution ID.
