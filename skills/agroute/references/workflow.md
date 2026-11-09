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
`--output` writes the raw 1–32-element candidate JSON array expected by `howhard`'s
`--candidates` flag, while stdout prints the full `agroute-candidates-v1` envelope including
any `excluded` agents or model tiers and their quota exclusion reasons.

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
