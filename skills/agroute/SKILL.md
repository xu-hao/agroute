---
name: agroute
description: Use agroute to filter multi-agent candidate routes (Codex, Claude Code, Antigravity) by live native usage limits, select a route with howhard, and resolve the decision into a native host delegation call or cross-agent CLI execution plan.
---

# agroute

Use the installed `agr` and `howhard` CLIs or Python APIs directly. The Python package and
module remain `agroute`. This skill is the consumer integration: `agroute` owns native
quota/rate-limit filtering, provider-qualified
candidate emission (`<agent>/<model>`), and execution-plan resolution (`native` vs. `cli`),
while `howhard` owns model/effort selection, execution accounting, and writable semantic memory.
`agroute` is plan-only and never spawns worker processes itself.

1. Read the actual task and current project instructions/context. Identify the current host
   agent (`codex`, `claude`, or `agy`) and read [host execution surfaces](references/hosts.md).
2. Inspect native quota state using [native usage tracking](references/usage.md)
   (`agr usage show --json`, `agr usage ingest`, `agr usage set`, or
   `agr usage probe`). Routing automatically refreshes missing, stale, or expired
   known evidence, excludes a provider in one bounded attempt when authentication expires or is
   logged out (for example, prompting `claude auth login`) and recovers immediately once logged
   back in, and excludes other infrastructure probe failures after two bounded attempts.
3. Follow [the routing and decision workflow](references/workflow.md):
   - Run `agr candidates --host HOST --output /tmp/TASK-candidates.json --json` to emit
     the full eligible inventory (`<agent>/<model>`, `<effort>`, and the `agent` label that
     `howhard` 6.0.0+ stores). (Without `--json`,
     `agr candidates` prints the plain JSON candidate array directly for `howhard --candidates`.)
     When `target == host`, `agroute` emits `native_candidates` supported by the host's native
     delegation tool; when `target != host`, `agroute` emits `cli_candidates` supported by the
     target's non-interactive CLI.
   - **Check `price_request` before routing**: If `price_request` in the `candidates --json`
     envelope (or `agr prices pending --host HOST --json`) is non-null, follow its `contract`
     once: perform a **live web lookup** against official first-party provider pricing pages
     (`status = "priced"`), map internal harness/orchestrator wrappers backed by a known catalog
     model (`status = "alias"`), or mark confirmed-absent internal/experimental models
     (`status = "unlisted"` with `checked_sources` listing the official URLs checked — never
     guess from training data or mark `"unlisted"` when offline), write the JSON resolution file,
     run `agr prices resolve --file /tmp/TASK-prices.json --json`, and re-run
     `agr candidates --host HOST --output /tmp/TASK-candidates.json --json`.
   - When an agent resolves a subscription `plan` (`plan = "auto"` by default from native
     `detected_plan`, or an explicit preset configurable via `agr config set-plan <agent> <plan>`),
     `agroute` keeps the plan's `monthly_cost_usd` / derived `weekly_cost_usd` separate from
     its calibrated quota credits. Candidates carry `cost_per_credit` and a
     `credit_budget.available_credit_usd` with `session_5h_remaining_credit_usd` and
     `weekly_7d_remaining_credit_usd` when known. Calibration follows plan credit ceilings and
     observed quota deltas; current measurements use `needed_credit_usd` or `credit_usd`, while
     older `cost_usd` observations remain readable. Active budgets require priced candidates,
     and `exclude_quota_groups` (such as `["3p"]`) is honored.
   - Pass `--candidates /tmp/TASK-candidates.json --host HOST` to
     `howhard context ... --auto-decide --seed SEED`
     (which completes warm memory-free prototype routes in one step when `decision_id` is returned;
     otherwise follow `context`, `retrieve`, and `decide`, evaluating each `baseline_request` in an
     independent stateless context only when non-null, without needing `--llm off`) or
     `howhard pick ... --host HOST`,
     saving the decision JSON to `/tmp/TASK-decision.json`.
   - Run `agr plan --host HOST --decision /tmp/TASK-decision.json --json` to resolve the
     decision into an `agroute-execution-plan-v1` payload (which also verifies that live
     `credit_budget.available_credit_usd` covers the larger of `needed_credit_usd` and
     `credit_p80_usd` when present, including for a host-affinity override). In a
     `howhard-decision-v7`, `v8`, `v9`, `v10`, or `v11` result, read quota credit from
     `needed_credit_usd` (the average, which `agroute` reserves) and `credit_p80_usd` (`v11`'s
     80% point), and read `session_cost_usd` as cost.
