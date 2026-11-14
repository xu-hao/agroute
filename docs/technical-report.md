# Technical report

## Abstract

agroute connects coding-agent availability to task-specific model selection. It
discovers or accepts configured model/effort candidates, filters them using native
usage evidence and policy, attaches price and credit metadata, and passes the
eligible inventory to howhard. It then resolves howhard's selection into a native
delegation directive or non-interactive CLI plan for the current host.

The design separates availability, task suitability and execution. agroute owns
quota filtering and plan resolution; howhard owns task forecasts, model/effort
selection, execution accounting and semantic learning; the host performs the work.
agroute's adaptive credit component estimates dollar-equivalent quota capacity
from observed consumption using prior shrinkage. Those estimates support
scheduling decisions and require evaluation against later observations.

## 1 Two different routing questions

A model can be a good fit for a task while its subscription pool is exhausted.
Another model can be available through a CLI but lack the same controls in the
current host's native delegation tool. agroute represents these distinctions
before asking howhard to choose a model and reasoning effort.

Let a route be {math}`r=(a,m,e,s)`: agent, model, effort and execution surface.
For the current host {math}`h`, the surface is native when {math}`a=h` and CLI
when {math}`a\ne h`. The task-specific choice is made over the eligible routes,
using model identities qualified with the agent namespace.

| Question | Owner | Evidence or output |
| --- | --- | --- |
| Which controls are represented as available? | agroute discovery/configuration | Native and CLI model/effort inventories |
| Which routes pass current usage policy? | agroute | Quota evidence, exclusions and optional credit budgets |
| Which route suits this task? | howhard | Task/project forecasts, cost/risk comparison and learned experience |
| How can this host invoke the route? | agroute | Native tool parameters or CLI arguments |
| What actually ran and happened? | Host and howhard | Execution identity, outcome and measured usage |

Discovery and usage evidence describe available routes; they do not establish
that a candidate will complete the task correctly. Conversely, a model's forecast
does not reserve subscription capacity.

## 2 Routing pipeline

```text
Current host + configured/discovered model surfaces
                        |
Refresh scoped usage evidence and check consumed quota pools
                        |
Attach price provenance and estimated remaining credit
                        |
Emit full eligible provider-qualified inventory
                        |
howhard reads task/project, forecasts and selects a model/effort
                        |
agroute rechecks live eligibility and resolves an execution plan
                        |
Host launches native delegation or the returned CLI command
                        |
howhard records actual outcome and measured execution usage
                        |
Later quota snapshots can update agroute's credit calibration
```

`agroute candidates --json` returns both the eligible inventory and the reasons
other routes were excluded. It also carries price-resolution requests and
calibration metadata. Without `--json`, the command emits the plain candidate
array accepted by howhard. The inventory is not truncated to an arbitrary count.

The package does not launch workers. It can run bounded usage probes while
refreshing availability, but a returned execution plan still requires the host
to invoke the worker. The [quickstart](quickstart.md) covers the calling workflow;
the {ref}`candidate contract <candidate-qualification>` specifies the envelope.

## 3 Host surfaces and model identity

Each agent has separate `native_candidates` and `cli_candidates` surfaces.
Explicit configuration can pin either surface; otherwise discovery uses the
locally installed agent's catalog or model command. This keeps the inventory
connected to the controls available through the selected execution surface.

Provider qualification preserves identity across agents. For example, a model
available through Antigravity is represented as `agy/<model>`, while a model
through Claude Code is `claude/<model>`. These routes can have different quota
pools and invocation controls even when they refer to a related underlying model.
agroute does not merge them into one interchangeable execution identity.

The adapter layer maps candidates to quota pools and invocation controls. Codex
native plans name `spawn_agent`, Claude Code plans name `Agent`, and Antigravity
plans use `swarm` for concrete model slugs or `invoke_subagent` for explicitly
configured abstract tiers. Cross-agent routes use the target's CLI.

The {ref}`surface contract <candidate-qualification>` and
{ref}`adapter interface <agent-adapters-and-dynamic-dispatch>` define discovery
and dispatch. The calling host remains responsible for honoring the returned
controls and reporting launch failures or substitutions.

## 4 Quota evidence and overlapping pools

### Freshness belongs to each scope

Usage evidence can describe session, weekly, model/tier and availability scopes.
A newer partial report refreshes only the scopes it actually covers. It cannot
make an older retained scope fresh merely by advancing a provider-level timestamp.
Unknown percentages remain unknown, rather than becoming zero consumption.

