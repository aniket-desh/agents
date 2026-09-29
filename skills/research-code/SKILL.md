---
name: research-code
description: Implement, debug, or simplify scientific and machine-learning research code with direct mathematical structure and focused validation. Use for numerical algorithms, simulations, tensor operations, and research experiments.
---

# Research code

Identify the observable that answers the question and the cheapest independent
correctness check. Fit the implementation to that experiment, preserving the
project's conventions and existing interfaces when they remain useful.

Choose checks by the likely scientific error. A tiny dense reference can expose
a tensor contraction error; a known spectrum or invariant can expose a quantum
simulation error; a finite difference can expose a gradient error. For SAE L0,
a hand-checkable activation matrix exposes a wrong axis or denominator. Import
success alone says little about those claims.

Distinguish a calculation bug from an empirical question. Whether sparsity is
useful requires measured reconstruction and activation behavior; additional unit
tests do not establish that. Likewise, one seed or one tiny exact comparison
does not establish general performance.

Keep one-off validation inline when appropriate. Add a lasting regression check
when it protects a meaningful invariant or observed failure. After relevant
checks pass, inspect the diff for wrappers, configuration, dependencies, or files
that do not help the current experiment, and remove that unnecessary machinery.

Report the change, the evidence checked, and any assumption still unverified.
Keep the response proportional to the task; do not manufacture a formal research
report for a local metric edit.
