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
stale, or expired known evidence before routing (tightening staleness automatically to 60s
when a usable window has less than 20% headroom before its cutoff), and `plan` records
in-flight credit reservations across consumed quota pools until a newer probe or measured
observation supersedes them. Fresh reports cover only their reported scopes. A recognized current report can retire already expired scopes, leaving them null;
omitted nonexpired stale scopes still need current evidence (explicit unexpired tier
rejections remain authoritative until `reset_at` or probe recovery). A failed refresh after two
bounded attempts excludes that provider with a retry
reason, while independently fresh providers remain available. Unknown unreported windows
remain null. `usage show` inspects state without probing.

## Ingest native command or event-stream output

Pipe or pass a file captured from the agent's native usage/status command or JSONL stream,
or pass inline host 429 error text (with optional `--tier` to scope the refusal to one model
tier):

```sh
# Codex JSONL rate_limits event or /status text
agr usage ingest --agent codex --file /tmp/codex-status.jsonl --json

# Claude Code /usage text or stream-json rate_limit_event
agr usage ingest --agent claude --file /tmp/claude-usage.txt --json

# Host 429 refusal scoped to one model tier
agr usage ingest --agent claude --tier fable-weekly --text "HTTP 429: You've hit your weekly limit · resets 2026-11-25T00:00:00Z" --json

# Antigravity quota/status output
agr usage ingest --agent agy --file /tmp/agy-usage.txt --json
```

## Explicitly set or clear quota windows and cooldowns

When a live call hits a rate limit or quota wall, update the agent's window or cooldown
directly (`--rate-limited` / `--no-rate-limited` and `--cooldown-until` apply to `--tier`
when given, or to the whole agent otherwise):

```sh
agr usage set --agent claude --session-used-pct 95 --session-reset-at 2026-11-08T21:00:00Z --json
agr usage set --agent claude --tier fable-weekly --rate-limited --cooldown-until 2026-11-25T00:00:00Z --json
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

For `claude`, `agroute` checks `claude auth status` (without reading credential files) before
invoking `claude -p /usage`, and also recognizes `authentication_failed` / `401` / logged-out
signals in `agr usage ingest` and recent `$CLAUDE_CONFIG_DIR/projects/*.jsonl` transcripts. When
Claude login has expired or is logged out:

- `agr usage probe --agent claude` raises `AgRouteAuthError` naming `claude auth login` without
  spawning `claude -p /usage`.
- Routing (`candidates`, `inventory`, `plan`) stops after 1 probe attempt (no 2-attempt retry),
  excludes `claude` with an actionable `claude auth login` reason, and still admits other healthy
  providers (failing closed only if no eligible providers remain).
- `agr usage show` marks `claude` and all its models ineligible (`eligible_as_target = false`,
  `eligible_as_host_native = false`), and `agr tui` displays `claude LOGIN` in `#route-status`
  and `STATUS = LOGIN` in `#quotas` (reserving `BLOCKED` strictly for quota, rate-limit, and
  credit-reserve exhaustion).
- As soon as you run `claude auth login`, the very next routing call or TUI probe (`P`)
  re-probes immediately without waiting out the 30-minute staleness window, clears the `auth`
  block and `probe.failed_at`, and restores `detected_plan` and eligibility.
