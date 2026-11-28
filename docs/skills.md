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

```{literalinclude} ../AGENTS.snippet.md
:language: text
```

The instruction only says when to use the skills. Exact commands and flags stay in the
`agroute` skill (`skills/agroute/SKILL.md`), so your instructions do not need to change when
the CLI does. The text above is the repository's root `AGENTS.snippet.md`, which the source
distribution also ships.

## Contributor validation

Validate the native plugin manifests without installing plugins globally or making live agent calls:

```sh
agc run --label "native plugin validation" --resource cpu=1 -- .venv/bin/python scripts/check-plugins.py
```
