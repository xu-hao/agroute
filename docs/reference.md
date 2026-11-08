# Routing and storage contracts

`agroute` is a plan-only router that connects native agent quota state to `howhard` 2.1+
candidate selection and host execution surfaces.

(candidate-qualification)=
## Candidate qualification and surface separation

An agent's native delegation tool (used when `target_agent == host_agent`) exposes different
controls from its non-interactive CLI (used when `target_agent != host_agent`). Each agent
configures two distinct lists in `agroute.toml`:

- `native_candidates`: candidate `(model, effort)` cells enforceable through the host's
  native delegation tool (`invoke_subagent` in Antigravity, `Agent` in Claude Code, or
  `spawn_agent` in Codex).
- `cli_candidates`: candidate `(model, effort)` cells enforceable through the target's
  non-interactive CLI (`codex exec`, `claude -p`, or `agy -p`).

`agroute candidates --host HOST` evaluates enabled agents, selects `native_candidates` for
`agent == HOST` and `cli_candidates` for `agent != HOST`, filters out quota-blocked agents
and model tiers, and prefixes every bare model ID with `<agent>/` (or `<agent>:` when
`namespace_separator = ":"`). `model_default`, `null`, and empty effort controls are refused.
The emitted list contains 1–32 distinct cells; if all routes are exhausted, `agroute` fails
closed with an `AgRouteError` naming each agent's exclusion reason.

(native-quota-tracking)=
## Native quota and rate-limit tracking

`agroute` tracks pre-task subscription quota windows in `<state_root>/usage.json`:

- `session_5h`: 5-hour rolling session window (`used_pct` in `0.0..100.0`, `reset_at` ISO-8601 UTC).
- `weekly_7d`: 7-day rolling weekly window (`used_pct` in `0.0..100.0`, `reset_at` ISO-8601 UTC).
- `tiers`: optional per-model or per-tier windows (such as `opus`, `sonnet`, `pro`, `flash`,
  `flash_lite`) with `used_pct` and `reset_at`.
- `rate_limited` and `cooldown_until`: active rate-limit state and expiry timestamp.

Window evaluation is time-aware: any window or cooldown with `now >= reset_at` expires
automatically at evaluation time. Unknown windows remain `null` and are never treated as `0.0`.
Post-execution token and cost accounting belongs to `howhard` (`howhard usage collect` and
`howhard usage attach`).

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
  - `agy`: `agy -p --output-format stream-json --model <model> --effort <effort>`,
    `output_capture_adapter = "agy-print-v1"`.

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
