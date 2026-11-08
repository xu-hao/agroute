# Configuration

`agroute` layers three configuration sources in order:

1. Packaged defaults (`agroute/data/default_config.toml`).
2. Optional user configuration at `${XDG_CONFIG_HOME:-~/.config}/agroute/config.toml`
   (or `AGROUTE_USER_CONFIG`; disable with `--no-user-config`).
3. Project `agroute.toml` in the current directory, or `--config PATH`.

Unknown keys, out-of-range percentages, negative `host_affinity_epsilon`, bare models containing
`/` or `:`, and `model_default` effort strings are refused before any state is modified.

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

[[agents.agy.native_candidates]]
model = "pro"
effort = "standard"
tier = "pro"

[[agents.agy.native_candidates]]
model = "flash"
effort = "standard"
tier = "flash"

[[agents.agy.cli_candidates]]
model = "gemini-2.5-pro"
effort = "high"
tier = "pro"

[agents.codex]
enabled = true
max_session_used_pct = 90.0
max_weekly_used_pct = 95.0
max_tier_used_pct = 95.0
host_reserve_pct = 0.0
usage_probe_cmd = []

[[agents.codex.native_candidates]]
model = "gpt-5.4"
effort = "high"

[[agents.codex.cli_candidates]]
model = "gpt-5.4"
effort = "high"

[[agents.codex.cli_candidates]]
model = "gpt-5.4"
effort = "medium"

[agents.claude]
enabled = true
max_session_used_pct = 90.0
max_weekly_used_pct = 95.0
max_tier_used_pct = 95.0
host_reserve_pct = 0.0
usage_probe_cmd = []

[[agents.claude.native_candidates]]
model = "claude-sonnet-4-6"
effort = "high"
tier = "sonnet"

[[agents.claude.cli_candidates]]
model = "claude-opus-4-6"
effort = "high"
tier = "opus"

[[agents.claude.cli_candidates]]
model = "claude-sonnet-4-6"
effort = "high"
tier = "sonnet"
```

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