4. Inspect `execution_mode`, `needs_review`, `invocation`, `fallback`, and `rate_card` (the
   target cell's price frozen at planning time, or `null`; `usd_per_mtok` appears for a blended
   price) in the returned plan:
   - Honor `needs_review` before delegating or accepting silent-risk work.
   - When `execution_mode == "native"` (`target_agent == host_agent`), invoke the host's own
     native delegation tool (`swarm` or `invoke_subagent` in Antigravity, as named by
     `invocation.tool_name`; `Agent` in Claude Code; or `spawn_agent` in Codex) using the exact
     `invocation.parameters`.
     - **Native launch failure / host 429 recovery**: If native delegation fails at launch
       (for example, host subagent concurrency or slot limit reached), execute the
       non-interactive CLI command in `fallback.argv` instead. If a worker is refused or
       terminated with a host HTTP 429 / usage-limit error, immediately report the refusal to
       `agroute` so that tier (or agent) is excluded before re-running `agr candidates`
       (`agr usage set --agent AGENT --tier TIER --rate-limited --cooldown-until RESET_AT` or
       `agr usage ingest --agent AGENT [--tier TIER] --text "HTTP 429: ..."`), and record the
       execution as `howhard observe ... interrupted --cause usage_limit`. When recording
       feedback after fallback execution, record raw native feedback
       (`howhard observe ... --native`) without attaching the unexecuted receipt.
   - When `execution_mode == "cli"` (`target_agent != host_agent`), run the non-interactive
     CLI command in `invocation.argv` (`codex exec --json`, `claude -p --verbose --output-format stream-json`,
     or `agy -p --output-format stream-json`), capturing its output in a task-owned temporary file.
5. After actual execution, record feedback in `howhard` following `feedback` in the resolved plan:
   - When `feedback.mode == "matched_receipt"`, record `howhard observe DECISION_ID OUTCOME --id EXECUTION_ID --json`.
     Record usage-limit, provider/host error, or cancellation aborts as `interrupted --cause usage_limit`
     (or `provider_error`, `host_error`, `cancelled`, `unrecorded`), never as `loud` model failures.
   - When `feedback.mode == "raw_native"` (for example, when `host_affinity_epsilon` selected a
     native host route over `howhard`'s cross-agent choice) or if a different known route ran,
     record raw native feedback with `howhard observe ... --native` without attaching the
     unexecuted receipt. Then run `agr close ROUTE_ID --obs EXECUTION_ID` to link the raw
     observation and remove the duplicate dashboard row, preserving a human-readable execution ID.
   - Attach measured execution usage via `howhard usage collect` / `howhard usage attach`,
     putting the plan's frozen `rate_card` in the `howhard-usage-manifest-v1` as the
     [workflow](references/workflow.md#4-execute-and-record-outcome-in-howhard) describes:
     `codex-exec-v1` and `claude-sdk-v1` for those CLI streams (the Claude adapters refuse the
     provider-qualified card), `agy-print-v1` for `agy` CLI streams, and `agy-transcript-v1`
     for Antigravity `swarm` `transcript.jsonl` files. `howhard` 6.0.0+ ships both `agy`
     adapters for Antigravity `1.5.18`; if it cannot collect an artifact, follow the workflow's
     fallback. Then complete `howhard memory learn-context` / `howhard memory learn`.
   - When `howhard` has no priced measurement, pass measured quota credit to
     `agr close ROUTE_ID --credit-usd USD` (with `--obs EXECUTION_ID` for raw feedback).
     Calibration counts the execution once: close credit fills a missing or zero `howhard`
     credit, and a positive `howhard` measurement wins.

6. Keep route lifecycle separate from howhard outcome: `ROUTE` is `routed` / `review`, then
   `done` / `switched` / `cancelled`; `done*` retains a plan review warning. After an in-place
   model switch, score only the first model's interruption (`--cause usage_limit`), use
   `agr close ROUTE_ID --switched-to AGENT/MODEL --effort EFFORT` to create a continuation,
   and close its returned `continuation_route_id` with measured `--tokens`, `--seconds`,
   and `--credit-usd` after completion. The continuation remains howhard-unscored
   (`OUTCOME=unscored` on `done`). Use `--from` / `--superseded-by` to link separately
   planned routes, or `--status cancelled` to abandon a plan and release its reservation.
