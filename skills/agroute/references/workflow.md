# Routing, `howhard` selection, and execution plan workflow

## 1. Emit filtered candidates for the current host

Run from the current project directory with `agroute` and `howhard` installed:

```sh
agroute paths --json
agroute models --json
agroute candidates --host agy --output /tmp/task-candidates.json --json
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
configuration, `<state_root>/prices.json` written via `agroute prices resolve --file <path>`, or
the built-in catalog. Some cards are labelled guesses, with a `source` starting `guess:`.
Whenever `agroute candidates --host HOST --json` (or `agroute prices pending --host HOST --json`)
returns a non-null `price_request`, inspect `price_request.unpriced_models` and fulfill
`price_request.contract` once before calling `howhard`:

```sh
# 1. Inspect pending unknown models (if not already read from `candidates --json`)
agroute prices pending --host agy --json

# 2. Write resolutions (`"priced"`, `"alias"`, or `"unlisted"`) and persist locally
agroute prices resolve --file /tmp/task-price-resolutions.json --json

# 3. Re-emit candidates now that prices.json is populated
agroute candidates --host agy --output /tmp/task-candidates.json --json
```

Note that `agroute candidates --host agy` WITHOUT `--json` prints the plain JSON candidate array
directly to stdout (matching `--output`) for `howhard --candidates` compatibility, whereas `--json`
prints the full `agroute-candidates-v1` envelope (`candidates`, `excluded`, `price_request`,
`credit_calibration`, and `warnings`). One unpriced cell turns `howhard`'s cost comparison off for
the whole inventory (or is excluded automatically when `credit_budget` is active). See the
rate-card and calling-agent callback contracts in `docs/reference.md`.

To generate a named `[inventories]` section for `howhard.toml` (`howhard` 2.1+ `--inventory`):

```sh
agroute inventory --host agy --name router
```

## 2. Select a route with `howhard`

Use the calling-agent workflow (independent baseline forecast in a fresh stateless context,
then memory retrieval and decision):

```sh
howhard context task --text-file /tmp/task.txt --candidates /tmp/task-candidates.json --llm off --json > /tmp/task-context.json
howhard retrieve REQUEST_ID --file /tmp/retrieval-queries.json --llm off --json
howhard decide REQUEST_ID --file /tmp/forecasts.json --llm off --seed 42 --json > /tmp/task-decision.json
```

Or use `howhard pick` when a provider adapter is configured:

```sh
howhard pick task --text-file /tmp/task.txt --candidates /tmp/task-candidates.json --seed 42 --json > /tmp/task-decision.json
```

## 3. Resolve the `howhard` decision into an execution plan

```sh
agroute plan --host agy --decision /tmp/task-decision.json --prompt-file /tmp/task.txt --output /tmp/task-plan.json --json
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

Execute the task using the native tool or CLI command in `invocation`. Then record the actual
outcome in `howhard`:

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
