# agroute

*Which agent should run this task within your live usage limits?*

[Read the documentation](https://agroute.readthedocs.io/en/latest/).

`agroute` routes delegated coding-agent tasks across Codex (`codex`), Claude Code
(`claude`), and Antigravity (`agy`) within live subscription quota and rate-limit
windows. It filters available `(agent, model, effort)` candidate routes, emits a
[`howhard` 2.1+](https://github.com/xu-hao/howhard) candidate inventory with
provider-qualified model identities (`<agent>/<model>`), and resolves a `howhard`
decision into a deterministic execution plan.

When the selected target agent matches the current host agent, `agroute` emits a
native delegation tool directive (`invoke_subagent`, `Agent`, or `spawn_agent`).
When the target agent differs from the host, `agroute` emits a non-interactive CLI
execution plan (`codex exec`, `claude -p`, or `agy -p`). `agroute` never spawns
worker processes itself; selection, outcome observation, execution accounting, and
writable semantic memory remain in `howhard`.

## Quickstart

You need Python 3.11+, a POSIX shell, Git, [`howhard` 2.1.0+](https://pypi.org/project/howhard/2.1.0/)
and a current Codex, Claude Code or Antigravity client with native plugin support.

Install the Python backend from [PyPI](https://pypi.org/project/agroute/0.2.0/) in your
project directory:

```sh
python3 -m venv .venv-agroute
. .venv-agroute/bin/activate
python -m pip install "agroute==0.2.0" "howhard>=2.1.0"
agroute --version
```

Expect `agroute 0.2.0`. Keep the environment active and start your agent from the same
shell so it can find `agroute` and `howhard`. An existing GUI session may need the
absolute path to `.venv-agroute/bin/agroute` in your project.

Install the native plugin for your host:

```sh
# Codex
codex plugin marketplace add xu-hao/agroute
codex plugin add agroute@agroute
```

```sh
# Claude Code
claude plugin marketplace add xu-hao/agroute
claude plugin install agroute@agroute
```

```sh
# Antigravity
agy plugin install https://github.com/xu-hao/agroute
agy plugin list
```

The plugin supplies the `agroute` routing skill. Backend and plugin updates are
separate; the [native installation guide](docs/skills.md) covers scopes and lifecycle
commands.

Ask your coordinating agent to use `agroute` and `howhard` on a real, authorized task
before starting a worker:

> Use agroute and howhard to choose the target agent, model and reasoning effort for
> this task within live usage limits. Inspect or refresh quota state, emit eligible
> candidates, obtain an independent baseline and consult memory in howhard, resolve
> the execution plan, execute with the host's native delegation tool or cross-agent
> CLI, then record the actual outcome and measured usage and update memory.

Invoke the skill explicitly with `$agroute` or `/skills` in Codex,
`/agroute:agroute` in Claude Code, or `/agroute` in Antigravity.

To require `agroute` and `howhard` before worker delegation across sessions, paste this
into your repository's `AGENTS.md` or `CLAUDE.md` (see
[requiring agroute before delegation](docs/skills.md#require-agroute-before-delegation)):

```text
Before delegating an authorized task to a worker, use the installed agroute and howhard
skills:
1. Inspect or refresh live agent quota state with `agroute usage`.
2. Emit eligible provider-qualified candidates for this host with
   `agroute candidates --host <codex|claude|agy> --output /tmp/TASK-candidates.json --json`.
3. Select a route with `howhard` (`context`/`retrieve`/`decide` with an independent
   stateless baseline forecast, or `pick`), then resolve the execution plan with
   `agroute plan --host <codex|claude|agy> --decision /tmp/TASK-decision.json --json`.
4. Honor `needs_review`. Execute native plans (`target_agent == host_agent`) with the
   host's native subagent tool and cross-agent plans (`target_agent != host_agent`) with
   the returned non-interactive CLI command.
5. Record the actual outcome and measured usage in `howhard` following the plan's
   `feedback` block (`matched_receipt` or `raw_native`), then update `howhard` memory.
```

The [complete quickstart](docs/quickstart.md) walks through quota inspection, candidate
emission, execution-plan resolution, feedback and memory learning. If all candidate
routes are exhausted or a required execution control is unavailable, `agroute` refuses
selection explicitly.

Inspect local state, discovered models, effective quota windows, eligible candidate
routes, and realtime routing decisions with:

```sh
agroute paths --json
agroute models --json
agroute usage show --json
agroute candidates --host agy --json
agroute list
agroute show <id>
agroute tui
```

`agroute` ships with no hardcoded model list in its packaged defaults: unless
`native_candidates` or `cli_candidates` are explicitly pinned in configuration,
`agroute` automatically discovers the live available models and supported reasoning
efforts from the locally installed agents (`~/.codex/models_cache.json` for Codex,
`~/.claude/cache/model-catalog/` for Claude Code, and `agy models` for Antigravity).

The [routing and storage contracts](docs/reference.md) and
[configuration guide](docs/configuration.md) describe the CLI/API, live model discovery,
native versus CLI candidate surfaces, `host_affinity_epsilon`, `[inventories]`
generation for `howhard.toml`, and the realtime model-route TUI. Contributors use
[isolated worktrees and the documentation-only exemption](docs/contributing.md).
