---
name: agroute
description: Use agroute to filter multi-agent candidate routes (Codex, Claude Code, Antigravity) by live native usage limits, select a route with howhard, and resolve the decision into a native host delegation call or cross-agent CLI execution plan.
---

# agroute

Use the installed `agroute` and `howhard` CLIs or Python APIs directly. This skill is the
consumer integration: `agroute` owns native quota/rate-limit filtering, provider-qualified
candidate emission (`<agent>/<model>`), and execution-plan resolution (`native` vs. `cli`),
while `howhard` owns model/effort selection, execution accounting, and writable semantic memory.
`agroute` is plan-only and never spawns worker processes itself.

1. Read the actual task and current project instructions/context. Identify the current host
   agent (`codex`, `claude`, or `agy`) and read [host execution surfaces](references/hosts.md).
2. Inspect native quota state using [native usage tracking](references/usage.md)
   (`agroute usage show --json`, `agroute usage ingest`, `agroute usage set`, or
   `agroute usage probe`). Routing automatically refreshes missing, stale, or expired
   known evidence and excludes a provider when bounded refresh fails.
3. Follow [the routing and decision workflow](references/workflow.md):
   - Run `agroute candidates --host HOST --output /tmp/TASK-candidates.json --json` to emit
     the full eligible inventory (`<agent>/<model>`, `<effort>`). (Without `--json`,
     `agroute candidates` prints the plain JSON candidate array directly for `howhard --candidates`.)
     When `target == host`, `agroute` emits `native_candidates` supported by the host's native
     delegation tool; when `target != host`, `agroute` emits `cli_candidates` supported by the
     target's non-interactive CLI.
   - **Check `price_request` before routing**: If `price_request` in the `candidates --json`
     envelope (or `agroute prices pending --host HOST --json`) is non-null, follow its `contract`
     once: look up official first-party provider token pricing (`status = "priced"`), map internal
     harness/orchestrator wrappers backed by a known catalog model (`status = "alias"`), or mark
     unpriced internal/experimental models (`status = "unlisted"`), write the JSON resolution file,
     run `agroute prices resolve --file /tmp/TASK-prices.json --json`, and re-run
     `agroute candidates --host HOST --output /tmp/TASK-candidates.json --json`.
   - When an agent resolves a subscription `plan` (`plan = "auto"` by default from native
     `detected_plan`, or an explicit preset configurable via `agroute config set-plan <agent> <plan>`),
     `agroute` keeps the plan's `monthly_cost_usd` / derived `weekly_cost_usd` separate from
     its calibrated quota credits. Candidates carry `cost_per_credit` and a
     `credit_budget.available_credit_usd` with `session_5h_remaining_credit_usd` and
     `weekly_7d_remaining_credit_usd` when known. Calibration follows plan credit ceilings and
     observed quota deltas; current measurements use `needed_credit_usd` or `credit_usd`, while
     older `cost_usd` observations remain readable. Active budgets require priced candidates,
     and `exclude_quota_groups` (such as `["3p"]`) is honored.
   - Pass `--candidates /tmp/TASK-candidates.json` to `howhard context` (followed by independent
     baseline forecast, `retrieve`, and `decide`) or `howhard pick`, saving the decision JSON
     to `/tmp/TASK-decision.json`.
   - Run `agroute plan --host HOST --decision /tmp/TASK-decision.json --json` to resolve the
     decision into an `agroute-execution-plan-v1` payload (which also verifies live
     `needed_credit_usd <= credit_budget.available_credit_usd` when present. In a
     `howhard-decision-v7` result, compare quota credit with `needed_credit_usd` and read
     `session_cost_usd` as cost.
4. Inspect `execution_mode`, `needs_review`, `invocation`, and `fallback` in the returned plan:
   - Honor `needs_review` before delegating or accepting silent-risk work.
   - When `execution_mode == "native"` (`target_agent == host_agent`), invoke the host's own
     native delegation tool (`swarm` or `invoke_subagent` in Antigravity, as named by
     `invocation.tool_name`; `Agent` in Claude Code; or `spawn_agent` in Codex) using the exact
     `invocation.parameters`.
     - **Native launch failure recovery**: If native delegation fails at launch (for example,
       host subagent concurrency or slot limit reached), execute the non-interactive CLI command
       in `fallback.argv` instead. When recording feedback after fallback execution, record raw
       native feedback (`howhard observe ... --native`) without attaching the unexecuted receipt.
   - When `execution_mode == "cli"` (`target_agent != host_agent`), run the non-interactive
     CLI command in `invocation.argv` (`codex exec --json`, `claude -p --verbose --output-format stream-json`,
     or `agy -p --output-format stream-json`), capturing its output in a task-owned temporary file.
5. After actual execution, record feedback in `howhard` following `feedback` in the resolved plan:
   - When `feedback.mode == "matched_receipt"`, record `howhard observe DECISION_ID OUTCOME --id EXECUTION_ID --json`.
   - When `feedback.mode == "raw_native"` (for example, when `host_affinity_epsilon` selected a
     native host route over `howhard`'s cross-agent choice) or if a different known route ran,
     record raw native feedback with `howhard observe ... --native` without attaching the
     unexecuted receipt.
   - Attach measured execution usage via `howhard usage collect` / `howhard usage attach` when
     supported, and complete `howhard memory learn-context` / `howhard memory learn`.
