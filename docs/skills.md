# Native installation of the agroute skill

All three supported hosts use the common Agent Skills format (`skills/agroute/SKILL.md` with
`references/hosts.md`, `references/workflow.md`, and `references/usage.md`). Thin native
plugin manifests discover the skill under `skills/`.

## First-party plugin installation

| Host | Register/install | Explicit invocation |
| --- | --- | --- |
| Codex | `codex plugin marketplace add xu-hao/agroute`, then `codex plugin add agroute@agroute` | `$agroute` or `/skills` |
| Claude Code | `claude plugin marketplace add xu-hao/agroute`, then `claude plugin install agroute@agroute` | `/agroute:agroute` |
| Antigravity (`agy`) | `agy plugin install https://github.com/xu-hao/agroute` | `/agroute` |

For a local checkout, substitute `/path/to/agroute` for `xu-hao/agroute`. The portable root
`plugin.json` and `.agents/plugins/marketplace.json` support Codex; `.claude-plugin/plugin.json`
and `.claude-plugin/marketplace.json` support Claude Code; `.antigravity-plugin/plugin.json`
supports Antigravity.

(require-agroute-before-delegation)=
## Require `agroute` and `howhard` before delegation

Add the following instruction to your project's persistent agent instructions (`AGENTS.md` or
`CLAUDE.md`) after installing `agroute` and `howhard`:

```text
Before delegating an authorized task to a worker, use the installed agroute and howhard
skills:
1. Inspect or refresh live agent quota state with `agr usage`.
2. Emit eligible provider-qualified candidates for this host with
   `agr candidates --host <codex|claude|agy> --output /tmp/TASK-candidates.json --json`.
3. Select a route with `howhard` (`context`/`retrieve`/`decide` with an independent
   stateless baseline forecast, or `pick`), then resolve the execution plan with
   `agr plan --host <codex|claude|agy> --decision /tmp/TASK-decision.json --json`.
4. Honor `needs_review`. Execute native plans (`target_agent == host_agent`) with the
   host's native subagent tool and cross-agent plans (`target_agent != host_agent`) with
   the returned non-interactive CLI command.
5. Record the actual outcome and measured usage in `howhard` following the plan's
   `feedback` block (`matched_receipt` or `raw_native`), then update `howhard` memory.
```

## Contributor validation

Validate the native plugin manifests without installing plugins globally or making live agent calls:

```sh
agc run --label "native plugin validation" --resource cpu=1 -- .venv/bin/python scripts/check-plugins.py
```
