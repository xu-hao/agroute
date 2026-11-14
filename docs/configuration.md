# Configuration

`agroute` layers three configuration sources in order:

1. Packaged defaults (`agroute/data/default_config.toml`).
2. Optional user configuration at `${XDG_CONFIG_HOME:-~/.config}/agroute/config.toml`
   (or `AGROUTE_USER_CONFIG`; disable with `--no-user-config`).
3. Project `agroute.toml` in the current directory, or `--config PATH`.

Unknown keys, out-of-range percentages, negative `host_affinity_epsilon`, bare models containing
`/` or `:`, and `model_default` effort strings are refused before any state is modified.

## Dynamic model discovery

Packaged defaults in `agroute/data/default_config.toml` set `native_candidates = []` and
`cli_candidates = []` for every agent. `agroute` discovers each agent's available models and
supported reasoning efforts dynamically at runtime from its local installation (`agroute models`),
so configuration files do not need static model lists as providers release new models and
retire older ones. Discovery retains every current model and supported effort, without
numeric caps; see the {ref}`selection rules <candidate-qualification>`.
Explicit candidate lists override discovery for their respective surface. `candidates`
and `inventory` emit the full eligible inventory; `--include-agents` restricts agents
by policy, and never serves as an automatic size limit.

Prices follow the {ref}`rate-card precedence <price-precedence>`:

- A candidate entry may carry either an optional `usd_per_mtok` or a `howhard-rate-card-v1`
  `rate_card`, but not both, and that price wins.
- Otherwise a per-model entry under `[agents.<agent>.prices."<model>"]` applies. It holds
  either `usd_per_mtok` alone or all six rate categories, and works alongside discovery.
  See {ref}`per-model user prices <per-model-prices>`.
- Otherwise the non-expired snapshot entry from `agroute prices resolve --file <path>`
  (`<state_root>/prices.json`) or the built-in catalog prices the cell, whichever took effect
  later (or via a labelled `"alias"` guess; see {ref}`calling-agent price resolution <calling-agent-price-resolution>`).

A candidate `rate_card`'s `model` must be the qualified cell model, for example
`agy/gpt-oss-120b-medium`, because `howhard` rejects a mismatch.

## Example `agroute.toml`

```toml
[routing]
default_host = "agy"
host_affinity_epsilon = 0.0
namespace_separator = "/"

[agents.agy]
enabled = true
plan = "ultra-200"
exclude_quota_groups = ["3p"]
max_session_used_pct = 90.0
max_weekly_used_pct = 95.0
max_tier_used_pct = 95.0
host_reserve_pct = 5.0
usage_stale_after_seconds = 1800
usage_probe_cmd = []

[agents.codex]
enabled = true
plan = "pro-200"
max_session_used_pct = 90.0
max_weekly_used_pct = 95.0
max_tier_used_pct = 95.0
host_reserve_pct = 0.0
usage_stale_after_seconds = 1800
usage_probe_cmd = []

[agents.claude]
enabled = true
plan = "max-20x"
usage_stale_after_seconds = 1800
max_session_used_pct = 90.0
max_weekly_used_pct = 95.0
max_tier_used_pct = 95.0
host_reserve_pct = 0.0
usage_probe_cmd = []
```

`agents.<name>.usage_stale_after_seconds` is a positive number of seconds (default
`1800`) for each provider. Before routing, known stale or expired scopes and missing
evidence trigger a bounded refresh; a failed refresh excludes that provider with a retry
reason. Unknown unreported windows stay `null`. See the
{ref}`quota freshness contract <native-quota-tracking>` for partial reports, retries,
and offline inspection.

(subscription-plan-presets)=
## Subscription plan presets, cost overrides, credit ceilings, and quota-group exclusions

Each `[agents.<name>]` section accepts a `plan` selector, a subscription cash cost, and
independent quota-credit ceilings:

- `plan = "auto"` (packaged default) resolves credit and cost from the provider's detected
  plan. `""` or `"none"` disables preset lookup, leaving any explicit `credit` override in
  place. An explicit preset or alias selects the corresponding packaged subscription.
