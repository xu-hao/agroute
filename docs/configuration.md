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
- Otherwise the refreshed snapshot from `agroute prices refresh` or the built-in catalog
  prices the cell, whichever took effect later.

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
## Subscription plan presets, credit overrides, and quota-group exclusions

Each `[agents.<name>]` section accepts:

- `plan = ""`: optional subscription plan preset identifier (trimmed and lowercased; `""`
  means no credit preset is configured). Unknown plan names are refused with `AgRouteError`
  listing the valid plans for that agent.
  - **`agy`**:
    - `"pro-20"` (aliases `"pro"`, `"google-ai-pro"`): `gemini` (`session_5h_usd = 32.0`,
      `weekly_7d_usd = 135.0`), `3p` (`session_5h_usd = 5.0`, `weekly_7d_usd = 10.0`).
    - `"ultra-200"` (aliases `"ultra"`, `"google-ai-ultra"`): `gemini` (`session_5h_usd = 320.0`,
      `weekly_7d_usd = 1350.0`), `3p` (`session_5h_usd = 25.0`, `weekly_7d_usd = 50.0`).
  - **`claude`**:
    - `"pro-20"` (aliases `"pro"`, `"claude-pro"`): `session_5h_usd = 18.0`, `weekly_7d_usd = 45.0`.
    - `"max-5x"` (alias `"claude-max-5x"`): `session_5h_usd = 90.0`, `weekly_7d_usd = 225.0`,
      `fable` tier `weekly_7d_usd = 75.0`.
    - `"max-20x"` (alias `"claude-max-20x"`): `session_5h_usd = 360.0`, `weekly_7d_usd = 900.0`,
      `fable` tier `weekly_7d_usd = 300.0`.
  - **`codex`**:
    - `"plus-20"` (aliases `"plus"`, `"chatgpt-plus"`): `session_5h_usd = 20.0`, `weekly_7d_usd = 50.0`.
    - `"pro-200"` (aliases `"pro"`, `"chatgpt-pro"`): `session_5h_usd = 200.0`, `weekly_7d_usd = 500.0`.
- `credit = {}`: optional table overriding `session_5h_usd`, `weekly_7d_usd`, or
  `tiers.<quota_group>.{session_5h_usd, weekly_7d_usd}` (positive numbers). Across configuration
  layers (`default -> user -> project`), `plan` replaces an earlier `plan`, and `credit`
  deep-merges on top of the resolved `plan` preset.
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
