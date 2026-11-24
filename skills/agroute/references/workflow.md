# Routing, `howhard` selection, and execution plan workflow

## 1. Emit filtered candidates for the current host

Run from the current project directory with `agr` and `howhard` installed:

```sh
agr paths --json
agr models --json
agr candidates --host agy --output /tmp/task-candidates.json --json
```

Unless explicitly overridden in `agroute.toml`, `agroute` automatically discovers the live
available models and reasoning efforts from the installed `codex`, `claude`, and `agy` clients.
`--output` writes the full eligible candidate JSON array consumed by `howhard`'s
`--candidates` flag, while stdout prints the full `agroute-candidates-v1` envelope including
any `excluded` agents or model tiers and their quota exclusion reasons. Routing refreshes
missing, stale, or expired known quota evidence; a provider whose refresh fails after two
bounded attempts is excluded with a retry reason. Inspect or retry the provider before
using it; independently fresh providers remain available.

Emitted cells carry a per-token `rate_card` when agroute can price their model. `agroute` is
100% offline for pricing and never fetches rates over the network: prices come from user/project
configuration, `<state_root>/prices.json` written via `agr prices resolve --file <path>`, or
the built-in catalog. Some cards are labelled guesses, with a `source` starting `guess:`.
Whenever `agr candidates --host HOST --json` (or `agr prices pending --host HOST --json`)
returns a non-null `price_request`, inspect `price_request.unpriced_models` and fulfill
`price_request.contract` once before calling `howhard`:

```sh
# 1. Inspect pending unknown models (if not already read from `candidates --json`)
agr prices pending --host agy --json

# 2. Write resolutions (`"priced"`, `"alias"`, or `"unlisted"` with `checked_sources` after a live web lookup) and persist locally
agr prices resolve --file /tmp/task-price-resolutions.json --json

# 3. Re-emit candidates now that prices.json is populated
agr candidates --host agy --output /tmp/task-candidates.json --json
```

Note that `agr candidates --host agy` WITHOUT `--json` prints the plain JSON candidate array
directly to stdout (matching `--output`) for `howhard --candidates` compatibility, whereas `--json`
prints the full `agroute-candidates-v1` envelope (`candidates`, `excluded`, `price_request`,
`credit_calibration`, and `warnings`). One unpriced cell turns `howhard`'s cost comparison off for
the whole inventory (or is excluded automatically when `credit_budget` is active). See the
rate-card and calling-agent callback contracts in `docs/reference.md`.

To generate a named `[inventories]` section for `howhard.toml` (`howhard` 2.1+ `--inventory`):

```sh
agr inventory --host agy --name router
```

## 2. Select a route with `howhard`

Use the calling-agent workflow (`--llm off` is no longer needed on `context`, `retrieve`, or
`decide` in `howhard` 5.0.0+). Pass `--auto-decide --seed 42` to `howhard context` so warm
memory-free prototype routes complete in one step and return `decision_id` directly; otherwise
follow `context`, `retrieve`, and `decide`, evaluating each `baseline_request` in a fresh
stateless context only when non-null (when `baseline_request` is `null`, matched prototypes
already cover that stage's cells, so omit `filter` or `forecasts` and reason only over active
memories):

```sh
howhard context task --text-file /tmp/task.txt --candidates /tmp/task-candidates.json --auto-decide --seed 42 --json > /tmp/task-context.json
# If /tmp/task-context.json already contains "decision_id", use it directly as /tmp/task-decision.json.
# Otherwise complete retrieval, Stage 1 filter, and Stage 2 forecast (evaluating baseline_request independently only when non-null):
howhard retrieve REQUEST_ID --file /tmp/retrieval-queries.json --json
howhard decide FILTER_REQUEST_ID --file /tmp/filter.json --json
howhard decide FORECAST_REQUEST_ID --file /tmp/forecasts.json --seed 42 --json > /tmp/task-decision.json
```

Or use `howhard pick` (which completes warm memory-free prototype routes directly or uses a
configured provider adapter otherwise), producing a `howhard-decision-v10` receipt (alongside
`howhard-decision-v7`, `v8`, and `v9`):

```sh
howhard pick task --text-file /tmp/task.txt --candidates /tmp/task-candidates.json --seed 42 --json > /tmp/task-decision.json
```

## 3. Resolve the `howhard` decision into an execution plan

```sh
agr plan --host agy --decision /tmp/task-decision.json --prompt-file /tmp/task.txt --output /tmp/task-plan.json --json
```

The returned `agroute-execution-plan-v1` object includes:

