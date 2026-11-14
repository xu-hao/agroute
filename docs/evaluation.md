# Evaluating routing and credit estimates

Evaluate agroute on authorized work from real maintained projects. Separate three
questions: whether the routing mechanics work, whether credit estimates predict
later consumption, and whether the combined routing policy improves task outcomes
or cost. A passing implementation check does not answer the other two.

## Record the decision context

Before execution, retain the task, project/checkout identity, current host, effective
configuration, discovered candidate surfaces, quota scopes and observation times.
Save the candidates envelope, price-resolution status and price basis, calibration
metadata, howhard decision and resolved execution plan.

After execution, retain the actual agent/model/effort, launch result, stable execution
ID, outcome, measured usage and subsequent relevant quota snapshots. Preserve the
time at which measurements or corrections became available. Keep unknown counters
and charges unknown.

Use explicitly owned execution artifacts and project stores. Do not scan unrelated
sessions or another agent's work to fill gaps in the evaluation record.

## Check availability and execution fidelity

On observed routing attempts, report:

- Eligible and excluded routes, grouped by the recorded reason: policy, quota,
  freshness/probe failure, missing price or insufficient credit.
- Refresh attempts, failures, latency and changes in the relevant evidence scopes.
- Decisions where an overlapping shared pool blocks an otherwise available model.
- Differences between the inventory used for selection and live eligibility at planning.
- Native versus CLI plans, host-affinity changes, launch failures and fallback executions.
- Agreement between the returned invocation and the controls actually accepted by the host.

A route that was never executed supplies no task outcome. A returned plan alone
cannot establish that the provider accepted the requested model or effort. Keep
planning failures, deliberate review and actual worker failures distinct.

Deterministic fixtures can verify pool handling, expiry, precedence and plan schemas
without provider calls. Real observations are needed to establish how those mechanisms
behave under an actual account's quota reports and host controls.

## Evaluate credit estimates prospectively

For each agent, effective plan, pool group and window, record the preset ceiling,
current calibrated ceiling, effective sample fraction, matched intervals, rejected
outliers and any manual override. Assess a frozen estimate against later eligible
intervals; do not use an interval to both fit and independently validate that estimate.

For a later matched interval with measured quota credit {math}`c` and quota-fraction increase
{math}`f`, the estimate predicts:

```{math}
\widehat f=\frac{c}{\widehat C}.
```

Compare predicted and observed fraction, and compare the calibrated estimate with
the unchanged plan-preset baseline on the same held-out intervals. Report absolute
and signed errors, interval counts, sample coverage and results by pool/window.
Stratify or annotate concurrent external activity, coarse reports, plan changes,
missing credit measurements and delayed feedback. These factors can change the relation
between measured quota credit and native quota consumption.

The calibration interval filters intentionally exclude some observations. Report
how many intervals were usable and why others were skipped; apparent accuracy on a
small accepted subset cannot establish accuracy over all usage. Manual overrides
are configured values rather than estimates learned by this procedure.

The {ref}`credit contract <remaining-credit-budgets>` defines matching, fixed prior
strength, outlier rejection and bounded statistics. It returns a point estimate and
support metadata, not a posterior coverage guarantee.

## Assess useful progress and total cost

Compare against a defined simpler policy, such as using one available agent with
fixed model/effort controls, on comparable real tasks. Freeze candidate policy,
price basis, acceptance criteria and review budget before observing results.
Keep related tasks and retries grouped and evaluate chronologically.

Measure accepted task results, caught failures, defects discovered later, completion
time, review/rework and interruptions attributable to usage limits. Include worker
cost, quota-refresh overhead, routing, howhard forecasts and amortized memory-writing
cost. Where only token-price estimates are available, label that basis explicitly;
estimated dollars do not establish actual subscription charges or cash savings.

Do not count measured rework twice through both actual costs and configured failure
penalties. Report those quantities separately. A chosen-action log cannot establish
what an unrun route would have done; comparative policy claims require actual
alternative executions or a prospective comparison with recorded assignment.

For howhard's forecast and delayed-outcome evaluation, follow its
[real-project evaluation protocol](https://howhard.readthedocs.io/en/latest/evaluation.html).

## Reporting limits

Use `agroute list`, `agroute show` and the TUI to inspect the recorded route history,
and retain the underlying evidence for analysis. Report the number of tasks and
intervals, missing measurements, follow-up coverage, price basis and changes in
account or provider conditions.

An initial feasibility exercise can show that collection, execution matching and
replayable records work. Claims about better routing, lower total cost or reliable
credit estimation need enough subsequent work to assess variability and rare
failures. Synthetic tasks, successful model discovery and a visually plausible
dashboard are not substitutes for that evidence.
