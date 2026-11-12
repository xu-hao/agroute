# Routing and storage contracts

`agroute` is a plan-only router that connects native agent quota state to `howhard` 2.1+
candidate selection and host execution surfaces.

(candidate-qualification)=
## Candidate qualification, dynamic model discovery, and surface separation

An agent's native delegation tool (used when `target_agent == host_agent`) exposes different
controls from its non-interactive CLI (used when `target_agent != host_agent`). Each agent
supports two distinct candidate surfaces in `agroute.toml`:

- `native_candidates`: candidate `(model, effort)` cells enforceable through the host's
  native delegation tool (`invoke_subagent` in Antigravity, `Agent` in Claude Code, or
  `spawn_agent` in Codex).
- `cli_candidates`: candidate `(model, effort)` cells enforceable through the target's
  non-interactive CLI (`codex exec`, `claude -p`, or `agy -p`).

By default, `agroute` leaves `native_candidates` and `cli_candidates` empty in its packaged
defaults and automatically discovers the currently available models and reasoning efforts
from the locally installed agents at runtime:

- **Codex (`codex`)**: reads `${CODEX_HOME:-~/.codex}/models_cache.json`, keeps visible models
  from the highest available numeric generation in model slugs (opaque unversioned IDs
  are retained), ordered by `priority`, and emits every supported reasoning level
  (`supported_reasoning_levels`).
- **Claude Code (`claude`)**: reads the newest catalog in
  `${CLAUDE_CONFIG_DIR:-~/.claude}/cache/model-catalog/*.json`, selects `main` (`quick_select`)
  models (falling back to all catalog models only when none are marked), infers quota tier
  (`opus`, `fable`, `sonnet`, `haiku`), and emits every `runtime.effort_levels` value.
- **Antigravity (`agy`)**: uses the host `invoke_subagent` schema tiers (`pro`, `flash`,
  `flash_lite` at `standard` effort) for `native_candidates`, and queries `agy models` (cached
  in `${AGROUTE_SPOOL_DIR:-~/.local/share/agroute}/agy_models_cache.json`, refreshable via
  `agroute models --refresh`) for `cli_candidates`, retaining every listed model.

Inspect discovered or configured models per agent and surface with `agroute models [--refresh] [--json]`.
Explicit `[[agents.<name>.native_candidates]]` or `[[agents.<name>.cli_candidates]]` entries in
`agroute.toml` or `~/.config/agroute/config.toml` override runtime discovery for that surface.

`agroute candidates --host HOST` evaluates enabled agents, selects `native_candidates` for
`agent == HOST` and `cli_candidates` for `agent != HOST`, filters out quota-blocked agents
and model tiers, and prefixes every bare model ID with `<agent>/` (or `<agent>:` when
`namespace_separator = ":"`). `model_default`, `null`, and empty effort controls are refused.
The emitted list is the full eligible inventory with no model-count or cell-count cap.
Efforts appear in canonical order (`low`, `medium`, `high`, `xhigh`, `max`), followed by
additional efforts in their declared order. Missing effort catalogs retain the declared
default effort (or `medium`). Use `--include-agents` to restrict agents by policy.
Quota exclusions remain visible in `excluded` with their reasons. If all routes are
exhausted, `agroute` fails closed with an `AgRouteError` naming each exclusion reason.
With `candidates --json`, the failing command also emits an empty `candidates` list,
structured `excluded` reasons, and `warnings` before exiting with status 1.
Consumer limits are validated by the consumer; agroute never drops cells to fit them.

(built-in-rate-cards)=
## Rate cards on emitted candidates

`howhard` counts worker money only when every candidate cell carries a price. `candidates`
and `inventory` (JSON and TOML) therefore price every emitted cell they can, with no
configuration needed, using a per-token `howhard-rate-card-v1` `rate_card`. A card has
these fields:

- `format = "howhard-rate-card-v1"`.
- `source`: where the price came from.
- `version`: which catalog, snapshot or guess table supplied it.
- `effective_at`: when the price became effective.
- `provider`: whose price list it is.
- `model`: the emitted provider-qualified cell model, for example `claude/claude-opus-5-5` or
  `agy/gemini-4-argon-high`. It follows `namespace_separator`, because `howhard` rejects a card
  whose `model` differs from its cell's.
- `service_tier = "standard"` and `basis = "estimate"`.
- `rates`: all six categories (`input`, `output`, `cache_read`, `cache_write`,
  `cache_write_5m`, `cache_write_1h`) in USD per million tokens.

Effort never changes a price. The per-token price is fixed by the model, and effort enters
`howhard`'s comparison through its per-cell token forecasts.

(price-precedence)=
### Precedence

For each cell, the first rule that applies sets its price. Higher rules come first:

1. **A price on the configured candidate**: its `usd_per_mtok` or `rate_card` in
   `native_candidates` or `cli_candidates`.
2. **A per-model user price**: `[agents.<agent>.prices."<model>"]`. See
   {ref}`per-model-prices`.
3. **The later-effective of the refreshed snapshot entry and the built-in card.** A refreshed
   entry is effective from its `fetched_at`. A built-in card is effective from the start of
   its period in effect at the cell's `evaluated_at`. On an exact tie, the built-in card wins.
   Either side applies only once its `effective_at` has passed.
4. **Unpriced.** The cell carries neither `usd_per_mtok` nor `rate_card`.

agroute resolves a cell's catalog key by exact rules only:

- **Claude Code and Codex**: the cell's model ID.
- **Antigravity CLI**: the model ID with exactly `-<cell effort>` removed from the end, when it
  ends that way. For example, `gemini-4-argon-high` at `high` resolves to `gemini-4-argon`, and
  `claude-opus-5-5-low` at `low` resolves to the Anthropic `claude-opus-5-5` card.
  `gemini-4-argon-high` at `low` keeps its full ID and is unpriced.
- **Antigravity native tiers** (`pro`, `flash`, `flash_lite`): an exact tier table of
  {ref}`labelled guesses <guessed-rate-cards>`. `inherit` runs the caller's model and stays
  unpriced.

Per-model user prices are keyed by the cell's bare model ID exactly as discovered or
configured, for example `gpt-oss-120b-medium` or `flash`.

### The built-in catalog

The catalog is a hardcoded snapshot of each provider's published list prices. It was
verified against the providers' pricing pages at `2026-11-12T02:18:20Z`, and a release
updates that timestamp whenever the prices are re-checked. A built-in card's `effective_at`
is the later of the verification time and its period's start:

- A price already in force at verification is effective from `2026-11-12T02:18:20Z`.
- A published scheduled change is effective from its own start, at UTC midnight. For
  example, `gemini-4-argon` changes on `2026-12-01T00:00:00Z`.
- No built-in card applies before the verification time.