Routing refreshes missing evidence and known stale or expired scopes with bounded
attempts. A command exiting successfully is insufficient if it yields no recognized
current quota or availability evidence. Failed refresh excludes the affected
provider as unavailable; independently fresh providers can remain eligible.
Quota expiry alone cannot establish a successful refresh.

This makes quota time part of the routing input. The same candidate inventory
can produce a different eligible set after a new report, rejection, plan change
or reset. The {ref}`quota contract <native-quota-tracking>` defines freshness,
partial reports, expiry and rejection supersession.

### Every consumed pool matters

An adapter can identify multiple pools consumed by one model. A route may need
both a model-specific allowance and a shared allowance. Eligibility applies
configured exclusions, scoped rejections and session/weekly percentage limits
across every consumed pool. An exhausted shared pool still blocks a route whose
model-specific counter looks healthy.

If {math}`Q(r)` is the set of consumed pools, the pool checks have the form:

```{math}
\operatorname{passes\_pools}(r)
=\bigwedge_{q\in Q(r)}\operatorname{passes}(q).
```

Provider availability and candidate controls are additional requirements. Known
counters are evaluated against the configured limits; missing counters are
retained as unknown. Policy can also reserve a percentage of the current host's
capacity for its coordinating session.

## 5 Prices and remaining credit

### Price provenance

agroute attaches a price to every candidate it can price, retaining its source and
basis. Explicit candidate prices take precedence over per-model user prices.
Then the active resolved price store and dated built-in catalog apply according
to their precedence rules. Alias estimates and unlisted models remain identifiable.

The runtime does not fetch price lists from the network. An unresolved discovered
model creates a `price_request` for the calling agent, which can supply official
rates, identify an alias, or record that a model is unlisted. The response is
validated and stored locally with expiry before candidates are emitted again.

A rate card describes token-price estimates. It is not evidence of the user's
subscription invoice or a native provider's exact credit-consumption formula.
Unknown prices remain unknown. howhard can exclude worker money from its
comparison when candidate prices are missing or their bases are incompatible.
When credit-budget evaluation is active, agroute excludes unpriced candidates
that cannot be evaluated against that budget.

See {ref}`rate cards <built-in-rate-cards>`,
{ref}`price precedence <price-precedence>` and
{ref}`calling-agent price resolution <calling-agent-price-resolution>` for the
current contract and catalog provenance.

### Dollar-equivalent capacity

Plan presets supply initial dollar-equivalent pool ceilings. These are scheduling
model inputs, not cash balances. Automatic plan detection or explicit configuration
selects a preset; manual credit overrides take precedence over learned estimates.

For a consumed pool {math}`q` and window {math}`w`, let {math}`C_{q,w}` be its
effective ceiling, {math}`u_{q,w}` its known used percentage and {math}`\rho`
the host reserve percentage, applied only to the current host. Remaining credit is:

```{math}
B_{q,w}=C_{q,w}\max\left(0,\frac{100-u_{q,w}-\rho}{100}\right).
```

The candidate's `available_usd` is the minimum of the calculated balances across
consumed pools and session/weekly windows. Terms with unknown inputs cannot
supply a calculated balance. Percentage eligibility caps are checked separately;
the budget equation is not a replacement for those caps.

For illustration, if one consumed pool has 10 estimated dollars left and another
has 4, the route's budget is 4. A forecast requiring 5 does not fit. Adding the
balances would incorrectly treat overlapping constraints as separate capacity.

These are forecast-based scheduling checks. They do not reserve capacity against
other sessions, guarantee that a task finishes before a provider limit, or cover
every possible retry. The {ref}`credit contract <remaining-credit-budgets>` defines
the emitted fields and policy behavior.

## 6 Empirical credit calibration

### Matching quota consumption to executions

Credit calibration is separate from howhard's learning about task suitability.
It estimates how much recorded execution cost corresponds to a full native quota
window. Its state is keyed by agent, effective plan, pool group and window.

A qualifying interval joins two non-resetting quota snapshots and completed
howhard observations for the same agent and consumed pool. It must have a quota
increase of at least 0.25 percentage points, positive recorded costs for all matched
observations, and no relevant unobserved or in-flight route straddling the boundary.
Reset changes, declining usage and missing measurements prevent an inappropriate
interval update. Each interval boundary is advanced so the interval is not reused.

This matching depends on observation coverage. Consumption from other activity,
delayed reports, coarse counters or changing price bases can affect the inferred
relationship. Retaining interval and support metadata makes those assumptions
available for evaluation.

