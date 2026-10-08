## Research work

For research code, make the smallest transparent change that answers the current
question. Keep equations and algorithmic structure visible, and explain
non-obvious axes, normalizations, approximations, and numerical assumptions.
Reuse existing code and dependencies; add structure for a present need.

When repetitive work or awkward data inspection obstructs research, build the
smallest useful helper. Keep experiment-specific viewers and plotting scripts
beside the experiment; share them across projects only after demonstrated reuse.
Helpers should work as ordinary scripts regardless of the agent calling them.

Validate the failure modes introduced by the change using the cheapest
informative check: a tiny exact reference, analytical limit, invariant, or
targeted reproduction. Reuse relevant existing tests. One-off checks can remain
inline; permanent tests should protect meaningful behavior. Broaden or repeat
checks only after a new change, failure, or unresolved concern. Infrastructure
that controls spending or preserves data needs appropriate recovery tests.

Distinguish implementation correctness from evidence for a hypothesis. Negative
and inconclusive results are valid. For measured results, retain the source
snapshot, parameters, relevant seeds, numerical outputs, and plotting code.

Routine edits do not require a plan document, a new test module, delegation, or
a review pipeline. Preserve project-specific scientific conventions. Do not
automatically pull, commit, or push merely because a research session started.

The human interface is the ordinary agent conversation. For paid GPU work, use
the installed gpu-research skill and research-compute tools under the existing
authorization and limits. Never bypass the controller using provider credentials
or direct rental commands. Stop and retain pods; termination is a human action.
An instruction to keep researching does not itself provide unattended execution:
use only a verified continuation integration, and report when it is unavailable.

## Writing

Lead with the answer and any qualification that changes its meaning. Write
direct, connected prose that links claims to evidence or mechanisms and explains
why they matter. Keep the length proportional to the question; cut repetition
and generic advice rather than compressing sentences. Use lists or tables when
the content benefits from them. Preserve requested wording, notation, scope,
and document voice; distinguish findings from hypotheses and planned work.
Use the technical-writing skill for substantive drafting or revision.
