# agroute

```{image} assets/route.svg
:alt: agroute branching-route mark
:width: 160px
:align: center
```

agroute helps you use your available coding-agent capacity across Codex, Claude Code
and Antigravity. It filters model and reasoning-effort routes using live usage
evidence, passes the eligible inventory to [howhard](https://howhard.readthedocs.io/en/latest/),
and turns the selection into a native delegation or CLI execution plan.

Your host executes the plan and records real outcomes in howhard. Start with the
[quickstart](quickstart.md), or read the [technical report](technical-report.md)
to understand quota filtering, estimated credit budgets and execution planning.

## Getting started

- [Quickstart](quickstart.md) — install the backend and plugin, filter candidates,
  resolve a howhard decision, execute the plan and record feedback.
- [Agent skill and host installation](skills.md) — native setup and coordinating-agent
  instructions for Codex, Claude Code and Antigravity.

```{toctree}
:hidden:
:caption: Getting started

quickstart
skills
```

## Technical deep dive

- [Technical report](technical-report.md) — the complete routing pipeline, overlapping
  quota pools, price provenance, credit calibration and feedback ownership.
- [Evaluating routing and credit estimates](evaluation.md) — measure quota decisions,
  budget estimates, execution fidelity and total cost on genuine work.

```{toctree}
:hidden:
:caption: Technical deep dive

technical-report
evaluation
```

## Reference

- [Routing and storage contracts](reference.md) — exact candidate, quota, pricing,
  execution-plan, monitoring and persistence behavior.
- [Configuration](configuration.md) — optional user/project configuration, model
  surfaces, plan presets, quota policies and named howhard inventories.

For a specific question, go directly to {ref}`quota freshness <native-quota-tracking>`,
{ref}`prices <built-in-rate-cards>`, {ref}`remaining credit <remaining-credit-budgets>`,
{ref}`execution plans <execution-plan-resolution>` or
{ref}`route monitoring <realtime-route-monitoring>`.

```{toctree}
:hidden:
:caption: Reference

reference
configuration
```

## Project

- [Contributor workflow](contributing.md) — issues, isolated worktrees and checks.
- [Packaging and release](releasing.md) — versioning, distribution validation and publication.
- [Changelog](changelog.md) — release history.
- [Image provenance](assets/README.md) — the source and geometry of the branching-route mark.
- [Session handoff](session_handoff_format.md) — reporting completed and open work.

```{toctree}
:hidden:
:caption: Project

contributing
releasing
changelog
assets/README
session_handoff_format
```