### Prior shrinkage estimator

Let {math}`C_0` be a preset ceiling, {math}`S_c` accumulated execution cost from
accepted calibration intervals and {math}`S_f` their consumed quota fraction. The implementation's
empirical Bayes calibration uses fixed prior strength {math}`F_0=0.20`:

```{math}
\widehat C=\frac{F_0 C_0+S_c}{F_0+S_f}.
```

With no accepted evidence it retains the preset. For {math}`S_f>0`, the same
expression is a weighted average of the preset and the observed cost-per-window:

```{math}
\widehat C=
\frac{F_0}{F_0+S_f}C_0+
\frac{S_f}{F_0+S_f}\frac{S_c}{S_f}.
```

Intervals whose implied ceiling falls outside 0.1 to 10 times the prior are
rejected as outliers. When accumulated fraction exceeds two windows, both cost
and fraction statistics are rescaled together to cap the fraction at two.
Older evidence consequently receives less influence as new intervals arrive.
Explicit manual credit overrides remain exact and are not calibrated.

The result is a shrinkage point estimate with reported support statistics. The
implementation does not compute posterior intervals for pool ceilings. A large
support count does not establish that quota consumption is linear or that all
background use was captured. The {ref}`credit contract <remaining-credit-budgets>`
defines the parameters and stored metadata; the [evaluation guide](evaluation.md)
describes how to assess estimates prospectively.

## 7 Planning and execution feedback

### Recheck before launch

Selection and execution can be separated in time. `agroute plan` reconstructs
the current eligible inventory and validates the selected qualified model and
effort against it. When live credit and the decision's required invocation credit
are both available, it rejects a requirement exceeding that live balance. It does
not manufacture a missing requirement estimate.

An optional host-affinity policy can prefer an eligible native host route from
the decision's scored panel when its score is within an absolute tolerance:

```{math}
J_{\mathrm{native}}\leq J_{\mathrm{chosen}}+\epsilon_{\mathrm{host}}.
```

The tolerance is in score units, not a percentage. This preference does not create
a new howhard receipt for the substituted invocation. `needs_review` remains an
instruction for the caller to honor. The
{ref}`execution-plan contract <execution-plan-resolution>` specifies candidate
checks, available score fields and the emitted invocation parameters.

### Feedback follows the actual invocation

| Execution | Feedback path |
| --- | --- |
| The selected route executes as matched to its receipt | Follow `matched_receipt` and record the observed result against that receipt |
| Host affinity chooses a different native route | Follow `raw_native`; retain the actual model and effort |
| Native launch fails and the caller uses the supplied CLI fallback | Record raw native feedback for the execution that actually ran |
| A plan is created but no worker runs | Retain the plan; do not invent an outcome or usage measurement |

The host executes the invocation and captures only artifacts belonging to that
execution. howhard records the outcome, measured usage and memory updates.
Unknown measurements stay unknown. agroute's route history provides planning
provenance and does not replace howhard's execution evidence.

## 8 State and observability

Git worktrees share repository state under the common Git directory while each
operation captures its current checkout. Plain folders use local `.agroute`.
Explicit state selection enforces project/store identity checks. This lets parallel
worktrees share availability and history without making a sibling checkout a
required application dependency.

| State | Purpose |
| --- | --- |
| `usage.json` | Native usage, reset and availability evidence |
| `prices.json` | Validated local price resolutions and expiry |
| `credit_calibration.json` | Pool snapshots and calibration statistics |
| `routes.jsonl` | Resolved plans and execution/decision links |
| howhard's own store | Decisions, execution outcomes, accounting and semantic memory |

`agroute list`, `agroute show` and `agroute tui` combine route and howhard evidence
for inspection. A planned or pending record is not proof of completed execution.
The {ref}`state contract <worktree-state-contract>` and
{ref}`monitoring contract <realtime-route-monitoring>` define ownership and display.

## 9 Evidence needed for the user benefit

The intended benefit is making useful task progress across available coding-agent
capacity. Demonstrating that benefit requires more than counting eligible routes
or inspecting plausible plans. Evaluate whether plans execute with the requested
controls, whether live quota changes are handled correctly, whether budget estimates
predict subsequent consumption, and whether total cost and completion time improve.

Include usage refresh, routing, howhard inference, learning, execution and rework
in the relevant cost and latency measures. Alternative-route outcomes require actual
executions or a prospective comparison; they cannot be inferred from unrun plans.
The [evaluation guide](evaluation.md) separates implementation checks, credit
estimation and policy-value evidence.
