# Host execution surfaces: native delegation vs. cross-agent CLI

`agroute` separates an agent's **native delegation tool** (used when `target_agent == host_agent`)
from its **non-interactive CLI** (used when `target_agent != host_agent`). Configure
`native_candidates` and `cli_candidates` per agent in `agroute.toml` to match the controls each
surface actually enforces.

| Agent ID | Invoke skill | Native host delegation (`target == host`) | Cross-agent CLI (`target != host`) |
| --- | --- | --- | --- |
| `codex` | `$agroute` or `/skills` | Tool: `spawn_agent` with `model` and `reasoning_effort` | `codex exec --json --model <MODEL> -c model_reasoning_effort=<EFFORT> -- "<PROMPT>"` |
| `claude` | `/agroute:agroute` or `/agroute` | Tool: `Agent` with `subagent_type`, `description`, `model`, `effort` | `claude -p --verbose --output-format stream-json --model <MODEL> --effort <EFFORT> -- "<PROMPT>"` |
| `agy` | `/agroute` | Tool: `invoke_subagent` with `TypeName`, `Model` (`inherit\|flash_lite\|flash\|pro`), `Role`, `Workspace` | `agy -p --output-format stream-json --model <MODEL> --effort <EFFORT> -- "<PROMPT>"` |

## Provider-qualified identities for `howhard`

`howhard` 2.1+ evaluates `(model, effort)` cells and treats provider-qualified model IDs as
opaque identities. `agroute candidates` prefixes every bare model ID with its agent ID:

- `agy/pro` (`effort = "standard"`) when `agy` is the host agent (`native_candidates`)
- `agy/gemini-2.5-pro` (`effort = "high"`) when routing to `agy` via CLI (`cli_candidates`)
- `codex/gpt-5.4` (`effort = "high"`)
- `claude/claude-sonnet-4-6` (`effort = "high"`)

`agroute plan` verifies the qualified cell against the current host's active eligible pool,
strips the `<agent>/` prefix for `invocation.parameters` or `invocation.argv`, and retains
the exact `<agent>/<model>` string in `howhard_cell` and `feedback` so `howhard observe`
matches the decision receipt.
