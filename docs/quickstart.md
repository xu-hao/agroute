# Quickstart

You need Python 3.11+, a POSIX shell, Git, `howhard` 2.1+, and a current Codex, Claude Code,
or Antigravity client with native plugin support.

## Install the backend

Run these commands from the project where you want to use `agroute`:

```sh
python3 -m venv .venv-agroute
. .venv-agroute/bin/activate
python -m pip install "agroute==0.1.0" "howhard>=2.1.0"
agroute --version
```

Expect `agroute 0.1.0`. Keep this environment active and start your coding agent from the
same shell so it can find `agroute` and `howhard`.

## Install the native plugin

The Python package supplies the backend. The native plugin supplies the `agroute` skill.
Install the plugin through your agent's own plugin manager.

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

See [native installation and lifecycle](skills.md) for scope and update commands.

## Route a task within live usage limits

1. **Inspect or update native quota state**:

   ```sh
   agroute usage show --json
   agroute usage ingest --agent codex --file /tmp/codex-status.jsonl --json
   ```

2. **Emit filtered `howhard` candidates for the current host**:

   ```sh
   agroute candidates --host agy --output /tmp/task-candidates.json --json
   ```

   `--output` writes the 1–32 cell JSON array expected by `howhard` (`<agent>/<model>` and
   `<effort>`). When `target == host`, `agroute` uses `native_candidates` supported by the
   host's native subagent tool; when `target != host`, `agroute` uses `cli_candidates`
   supported by the target's non-interactive CLI.

3. **Select a cell with `howhard` and resolve the execution plan**:

   ```sh
   howhard context task --text-file /tmp/task.txt --candidates /tmp/task-candidates.json --llm off --json
   # After independent baseline forecast, optional retrieval, and decide:
   agroute plan --host agy --decision /tmp/task-decision.json --prompt-file /tmp/task.txt --json
   ```

4. **Execute and record feedback**:

   - If `execution_mode == "native"`, invoke the host's native delegation tool
     (`invoke_subagent`, `Agent`, or `spawn_agent`) with `invocation.parameters`.
   - If `execution_mode == "cli"`, execute `invocation.argv` and capture the output stream.
   - Follow `feedback` in the returned plan to record the outcome via `howhard observe`,
     attach measured usage via `howhard usage collect` / `attach`, and update `howhard` memory.
