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
2. Refresh or inspect live native quota state using [native usage tracking](references/usage.md)
   (`agroute usage show --json`, `agroute usage ingest`, `agroute usage set`, or
   `agroute usage probe`). Expired windows clear automatically when `now >= reset_at`.
3. Follow [the routing and decision workflow](references/workflow.md):
   - Run `agroute candidates --host HOST --output /tmp/TASK-candidates.json --json` to emit
     the full eligible inventory (`<agent>/<model>`, `<effort>`). When `target == host`,
     `agroute` emits `native_candidates` supported by the host's native delegation tool; when
     `target != host`, `agroute` emits `cli_candidates` supported by the target's non-interactive CLI.
   - Pass `--candidates /tmp/TASK-candidates.json` to `howhard context` (followed by independent
     baseline forecast, `retrieve`, and `decide`) or `howhard pick`, saving the decision JSON
     to `/tmp/TASK-decision.json`.
   - Run `agroute plan --host HOST --decision /tmp/TASK-decision.json --json` to resolve the
     decision into an `agroute-execution-plan-v1` payload.
4. Inspect `execution_mode`, `needs_review`, `invocation`, and `fallback` in the returned plan:
   - Honor `needs_review` before delegating or accepting silent-risk work.
   - When `execution_mode == "native"` (`target_agent == host_agent`), invoke the host's own
     native delegation tool (`invoke_subagent` in Antigravity, `Agent` in Claude Code, or
     `spawn_agent` in Codex) using the exact `invocation.parameters`.
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
