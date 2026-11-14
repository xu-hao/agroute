# Quickstart

You need Python 3.11+, a POSIX shell, Git, `howhard` 2.1.0+ and a current Codex,
Claude Code or Antigravity client with native plugin support.

## Install the backend

Run these commands from the project where you want to use `agroute`:

```sh
python3 -m venv .venv-agroute
. .venv-agroute/bin/activate
python -m pip install "agroute==0.5.0" "howhard>=2.1.0"
agr --version
```

Expect `agr 0.5.0`. The Python package is available on
[PyPI](https://pypi.org/project/agroute/0.5.0/).

Keep this environment active and start your coding agent from the same shell so it
can find `agr` and `howhard`. If an existing GUI session cannot find it, configure
the absolute path to your project's `.venv-agroute/bin/agr`; reopening a GUI does
not activate the shell's environment.

## Install the native plugin

The Python package supplies the backend. The native plugin supplies the `agroute`
routing skill. Install the plugin through your agent's own plugin manager.

Codex:

```sh
codex plugin marketplace add xu-hao/agroute
codex plugin add agroute@agroute
```

Claude Code:

```sh
claude plugin marketplace add xu-hao/agroute
claude plugin install agroute@agroute
```

Antigravity:

```sh
agy plugin install https://github.com/xu-hao/agroute
agy plugin list
```

These commands use the user's plugin environment. Claude Code also supports explicit
`--scope project` or `--scope local` for both marketplace addition and installation.
Reopen the agent if the skill is not visible. Backend and plugin updates are separate;
see [native installation and lifecycle](skills.md) for update and scope commands.

## Use agroute on a real task

Give the coordinating agent an authorized task and ask it to use `agroute` and `howhard`
before starting a worker. For example:

> Use agroute and howhard for this task: repair transaction rollback and verify its
> failure boundary. Inspect or refresh live agent quota state, emit eligible
> provider-qualified candidates for this host, obtain an independent baseline and
> consult memory in howhard, resolve the execution plan, execute the work, record the
> actual outcome and measured usage under a stable execution ID, then update memory.
> Report exhausted quotas or unsupported controls.

Explicit skill invocation is `$agroute` or `/skills` in Codex, `/agroute:agroute` in
Claude Code, and `/agroute` in Antigravity. Codex can also activate the skill when the
task matches its description. Installing the plugin does not make every worker start
run it automatically.

For persistent use across tasks, copy this {ref}`coordinating-agent instruction <require-agroute-before-delegation>`
into your project's `AGENTS.md` or `CLAUDE.md`:

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

The agent updates or inspects live quota state (`agr usage`), writes the filtered
full eligible candidate list (`agr candidates --host HOST --output FILE`), calls
`howhard` to choose a provider-qualified `<agent>/<model>` and `effort`, and resolves
the decision into an `agroute-execution-plan-v1` payload (`agr plan --host HOST --decision FILE`):

```sh
agr usage show --json
agr candidates --host agy --output /tmp/task-candidates.json --json
```

Before passing candidates to howhard, inspect the `candidates --json` envelope.
If `price_request` is non-null, follow its resolution contract, apply the response
with `agr prices resolve --file PRICE_RESPONSE.json --json`, and emit candidates
again. The {ref}`price resolution contract <calling-agent-price-resolution>` defines
priced, alias and unlisted responses.

```sh
howhard context task --text-file /tmp/task.txt --candidates /tmp/task-candidates.json --json > /tmp/task-context.json
# Complete the returned independent baseline/reader stages with howhard decide:
agr plan --host agy --decision /tmp/task-decision.json --prompt-file /tmp/task.txt --json
```

When `target_agent == host_agent`, `agroute` selects from `native_candidates` and emits
`execution_mode = "native"` with the host's native delegation tool (`swarm` for a
concrete model slug, or `invoke_subagent` for a configured tier, in Antigravity, `Agent` in Claude Code, or `spawn_agent` in Codex). When
`target_agent != host_agent`, `agroute` selects from `cli_candidates` and emits
`execution_mode = "cli"` with the target's non-interactive CLI command (`codex exec`,
`claude -p`, or `agy -p`). Honor `needs_review` before executing or accepting
silent-risk work.

## Record the outcome and learn

After actual execution, follow the `feedback` block in the resolved plan to record the
outcome in `howhard`. When `feedback.mode == "matched_receipt"`, replace `DECISION_ID`
with the returned receipt, `EXECUTION_ID` with a stable ID for that invocation, and
`OUTCOME` with `clean`, `loud` or `silent`:

```sh
howhard observe DECISION_ID OUTCOME --id EXECUTION_ID --json
howhard evidence EXECUTION_ID --raw --json
howhard memory learn-context EXECUTION_ID --json
```

When `feedback.mode == "raw_native"` (for example, when `host_affinity_epsilon`
selected an eligible native host route within `epsilon` of `howhard`'s cross-agent
choice), record raw native feedback with `howhard observe ... --native` using the
command template in `feedback.command_template` without attaching the unexecuted
cross-agent receipt. See {ref}`execution plan resolution <execution-plan-resolution>`
for the full protocol.

Apply the memory writer response with the exact revision and event key returned by
`howhard memory learn-context`:

```sh
howhard memory learn EXECUTION_ID --file WRITER_RESPONSE.json --expected-revision N --event-key EVENT_KEY --json
```

For cross-agent CLI runs that capture an owned JSONL stream (`codex-exec-v1` or
`claude-sdk-v1`), use `howhard usage collect` and `howhard usage attach` to attach
measured token usage to the recorded execution.

## Inspect project state, discovered models, and realtime routes

```sh
agr paths --json
agr models --json
agr usage show --json
agr candidates --host agy --json
agr inventory --host agy --name router
agr list
agr show <id>
agr tui
```

By default, `agroute` automatically discovers available models and reasoning efforts
from your installed agents (`codex`, `claude`, and `agy`) at runtime (`agr models`,
or `agr models --refresh` to refresh the Antigravity CLI cache). `agr tui`
opens a live terminal dashboard of `agr plan` executions and `howhard` routing
decisions across registered repositories, while `agr list` and `agr show <id>`
provide scriptable CLI access.

Git worktrees share repository state in `<git-common-dir>/agroute` while each task
captures its current checkout. Plain folders use local `.agroute`. `paths` shows the
resolved locations; the {ref}`worktree state contract <worktree-state-contract>`
explains project identity checks and explicit `--state-dir` selection.
