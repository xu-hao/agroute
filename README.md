# agroute

<img src="docs/assets/route.svg" alt="agroute branching-route mark" width="160" height="160">

*Which agent should run this task within your live usage limits?*

[Read the documentation](https://agroute.readthedocs.io/en/latest/).

Start with the [quickstart](docs/quickstart.md), read the
[technical report](docs/technical-report.md) for the routing and credit model, or
use the [evaluation guide](docs/evaluation.md) to measure it on real work.

`agroute` routes delegated coding-agent tasks across Codex (`codex`), Claude Code
(`claude`), and Antigravity (`agy`) within live subscription quota and rate-limit
windows. It filters available `(agent, model, effort)` candidate routes, emits a
`howhard` 2.1+ candidate inventory with
provider-qualified model identities (`<agent>/<model>`), and resolves a `howhard`
decision into a deterministic execution plan.

When the selected target agent matches the current host agent, `agroute` emits a
native delegation tool directive (`swarm` or `invoke_subagent`, `Agent`, or `spawn_agent`).
When the target agent differs from the host, `agroute` emits a non-interactive CLI
execution plan (`codex exec`, `claude -p`, or `agy -p`). `agroute` never spawns
worker processes itself; selection, outcome observation, execution accounting, and
writable semantic memory remain in `howhard`.

## Quickstart

You need Python 3.11+, a POSIX shell, Git, [`howhard` 2.1.0+](https://pypi.org/project/howhard/2.1.0/)
and a current Codex, Claude Code or Antigravity client with native plugin support.

Install the Python backend from [PyPI](https://pypi.org/project/agroute/0.4.0/) in your
project directory:

```sh
python3 -m venv .venv-agroute
. .venv-agroute/bin/activate
python -m pip install "agroute==0.4.0" "howhard>=2.1.0"
agr --version
```

Expect `agr 0.4.0`. Keep the environment active and start your agent from the same
shell so it can find `agr` and `howhard`. An existing GUI session may need the
absolute path to `.venv-agroute/bin/agr` in your project.

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

Ask your coordinating agent to use the installed `agroute` skill and `howhard` on a real, authorized task
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
1. Inspect or refresh live agent quota state with `agr usage`.
2. Emit eligible provider-qualified candidates for this host with
   `agr candidates --host <codex|claude|agy> --output /tmp/TASK-candidates.json --json`.
   If `price_request` is non-null, look up official first-party provider pricing (or classify
   internal harness models as `"alias"` or `"unlisted"`), run
   `agr prices resolve --file /tmp/TASK-prices.json --json`, and re-emit candidates once.
3. Select a route with `howhard` (`context`/`retrieve`/`decide` with an independent
   stateless baseline forecast, or `pick`), then resolve the execution plan with
   `agr plan --host <codex|claude|agy> --decision /tmp/TASK-decision.json --json`.
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
agr paths --json
agr models --json
agr usage show --json
agr candidates --host agy --json
agr list
agr show <id>
agr tui
```

`agroute` ships with no hardcoded model list in its packaged defaults: unless
`native_candidates` or `cli_candidates` are explicitly pinned in configuration,
`agroute` automatically discovers the live available models and supported reasoning
efforts from the locally installed agents (`~/.codex/models_cache.json` for Codex,
`~/.claude/cache/model-catalog/` for Claude Code, and `agy models` for Antigravity).

Emitted candidates are priced without configuration. Every cell whose model has a known
price carries a per-token `howhard-rate-card-v1` `rate_card`, so `howhard` can compare
worker cost across agents. The prices come from a built-in catalog of Anthropic, OpenAI and
Google list prices. The catalog is hardcoded and was verified against each provider's
pricing page at `2026-11-12T02:18:20Z`. A release updates that timestamp whenever the
prices are re-checked. A published scheduled change, such as `gemini-4-argon` on
2026-12-01, takes effect on its date. A price you configure always wins. A model with no
price stays unpriced, and one unpriced cell turns `howhard`'s cost comparison off for the
whole inventory. The [rate-card contract](docs/reference.md#rate-cards-on-emitted-candidates)
lists every price, the category mapping and the precedence rules.

### Guessed prices

Antigravity's `invoke_subagent` tiers name no concrete model, so no price is published for
them. When configured as native candidates, each tier carries a labelled guess, with a `source` starting `guess:` and
`version = "agroute-guess-2026-11-12"`:

- `agy/pro` is priced as `gemini-4-argon`, the Gemini 4 reasoning model Antigravity offers.
- `agy/flash` is priced as `gemini-3.8-flash`, Antigravity's baseline model.
- `agy/flash_lite` is priced as `gemini-3.5-flash-lite`, the newest Flash-Lite on Google's
  pricing page.

`gpt-oss-120b` on Antigravity has no first-party list price. It carries a published
open-weight market estimate (`provider = "openai-oss"`), which is not an official first-party
list price: `input` 0.15, `output` 0.60 and `cache_read` 0.075 USD per million tokens, with
writes at input.

### Overriding a price

Set a per-model price in `~/.config/agroute/config.toml` or a project `agroute.toml`.
Discovery stays on. An entry is either all six rates or `usd_per_mtok` alone. It can
replace a guess or price a model the catalog lacks:

```toml
[agents.agy.prices."pro"]
input = 2.0
output = 10.0
cache_read = 0.10
cache_write = 2.0
cache_write_5m = 2.0
cache_write_1h = 2.0
```

The rates shown are placeholders; replace them with your own price. agroute refuses a
partial, unknown or negative entry when it loads the configuration. A price on a
configured candidate beats a per-model price, and both beat calling-agent resolved and
built-in prices. The [per-model price guide](docs/reference.md#per-model-user-prices) has
the details.

### Resolving newly discovered models offline (`agr prices`)

`agroute` never fetches prices over the network. Whenever `agr candidates --json`,
`agr inventory --json`, or `agr prices pending --json` discovers a model whose
catalog key is missing from configuration, the built-in catalog, and non-expired
`<state_root>/prices.json` (or has expired), it emits a `price_request` payload listing the
unknown model keys, sample slugs, and display names along with a resolution `contract`.
The calling agent looks up official first-party provider pricing (or classifies an internal
harness model as an `"alias"` of a known catalog key or `"unlisted"`) and persists the
resolution locally via `agr prices resolve --file <path> --json` (inspected with
`agr prices show --json`). `"priced"` and `"alias"` entries carry a 30-day default TTL,
while `"unlisted"` negative-cache entries carry a 7-day default TTL so unpriced internal
models do not re-prompt on every call. See
[calling-agent price resolution](docs/reference.md#calling-agent-price-resolution).

### Subscription cost and quota credit

Plan presets expose monthly subscription cost (`monthly_cost_usd`) and derive the 7-day
amortized cost (`weekly_cost_usd`) separately from session and weekly quota-credit ceilings.
For multi-tier plans, weekly subscription cost is allocated across pools by calibrated weekly
credit ceiling; candidates carry `cost_per_credit` for backend cost ranking. After a decision,
`session_cost_usd` reports estimated session cost while `needed_credit_usd` is the amount
checked against the live credit budget.

Calibration estimates each plan's quota-credit ceiling from completed observations and
non-resetting quota intervals. New measurements use `needed_credit_usd` or `credit_usd`;
older `cost_usd` records remain readable as list-price credit evidence. Explicit
`[agents.<name>].credit` overrides remain exact. The emitted `credit_budget` names its
available balance `available_credit_usd` and provides the separate
`session_5h_remaining_credit_usd` / `weekly_7d_remaining_credit_usd` values when known. See
[configuration](docs/configuration.md) and the [routing reference](docs/reference.md) for
preset values, migration details and exact output fields.

The [routing and storage contracts](docs/reference.md) and
[configuration guide](docs/configuration.md) describe the CLI/API, live model discovery,
native versus CLI candidate surfaces, `host_affinity_epsilon`, `[inventories]`
generation for `howhard.toml`, and the realtime model-route TUI. Contributors use
[isolated worktrees and the documentation-only exemption](docs/contributing.md).
