# agroute

*Route delegated agent tasks across local coding agents within live usage limits.*

`agroute` is a configurable, plan-only local router for coding agents (`codex`,
`claude`, and `agy`). Given a host agent and explicit native usage-limit evidence,
it filters available `(agent, model, effort)` candidate routes, emits a
`howhard`-compatible candidate inventory with provider-qualified model identities
(`<agent>/<model>`), and resolves a `howhard` decision into a deterministic
execution plan:

- **Native host delegation** (`target_agent == host_agent`): emits a structured
  native subagent tool directive for the current host (`invoke_subagent` in
  Antigravity, `Agent` in Claude Code, or native delegation in Codex).
- **Cross-agent CLI execution** (`target_agent != host_agent`): emits a
  non-interactive CLI execution plan (`codex exec`, `claude -p`, or `agy -p`)
  with exact model, effort, and output-capture controls.

`agroute` never spawns worker processes itself. Selection, outcome observation,
portable execution accounting, and writable semantic memory remain in `howhard`.

## Status

Under construction. The specification, architecture, and build plan are tracked
in this repository's issues.