- `execution_mode`: `"native"` when `target_agent == host_agent`, or `"cli"` when `target_agent != host_agent`.
- `invocation`:
  - For `"native"`: `{"kind": "native_tool", "tool_name": "...", "parameters": {...}}`.
  - For `"cli"`: `{"kind": "cli_command", "argv": [...], "output_capture_adapter": "..."}`.
- `fallback`: when `execution_mode == "native"`, provides the pre-rendered non-interactive CLI command
  object (`argv`, `output_capture_adapter`, `prompt_delivery`) for the target model and effort so the
  host agent can recover immediately if native delegation fails at launch (e.g. subagent slot exhaustion);
  `null` when `execution_mode == "cli"`.
- `host_affinity_applied`: `true` if `routing.host_affinity_epsilon > 0.0` overrode a cross-agent
  choice in favor of an admissible native host cell within `epsilon` of the chosen cell's
  objective `J`.
- `feedback`: exact `howhard observe` template (`mode: "matched_receipt"` when the chosen cell
  is preserved, or `mode: "raw_native"` when `host_affinity_applied` is `true`).

## 4. Execute and record outcome in `howhard`

Execute the task using the native tool or CLI command in `invocation`. If the host refuses
or terminates the worker with an HTTP 429 / usage-limit error, immediately report it to
`agroute` (`agr usage set --agent AGENT --tier TIER --rate-limited --cooldown-until RESET_AT`
or `agr usage ingest --agent AGENT [--tier TIER] --text "HTTP 429: ..."`) before re-running
`agr candidates` for a fallback route. Then record the actual
outcome in `howhard` (`clean`, `loud`, `silent`, or `interrupted` with `--cause usage_limit` /
`provider_error` / `host_error` / `cancelled` / `unrecorded` for operational interruptions rather
than `loud` model failures):

```sh
howhard observe DECISION_ID clean --id EXECUTION_ID --json
howhard memory learn-context EXECUTION_ID --json
```

When `execution_mode == "cli"` produces an owned JSONL artifact for Codex (`codex-exec-v1`)
or Claude Code (`claude-sdk-v1`), collect and attach measured token usage through
`howhard usage collect` and `howhard usage attach` as documented in `howhard`.

An Antigravity native `swarm` worker (`invocation.tool_name == "swarm"`) writes no CLI
artifact. Read its actual usage from the worker's own conversation transcript,
`<appDataDir>/brain/<conversationId>/.system_generated/logs/transcript.jsonl`: sum
`input_tokens`, `cache_read_tokens` and `output_tokens` over its `PLANNER_RESPONSE` steps
after the worker finishes, and record that total with the outcome.

There is no model-count or cell-count cap. Every current model and supported effort is
emitted after quota filtering. Use `--include-agents` to restrict agents by policy. A
consumer with a smaller supported inventory must report its own validation error;
agroute never truncates the inventory to satisfy it.

## 5. Close raw-feedback, switched, or abandoned routes

`agr list` and `agr tui` show agroute's `ROUTE` separately from howhard's `OUTCOME`.
Receipt observations match automatically by `decision_id`. Keep `EXECUTION_ID` readable;
for raw native feedback after a fallback or host-affinity override, explicitly link it:

```sh
agr close ROUTE_ID --obs EXECUTION_ID --json
```

This suppresses the duplicate standalone raw row and sets `outcome_source=linked_raw`;
it does not score the unexecuted howhard receipt. For an in-place mid-task model switch,
record Model 1 as interrupted with `--cause usage_limit`, then create a continuation:

```sh
agr close ROUTE_ID --switched-to agy/gemini-4-argon-high --effort high --json
agr close CONTINUATION_ROUTE_ID --tokens 19000 --seconds 180 --credit-usd 1.4 --json
```

Use the returned `continuation_route_id` for the second command after Model 2 finishes.
The first route is `switched`; the continuation is `done` with `OUTCOME=unscored`.
Do not attribute the whole task's capability outcome to Model 2. Separately planned
routes can be linked with `--from ROUTE_ID` and `--superseded-by ROUTE_ID`.
`agr close ROUTE_ID --status cancelled --note "Declined after review"` abandons a plan.
`--status` otherwise defaults to `done`, or `switched` with `--switched-to` /
`--superseded-by`. Closure also accepts `--note`, `--tokens`, `--seconds`, and `--credit-usd`.
Completed review-flagged plans retain `done*`. Closed routes stop blocking calibration
intervals; cancellation releases its credit reservation. The TUI's `h` hides terminal
routes and observed raw rows; `s` filters lifecycle or outcome, and `?` explains both.
