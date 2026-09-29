# SPEC — <experiment name>

Optional template for the legacy team workflow. The new installer does not
activate its goal/judge hooks. A research question can end with a negative or
inconclusive result, an exhausted budget, or a need for human input; record that
outcome and stop. Completion must never require obtaining a positive result.

## Goal

<One or two sentences: what question are you testing, and what evidence would
distinguish the hypotheses?>

## Approach / scope

<The rough plan. What's in scope, what's explicitly NOT. Seed ideas the agent
should explore. Keep it lean — give direction and a way to fetch detail, not a
wall of context.>

## Evidence and completion criteria

- [ ] <e.g. the tiny exact reference agrees with the implementation>
- [ ] <e.g. steering metric on layers 0–31 logged to reports/<name>.md>
- [ ] <e.g. report mean Δ-metric versus the pinned baseline on the held-out split,
  including negative results and uncertainty>
- [ ] <e.g. a plot saved to logs/ showing X>
- [ ] <record answered, negative, inconclusive, budget/deadline reached, or blocked;
  distinguish completed checks from checks that were not run>

## Spending and stopping

<Approved cumulative budget, optional review point, deadline, hardware limits,
and useful early-stopping conditions. Stop and retain GPU pods when finished or
blocked. The legacy hooks alone do not mechanically enforce these limits.>

## How to verify

<Exact commands the agent (and the judge) should run to check the criteria.
The judge inspects the repo and runs these — make them concrete.>

```bash
# example
python eval.py --sweep layers --baseline main
pytest tests/fra/ -q
```

## Out of scope / do not

- <e.g. don't touch the data pipeline; another peer owns it>
- <e.g. don't force-push; don't delete /workspace state>
