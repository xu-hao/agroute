# agroute

*Route delegated agent tasks across local coding agents within live usage limits.*

`agroute` is a configurable, **plan-only** local task router for coding agents (`codex`,
`claude`, and `agy`). Given a host agent and native quota/rate-limit state, it filters
available `(agent, model, effort)` candidate routes, emits a [`howhard` 2.1+](https://github.com/xu-hao/howhard)
candidate inventory with provider-qualified model identities (`<agent>/<model>`), and
resolves a `howhard` decision into a deterministic execution plan:

- **Native host delegation** (`target_agent == host_agent`): emits a structured native
  subagent tool directive for the current host (`invoke_subagent` in Antigravity,
  `Agent` in Claude Code, or `spawn_agent` in Codex).
- **Cross-agent CLI execution** (`target_agent != host_agent`): emits a non-interactive
  CLI execution plan (`codex exec --json`, `claude -p --verbose --output-format stream-json`,
  or `agy -p --output-format stream-json`) with exact model, effort, and output-capture
  adapter metadata.

`agroute` never spawns worker processes itself. Model/effort selection, outcome observation,
portable execution accounting, and writable semantic memory remain in `howhard`.

## Quickstart

You need Python 3.11+, a POSIX shell, Git, [`howhard` 2.1+](https://pypi.org/project/howhard/2.1.0/),
and a current Codex, Claude Code, or Antigravity client with native plugin support.

Install `agroute` and `howhard` in your project environment:

```sh
python3 -m venv .venv-agroute
. .venv-agroute/bin/activate
python -m pip install "agroute==0.1.0" "howhard>=2.1.0"
agroute --version
```

Expect `agroute 0.1.0`. Keep the environment active and start your coding agent from the
same shell so it can find `agroute` and `howhard`.

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

Invoke the skill explicitly with `$agroute` or `/skills` in Codex, `/agroute:agroute` in
Claude Code, or `/agroute` in Antigravity.

## Core workflow

1. **Inspect or update native quota state**:

   ```sh
   agroute usage show --json
   agroute usage ingest --agent codex --file /tmp/codex-status.jsonl --json
   agroute usage set --agent claude --session-used-pct 92 --session-reset-at 2026-11-08T21:00:00Z --json
   ```

2. **Emit filtered `howhard` candidates for the current host**:

   ```sh
   agroute candidates --host agy --output /tmp/task-candidates.json --json
   ```

3. **Select a cell with `howhard`** (calling-agent `context`/`retrieve`/`decide` or `pick`)
   and **resolve the decision into an execution plan**:

   ```sh
   agroute plan --host agy --decision /tmp/task-decision.json --prompt-file /tmp/task.txt --json
   ```

4. **Execute the returned plan** (native tool call when `target_agent == host_agent`, or
   CLI command when `target_agent != host_agent`) and **record the outcome in `howhard`**:

   ```sh
   howhard observe DECISION_ID clean --id EXECUTION_ID --json
   ```

See the [quickstart](docs/quickstart.md), [documentation index](docs/index.md),
[routing and storage contracts](docs/reference.md), [configuration guide](docs/configuration.md),
[skill guide](docs/skills.md), and [changelog](CHANGELOG.md). Contributors use
[AGCoord and isolated worktrees](docs/contributing.md).