| Provider | Source (`source`, `version`) | Mapping onto the six categories |
| --- | --- | --- |
| Anthropic | [Claude API pricing](https://platform.claude.com/docs/en/about-claude/pricing), `anthropic-list-2026-11-12` | `input` = base input; `output`; `cache_read` = cache hits and refreshes; `cache_write` and `cache_write_5m` = 5-minute cache write; `cache_write_1h` = 1-hour cache write. |
| OpenAI | [OpenAI API pricing](https://developers.openai.com/api/docs/pricing), `openai-list-2026-11-12` | Standard short-context rows. `cache_read` = cached input. `cache_write`, `cache_write_5m` and `cache_write_1h` all equal the default 30-minute cache write (1.25x input). The optional 12-hour write (2.5x input) is not represented: Codex writes at the default lifetime, and `howhard`'s Codex usage collection records every Codex write as `cache_write`, with zero 5m and 1h tokens. |
| Google | [Google Cloud generative AI pricing](https://cloud.google.com/vertex-ai/generative-ai/pricing) (Global, Standard), `google-cloud-list-2026-11-12` | `cache_read` = cached input (implicit caching). All three write categories equal input, because implicit caching bills writes as ordinary input. The per-hour storage price of explicit caches is excluded. Antigravity overage credits are consumed at Gemini Enterprise (Google Cloud) consumption pricing. For these models, the Global rates equal the Gemini Developer API list prices. |
| OpenRouter (third-party listing) | `https://openrouter.ai/api/v1/models (openrouter/openai/gpt-oss-120b)`, `openrouter-listing-2026-11-12` | Only for `gpt-oss-120b`, which has no first-party list price. This is a third-party listed rate, not an official one. `input` = prompt; `output` = completion; `cache_read` = cache read. All three writes equal input, because no write price is listed. |

Two providers price by prompt length, and the catalog picks one row for each:

| Model | Row used | Excluded alternative | Reason |
| --- | --- | --- | --- |
| `claude-haiku-5-5` | Prompts over 100,000 tokens | Prompts up to 100,000 tokens (0.10 / 0.125 / 0.20 / 0.01 / 0.50) | Agent sessions routinely exceed 100K tokens of context. |
| OpenAI `gpt-6*` | Short context, up to 272K input tokens | Long context: over 272K input tokens costs 2x input and cache rates and 1.5x output for the whole request | The threshold is far above typical agent prompts, and OpenAI states that Codex subscription usage has no long-context surcharge. |

Catalog prices in USD per million tokens:

| Model | `input` | `output` | `cache_read` | `cache_write` / `cache_write_5m` | `cache_write_1h` |
| --- | --- | --- | --- | --- | --- |
| `claude-fable-5-2`, `claude-mythos-5-2` | 8 | 40 | 0.20 | 10 | 16 |
| `claude-fable-5-1`, `claude-mythos-5-1` | 10 | 50 | 0.25 | 12.50 | 20 |
| `claude-fable-5`, `claude-mythos-5` | 10 | 50 | 1 | 12.50 | 20 |
| `claude-opus-5-5` | 4 | 20 | 0.20 | 5 | 8 |
| `claude-opus-5`, `claude-opus-4-8`, `claude-opus-4-7`, `claude-opus-4-6` | 5 | 25 | 0.50 | 6.25 | 10 |
| `claude-sonnet-5-5` | 2 | 10 | 0.10 | 2.50 | 4 |
| `claude-sonnet-5` | 2 | 10 | 0.20 | 2.50 | 4 |
| `claude-sonnet-4-6` | 3 | 15 | 0.30 | 3.75 | 6 |
| `claude-haiku-5-5` (over 100K row) | 0.50 | 2.50 | 0.05 | 0.625 | 1 |
| `claude-haiku-4-5`, `claude-haiku-4-5-20251001` | 1 | 5 | 0.10 | 1.25 | 2 |
| `gpt-6-astra` | 10 | 50 | 1 | 12.50 | 12.50 |
| `gpt-6-sol` | 2 | 10 | 0.20 | 2.50 | 2.50 |
| `gpt-6.1-sol` | 2 | 10 | 0.10 | 2.50 | 2.50 |
| `gpt-6.1-terra` | 1 | 5 | 0.05 | 1.25 | 1.25 |
| `gpt-6-luna` | 0.10 | 0.50 | 0.01 | 0.125 | 0.125 |
| `gemini-4-argon` until 2026-12-01 | 2 | 10 | 0.10 | 2 | 2 |
| `gemini-4-argon` from 2026-12-01 | 4 | 20 | 0.20 | 4 | 4 |
| `gemini-3.8-flash` until 2027-01-01 | 0.75 | 3.75 | 0.075 | 0.75 | 0.75 |
| `gemini-3.8-flash` from 2027-01-01 | 1.50 | 7.50 | 0.15 | 1.50 | 1.50 |
| `gemini-3.5-flash-lite` | 0.30 | 2.50 | 0.03 | 0.30 | 0.30 |
| `gpt-oss-120b` (OpenRouter listing) | 0.15 | 0.60 | 0.075 | 0.15 | 0.15 |

(guessed-rate-cards)=
### Guessed rate cards: not published prices

Antigravity's native subagent tiers name no concrete model, so no price is published for
them. So that a cross-agent inventory stays fully priced, agroute prices each tier as one
concrete model and labels the card as a guess:

- `basis = "estimate"`;
- `version = "agroute-guess-2026-11-12"`;
- `source` reads `guess: no published price for the Antigravity <tier> tier; priced as
  <model> from <version>`, where `<version>` names the catalog or snapshot that priced the
  model.

| Cell | Priced as | Reasoning |
| --- | --- | --- |
| `agy/pro` | `gemini-4-argon`, including its 2026-12-01 change | Antigravity describes `pro` only as a larger, more capable model. Gemini 4 Argon is the Gemini 4 reasoning model that Antigravity offers on paid plans. |
| `agy/flash` | `gemini-3.8-flash`, including its 2027-01-01 change | `flash` is described as a smaller, faster model. Gemini 3.8 Flash is Antigravity's baseline model. |
| `agy/flash_lite` | `gemini-3.5-flash-lite` | `flash_lite` is described as a very light model. Gemini 3.5 Flash-Lite is the newest Flash-Lite model on Google's pricing page. |

To replace a guess, set a per-model price for the tier, for example
`[agents.agy.prices."pro"]`, as described below.

(per-model-prices)=
### Per-model user prices

Price any model by its bare model ID under `agents.<agent>.prices`, in the user
configuration or a project `agroute.toml`. Discovery stays on. The model need not be in the
built-in catalog, so this is also how you price an unpriced cell.

```toml
# A full per-token card: all six categories, USD per million tokens.
[agents.claude.prices."claude-opus-5-5"]
input = 4.0
output = 20.0
cache_read = 0.20
cache_write = 5.0
cache_write_5m = 5.0
cache_write_1h = 8.0

# Or a legacy blended price on its own.
[agents.agy.prices."flash"]
usd_per_mtok = 0.9
```

Each entry is strict and must take one of two forms:

- `usd_per_mtok` alone, a positive number;
- all six rate categories, each a non-negative number.

agroute refuses a partial, combined, unknown, non-numeric or negative entry at config load
with an error that names the entry. It never merges an entry with a built-in card. Later
configuration layers replace an earlier layer's entry for the same model only.

A six-rate entry is emitted as a card with these fields:

- `source = "user config"` and `version = "user-config"`;
- `provider` set to the agent ID;
- `basis = "estimate"`;
- `effective_at` set to the evaluation time.

A `usd_per_mtok` entry is emitted as the cell's `usd_per_mtok`. Mixing money bases has a
cost: `howhard` counts worker money only when every cell uses one basis, so a
`usd_per_mtok` cell next to rate cards turns the comparison off. Prefer six-rate entries.

(refreshing-prices)=
### Refreshing prices

`agroute prices refresh [--json]` is the only command that reads prices from the network.
`candidates`, `inventory`, `plan` and routing never fetch.

**What it fetches.** It fetches `https://openrouter.ai/api/v1/models` and maps OpenRouter
IDs to agroute model IDs through an explicit table, for example:

- `anthropic/claude-opus-5.5` maps to `claude-opus-5-5`;
- `openai/gpt-6.1-sol` maps to `gpt-6.1-sol`;
- `google/gemini-4-argon` maps to `gemini-4-argon`;
- `openai/gpt-oss-120b` maps to `gpt-oss-120b`;
- `anthropic/claude-haiku-4.5` maps to both Haiku 4.5 IDs.

`:batch` variants and unmapped IDs are ignored. Per-token USD strings are converted to USD
per million tokens.

**Category mapping.** Each provider's categories map by the built-in catalog's rule for that
provider:

- **Anthropic**: prompt, completion, cache read, cache write (5-minute) and 1-hour cache write.
- **OpenAI**: every write equals the listed cache write. The long-context `overrides`
  (prompts over 272K tokens) are ignored.
- **Google**: every write equals the prompt price. OpenRouter's Google `input_cache_write` is
  ignored, because it is a storage-derived charge, not a write price. For `gemini-3.8-flash`
  it is 0.0417, which equals $0.50/MTok/hour of storage x 5/60 hours.
- **`gpt-oss-120b`**: writes equal the prompt price.

`claude-haiku-5-5` takes the over-100K tier from the listing's `overrides`. If the listing
carries no such tier, the built-in card stays in use, and the report says so. OpenRouter
lists no Mythos models, so those stay built-in only.

**The snapshot.** Refresh writes `<state_root>/prices.json` atomically (see
`agroute paths`; `--state-dir` selects another store). The snapshot records:

- `fetched_at`;
- the source URL;
- a SHA-256 of the response;
- the mapped six-category rates per model.

A refreshed card has these fields:

- `provider = "openrouter"`;
- `source = "https://openrouter.ai/api/v1/models (openrouter/<id>)"`;
- `version = "openrouter-<fetched_at>"`, so a stale snapshot is visible on every cell;
- `effective_at` set to `fetched_at`.

`--now` overrides the recorded fetch time.

**Failure.** Refresh makes at most three attempts, with 1- and 2-second backoff, and then
exits nonzero naming each attempt's cause. Every failure leaves the previous snapshot
intact. Failures include a network or HTTP error, a non-JSON or malformed response, a
mapped model missing from the response, and a missing or negative price. Refresh never
writes partially, and never reports success after a fallback. A refresh replaces an
unreadable snapshot and reports the error. `candidates` refuses an unreadable snapshot and
asks for a refresh.

**Effect on routing.** Snapshot and built-in prices compare by `effective_at`
({ref}`precedence rule 3 <price-precedence>`). A worked example for `gemini-4-argon`, with a
refresh fetched on 2026-11-20:

- Evaluating on 2026-11-25, the refreshed price wins over the built-in period that started
  at verification on 2026-11-12.
- Evaluating on 2026-12-02, the built-in period starting 2026-12-01 is newer and wins, until
  a later refresh.
- A refresh fetched before the catalog's verification time loses to the built-in card.
- A refresh fetched at exactly the verification time ties, and the built-in card wins.

**Diff report.** Each refresh prints, per model, the price in use before the refresh and
after it:

- every changed category, as old to new;
- models added or dropped;
- each side's `version`, which shows the previous snapshot's fetch time;
- models kept on their built-in card.

`--json` emits the same report as `agroute-price-refresh-v1`. Known drift: OpenRouter lists
`gemini-3.8-flash` at 0.825 / 4.125, against Google Cloud's Global 0.75 / 3.75. That is
Google's non-global regional rate.

(pricing-unpriced-cells)=
### Unpriced cells turn off cost comparison

One unpriced cell turns `howhard`'s worker-money comparison off for the whole inventory, and
`howhard` then compares only risk and time. A cell is unpriced when no rule above prices
it. Examples:

- Antigravity's native `inherit` tier, when configured;
- Antigravity CLI models outside the catalog, such as `gemini-4-carbon-<effort>`,
  `gemini-3.7-flash-<effort>`, `gemini-3.6-flash-<effort>` and `gemini-3.1-pro-<effort>`,
  which other Antigravity plans list;
- Claude or Codex models released after the catalog was verified.

Price such a cell with a per-model entry. Use the cell's bare model ID, which for an
Antigravity CLI cell includes its effort suffix:

```toml
[agents.agy.prices."gemini-4-carbon-high"]
input = 4.0
output = 20.0
cache_read = 0.20
cache_write = 4.0
cache_write_5m = 4.0
cache_write_1h = 4.0
```

The rates shown are placeholders; replace them with your own price. To exclude unpriced
cells instead, use one of these:

- restrict routing with `--include-agents`;
- list the surface's candidates without those cells;
- disable the agent with `[agents.agy] enabled = false`.

(native-quota-tracking)=
## Native quota and rate-limit tracking

`agroute` tracks pre-task subscription quota windows in `<state_root>/usage.json`:

- `session_5h`: 5-hour rolling session window (`used_pct` in `0.0..100.0`, `reset_at` ISO-8601 UTC).
- `weekly_7d`: 7-day rolling weekly window (`used_pct` in `0.0..100.0`, `reset_at` ISO-8601 UTC).
- `tiers`: optional per-model or per-tier windows (such as `opus`, `sonnet`, `pro`, `flash`,
  `flash_lite`, `argon`, `3p-5h`, `3p-weekly`) with `used_pct` and `reset_at`.
- `models`: resolved per-model quota windows and eligibility (`session_5h`, `weekly_7d`,
  `quota_group`, `eligible`, `exclusion_reason`) for every available native and CLI model.
- `rate_limited` and `cooldown_until`: active rate-limit state and expiry timestamp.

Window evaluation is time-aware: any window or cooldown with `now >= reset_at` expires
automatically at evaluation time. Unknown windows remain `null` and are never treated as `0.0`.
Post-execution token and cost accounting belongs to `howhard` (`howhard usage collect` and
`howhard usage attach`).

### Freshness before routing

`candidates`, `inventory`, and `plan` refresh missing, stale, or expired known quota
evidence for each enabled provider in scope before admitting it, including providers
currently blocked by quota. Evidence is fresh for the configured per-agent age bound.
Freshness is evaluated separately for each known window or availability scope: a report
updates only the scopes it covers, and cannot refresh an older retained scope. A first-ever
unreported window remains unknown (`null`); no required counter is invented. An expired
scope triggers a refresh, and a recognized current report may retire scopes already expired
before that report, leaving them `null`. Failed refresh cannot admit a provider through
expiry alone. Omitted, nonexpired stale scopes remain freshness obligations. A fresh
whole-provider quota block remains a quota exclusion even when another scope is stale or
expired. Existing legacy evidence without scope timestamps uses its genuine stored
evidence timestamp, including when another scope later receives a partial report.

A refresh has at most two attempts, each bounded to 15 seconds for a command or RPC. If
refresh cannot establish current evidence for known nonexpired stale scopes, the provider is excluded
as infrastructure unavailable, with an actionable reason to retry; other independently fresh
providers remain eligible. If none remain, routing fails with structured exclusions. Probe
failure metadata is persisted without advancing quota evidence time. Command success without
recognized current quota or availability evidence is not a successful probe; old cached
evidence cannot become a fresh probe result. `usage show` and
history remain inspectable without starting a probe. Expiry and supersession of individual
rejections continue to follow the rules below.

### Claude availability evidence and freshness

Claude reports may name a limit with a string scope or a structured
`scope.model.display_name` (falling back to `id` or `name`). Names are lowercased and
sanitized to letters, digits, underscores and hyphens. A meaningful `surface` is appended;
for example Fable on Claude Code becomes `fable-claude-code`. The base tier and its
`-5h` or `-weekly` variant are retained. Model matching uses the existing model/tier
tokens, so a Fable limit affects Fable cells. Each malformed or unknown limit is skipped
individually; recognized limits still apply and probe JSON includes `warnings`.

A `rejected`, `rate_limited`, `exhausted`, or numeric 429 rate-limit status is availability
evidence even without a percentage. `seven_day` names the weekly quota and `five_hour`
the session quota; model scopes affect only that model. Unscoped credit/entitlement
rejections affect the whole agent. Scoped access/credit evidence is stored separately in
`scope_availability`, so partial session or weekly counters cannot erase it. A matching
unwindowed accepted/allowed event can restore that scope. `overageStatus: rejected` supplies
no overage fallback.
Active rejections exclude routes with a visible reason and reset time. Unknown usage
remains `null`; rejection does not manufacture a 100% counter.

Availability is reconciled separately for each model/surface and window. Newer authoritative
quota reports or accepted/allowed events supersede rejection for the refreshed quota,
including access restored by a plan upgrade before the old reset. A plan change alone does
not establish remaining quota. An unrelated successful request or partial report cannot
clear another rejected model/window. Recovery discards an obsolete rejection-era counter
when no replacement is measured, while current measured usage and configured quota
thresholds still apply. Ordinary accepted events preserve genuine measured counters.
`now >= reset_at` expires the affected evidence. A successful probe command with no
recognized quota/availability data is a failed probe and cannot restore access.

Window metadata records `status`, `observed_at`, and, for measured percentages,
`measured_at`. Recent transcript events are compared with persisted evidence even when a
snapshot already exists. Explicit event timestamps determine ordering; untimestamped
transcript events use their file time and a hashed source/line identity. A current probe
supersedes previously observed identities for exactly the quotas it refreshes. Touching an
old transcript cannot replay a rejection, while a newly recorded identical rejection is
new evidence and can exclude the route again. Discovery reads the last 256 KiB of the five
most recently modified transcript files.

The snapshot's `updated_at` describes quota evidence time. `probe.attempted_at`,
`probe.succeeded_at`, `probe.failed_at`, and `probe.error` track probe operations separately;
failed probes preserve the data time. Old cached data is never counted as a fresh probe.
`usage show`, `candidates`, `inventory`, and `plan` include top-level `warnings` when Claude
evidence or the last successful probe is stale, or the latest probe failed. The configured
age bound defaults to 1,800 seconds. After a failed probe, stale percentages are displayed
as `null`; fresh measured counters and active rejection evidence remain effective.
Older persisted Claude telemetry cooldowns are migrated into recoverable availability
evidence. An unambiguous matching stored window/reset identifies the old quota; a new
report may replace both the old rejection and reset after a plan change. Ambiguous old
cooldowns remain access rejections, cleared by explicit unscoped accepted/allowed evidence
or expiry, rather than by an unrelated partial quota counter.
Unknown windows remain null; admission follows the refresh contract above for all providers.


(execution-plan-resolution)=
## Execution plan resolution and host affinity

`agroute plan --host HOST --decision FILE` validates a `howhard` decision JSON (`decision_id`,
`chosen.model`, `chosen.effort`, `cap_met`, `needs_review`) against the current host's active
eligible candidate pool and emits an `agroute-execution-plan-v1` object:

- **`execution_mode = "native"` (`target_agent == host_agent`)**:
  - `agy`: `tool_name = "invoke_subagent"` with `TypeName`, `Model`, `Role`, `Workspace`, and
    optional `Prompt`.
  - `claude`: `tool_name = "Agent"` with `subagent_type`, `description`, `model`, `effort`,
    and optional `prompt`.
  - `codex`: `tool_name = "spawn_agent"` with `model`, `reasoning_effort`, optional
    `agent_type`, and optional `prompt`.
- **`execution_mode = "cli"` (`target_agent != host_agent`)**:
  - `codex`: `codex exec --json --model <model> -c model_reasoning_effort=<effort>`,
    `output_capture_adapter = "codex-exec-v1"`.
  - `claude`: `claude -p --verbose --output-format stream-json --model <model> --effort <effort>`,
    `output_capture_adapter = "claude-sdk-v1"`.
  - `agy`: `agy --output-format stream-json --model <model> --effort <effort> -p <prompt>`,
    `output_capture_adapter = "agy-print-v1"`.

When `execution_mode = "native"`, `agroute plan` also attaches a `fallback` CLI command object
(`argv`, `output_capture_adapter`, `prompt_delivery`) for the target model and effort. If native
tool delegation fails at launch (for example, if a host client reaches its local concurrent subagent
limit), the host agent can immediately execute `fallback` via CLI and record the outcome as raw native
feedback (`howhard observe ... --native`).

When `routing.host_affinity_epsilon > 0.0` and `howhard` chose a cross-agent route, `agroute plan`
checks whether an eligible native host route in the decision's scored panel (`candidates`,
`cells`, or `ranking`) has `cap_met != false` and expected cost `J_native <= J_chosen + epsilon`.
When that condition holds, `agroute plan` selects the native host cell, sets
`host_affinity_applied = true`, and sets `feedback.mode = "raw_native"` so the caller records
`howhard observe ... --native` instead of attaching the unexecuted cross-agent receipt.

(worktree-state-contract)=
## Worktree-aware state ownership

`agroute paths --json` reports `capture_root`, `state_root`, `identity_path`, `project_identity`,
and `config_path`. Fresh zero-config Git worktrees share `<git-common-dir>/agroute` while
capturing the current checkout root. Plain folders use local `.agroute`. `--state-dir PATH`
(or `AGROUTE_STATE_DIR`) selects an intentional store and enforces `agroute-project-v1` and
`agroute-store-v1` identity checks so another project's store is refused.

(agent-adapters-and-dynamic-dispatch)=
## Agent adapters and dynamic dispatch

Agent-specific behaviors are decoupled from routing and usage logic through the `AgentAdapter`
interface and dynamic dispatch registry (`agroute.adapters`). Each supported agent (`agy`, `claude`,
`codex`) implements an `AgentAdapter` that encapsulates:

- `name` & `display_name`: canonical identifier and human-readable label.
- `capture_adapter`: CLI stream output capture protocol name (`agy-print-v1`, `claude-sdk-v1`, `codex-exec-v1`).
- `validate_native_candidate`: agent-specific constraints on native subagent model parameters (such as the `invoke_subagent` enum in Antigravity).
- `discover_candidates`: surface-aware local model discovery (`native` vs `cli`).
- `build_native_invocation`: formats the native tool call directive (`invoke_subagent`, `Agent`, `spawn_agent`).
- `build_cli_invocation`: constructs the non-interactive CLI argv and prompt delivery mechanism.
- `discover_live_usage_patch`: discovers rate-limit telemetry from local session files and probe caches.
- `probe_usage`: performs zero-token or RPC rate-limit probing.
- `resolve_quota_group`: resolves model-specific or multi-provider quota buckets (such as `3p` and `gemini` pools).
- `rate_card_key`: resolves a candidate cell to its exact built-in {ref}`rate-card <built-in-rate-cards>` catalog key, or `None`.

New agent integrations or mock adapters can be registered dynamically via `register_adapter(adapter)`
and queried with `get_adapter(name)`.

(realtime-route-monitoring)=
## Realtime route monitoring (`tui`, `list`, `show`)

Every `agroute plan` resolution appends a record to `<state_root>/routes.jsonl` and registers
the repository in `${AGROUTE_SPOOL_DIR:-~/.local/share/agroute}/repositories.json`. `agroute`
correlates `<state_root>/routes.jsonl` with `.git/howhard/` (`decision-events.jsonl`,
`decisions/*.json`, and `pending.jsonl`, including `amends` chains) so both `agroute plan`
resolutions and direct `howhard` routing decisions appear in reverse-chronological order with
their live outcome state (`pending`, `clean`, `loud`, `silent`, `review`, `planned`) and
task summary.

- `agroute tui [--all] [--repo NAME] [--agent AGENT] [--model MODEL] [--state STATE] [--refresh-seconds SEC]`:
  interactive Textual terminal dashboard showing live agent quota/eligibility badges, active and
  historical model routes, `Enter` route detail modal, `u` live usage & eligibility modal,
  `h` history toggle, and filter pickers for repository (`p`), target agent (`a`), model (`m`),
  and state (`s`).
- `agroute list [--all] [--repo NAME] [--agent AGENT] [--model MODEL] [--state STATE] [--limit N] [--json]`:
  scriptable tabular or JSON view of the same route records.
- `agroute show <id> [--json]`: full route record, invocation details, and `howhard` decision
  for a route ID, decision ID, or execution ID.
