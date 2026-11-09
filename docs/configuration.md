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

## Example `agroute.toml`

```toml
[routing]
default_host = "agy"
host_affinity_epsilon = 0.0
namespace_separator = "/"

[agents.agy]
enabled = true
max_session_used_pct = 90.0
max_weekly_used_pct = 95.0
max_tier_used_pct = 95.0
host_reserve_pct = 5.0
usage_probe_cmd = []

[agents.codex]
enabled = true
max_session_used_pct = 90.0
max_weekly_used_pct = 95.0
max_tier_used_pct = 95.0
host_reserve_pct = 0.0
usage_probe_cmd = []

[agents.claude]
enabled = true
usage_stale_after_seconds = 1800
max_session_used_pct = 90.0
max_weekly_used_pct = 95.0
max_tier_used_pct = 95.0
host_reserve_pct = 0.0
usage_probe_cmd = []
```

`agents.claude.usage_stale_after_seconds` is a positive number of seconds (default
`1800`). It controls the freshness warnings in usage and routing output. A recent failed
probe is also reported even within this age bound. Unknown percentages stay `null`, and
active rejections continue to exclude the affected scopes. The setting is accepted for
all agents, but the freshness policy currently applies only to Claude.

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