- `monthly_cost_usd` optionally overrides the preset's monthly subscription cost. When the
  plan has a known cost, `weekly_cost_usd` is derived as
  `monthly_cost_usd * 7 / 30`.
- `credit = {}` optionally overrides quota credit ceilings. Its `session_5h_credit_usd` and
  `weekly_7d_credit_usd` values, including values under
  `tiers.<quota_group>.{session_5h_credit_usd, weekly_7d_credit_usd}`, are list-price
  credit ceilings in USD. Manual credit overrides remain exact and are not recalibrated.

Packaged plans keep cash cost separate from quota credit:

| Agent | Plan | Aliases | Monthly cost | Credit ceilings |
| --- | --- | --- | ---: | --- |
| `agy` | `free` | `google-ai-free`, `google ai free` | $0 | `gemini`: $3 session / $10 weekly |
| `agy` | `plus-8` | `plus`, `google-ai-plus`, `google ai plus` | $8 | `gemini`: $12 / $50 |
| `agy` | `pro-20` | `pro`, `google-ai-pro`, `google ai pro` | $20 | `gemini`: $32 / $135; `3p`: $5 / $10 |
| `agy` | `ultra-100` | — | $100 | `gemini`: $80 / $337.50; `3p`: $12.50 / $25 |
| `agy` | `ultra-200` | `ultra`, `google-ai-ultra`, `google ai ultra` | $200 | `gemini`: $320 / $1,350; `3p`: $25 / $50 |
| `claude` | `pro-20` | `pro`, `claude-pro` | $20 | shared: $18 / $45 |
| `claude` | `max-5x` | `max_5x`, `claude-max-5x` | $100 | shared: $90 / $225; `fable` weekly: $75 |
| `claude` | `max-20x` | `max_20x`, `claude-max-20x` | $200 | shared: $360 / $900; `fable` weekly: $300 |
| `codex` | `plus-20` | `plus`, `chatgpt-plus` | $20 | shared: $20 / $50 |
| `codex` | `pro-200` | `pro`, `chatgpt-pro` | $200 | shared: $200 / $500 |
| `codex` | `pro-500` | `promax` | $500 | shared: $500 / $1,250 |

The credit column lists `session_5h_credit_usd` / `weekly_7d_credit_usd` unless it names a
single weekly tier ceiling. Credit ceilings are capacity estimates, not subscription charges.
For multi-tier plans, weekly subscription cost is allocated across quota pools in proportion
to their calibrated weekly credit ceilings; each pool exposes its `cost_per_credit`. A free
plan has `cost_per_credit = 0.0`; pay-as-you-go without a subscription plan uses `1.0`.

Configuration layers (`default -> user -> project`) replace an earlier `plan`, apply an
explicit `monthly_cost_usd` override, and deep-merge `credit` overrides over the selected
preset. The former `session_5h_usd` / `weekly_7d_usd` names remain accepted as input aliases
for credit overrides; normalized configuration and emitted credit fields use the explicit
`*_credit_usd` names.

Inspect or persist `[agents.<agent>].plan` from the CLI while preserving existing TOML comments,
keys, and subtables:

```sh
agroute config show --json
agroute config set-plan agy ultra-100 --json
agroute config set-plan claude max-20x --project --json
agroute config set-plan codex promax --json
```

- `exclude_quota_groups = []`: list of quota-group labels (for example `["3p"]` under
  `[agents.agy]`) to exclude while keeping dynamic model discovery active for remaining groups.
  See {ref}`remaining credit budgets <remaining-credit-budgets>`.

(howhard-inventories)=
## Generating `[inventories]` for `howhard.toml`

`howhard` 2.1+ accepts explicitly named `[inventories]` in `howhard.toml` selected via
`howhard ... --inventory NAME` or Python `inventory="NAME"`. Generate a filtered inventory
table for the current host with:

```sh
agroute inventory --host agy --name router
```

When availability changes dynamically with live usage windows, pass the fresh candidate list
from `agroute candidates --host HOST --output FILE` to `howhard ... --candidates FILE`.
