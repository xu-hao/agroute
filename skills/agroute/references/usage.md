# Native quota and rate-limit tracking

`agroute` tracks pre-task subscription quota windows (`session_5h`, `weekly_7d`, and optional
model-tier windows) and rate-limit cooldowns per agent (`codex`, `claude`, `agy`). Post-task
token and cost accounting remains in `howhard`.

Subscription charges and quota credit use distinct fields. `monthly_cost_usd` is the plan's
monthly cash charge and `weekly_cost_usd` is the 7-day amortized charge. `session_5h_credit_usd`,
`weekly_7d_credit_usd`, and `credit_budget.*_credit_usd` describe list-price quota capacity.
Candidate cells expose `cost_per_credit` so `howhard` can convert estimated quota credit to
`session_cost_usd`; its `needed_credit_usd` remains the value checked against
`credit_budget.available_credit_usd`. `usage show` reports subscription costs beside the
per-model cost-per-credit, credit budget, and credit calibration metadata.

## Inspect current quota and eligibility

```sh
agr usage show --json
agr usage show --agent claude --json
```

Windows whose `reset_at` (or `cooldown_until`) timestamp has passed (`now >= reset_at`) clear
automatically during evaluation. `candidates`, `inventory`, and `plan` refresh missing,
stale, or expired known evidence before routing; fresh reports cover only their reported
scopes. A recognized current report can retire already expired scopes, leaving them null;
omitted nonexpired stale scopes still need current evidence. A failed refresh after two
bounded attempts excludes that provider with a retry
reason, while independently fresh providers remain available. Unknown unreported windows
remain null. `usage show` inspects state without probing.

## Ingest native command or event-stream output

Pipe or pass a file captured from the agent's native usage/status command or JSONL stream:

```sh
# Codex JSONL rate_limits event or /status text
agr usage ingest --agent codex --file /tmp/codex-status.jsonl --json

# Claude Code /usage text or stream-json rate_limit_event
agr usage ingest --agent claude --file /tmp/claude-usage.txt --json

# Antigravity quota/status output
agr usage ingest --agent agy --file /tmp/agy-usage.txt --json
```

## Explicitly set or clear quota windows and cooldowns

When a live call hits a rate limit or quota wall, update the agent's window or cooldown directly:

```sh
agr usage set --agent claude --session-used-pct 95 --session-reset-at 2026-11-08T21:00:00Z --json
agr usage set --agent codex --rate-limited --cooldown-until 2026-11-08T19:30:00Z --json
agr usage set --agent agy --tier pro --tier-used-pct 96 --tier-reset-at 2026-11-08T20:00:00Z --json
agr usage clear --agent codex --json
```

## Run live usage probes

Refresh quota state across enabled agents using zero-token built-in live probes (`agy -p /usage`,
`claude -p /usage`, and `codex app-server` `account/rateLimits/read`), local session logs, or
custom `agents.<name>.usage_probe_cmd` commands:

```sh
agr usage probe --json
```
