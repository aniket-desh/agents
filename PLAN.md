# Research workflow: implementation and boundaries

Implemented in this checkout after the approved refactor plan, September 2026.
Local fake-provider and mocked-agent validation is separate from a live host,
native TUI, and paid RunPod acceptance run, which have not been performed here.

## Human interface

The user opens ordinary `codex`, `claude`, or `pi` conversations in tmux/Ghostty.
They describe questions, hardware, budgets, deadlines, and autonomy naturally.
Installed global guidance and skills translate those requests into tools; the
user doesn't operate a new research CLI or register projects every morning.
Any number of interactive and autonomous investigations can coexist.

Shared research guidance favors direct, minimal mathematical code and the
cheapest informative correctness oracle. Negative results, inconclusive results,
and budget/deadline outcomes are valid. Routine edits don't require a planning
document, test scaffold, fixed verifier pipeline, or production architecture.
Project-specific scientific conventions retain precedence.

## Implemented structure

```text
Laptop terminal → SSH → persistent Linux CPU host
                         ├─ isolated persistent workspace container
                         │    ├─ tmux + native agent conversations
                         │    ├─ guidance + continuation adapter
                         │    └─ experiment collectors → SSH workers
                         ├─ protected controller → official RunPod MCP
                         │    └─ SQLite policies, leases, jobs, journal, costs
                         └─ independent systemd watchdog → stop/reconcile

RunPod persistent volume → compact periodic/final results → CPU host
```

The runtime uses Python's standard library, SSH, SQLite, and a pinned official
RunPod MCP subprocess. It doesn't depend on `gpuc` or invoke a separate billed
model API. The controller owns provider credentials but never executes research
commands; unprivileged collectors launch those commands on workers.

| Area | Implemented behavior |
|---|---|
| User installation | Managed instruction blocks, two skills, native adapters, preserved settings, backups, owned-file conflict checks |
| Agent workspace | Nonroot Docker image, private home/code volumes, read-only root, no personal-home/SSH-agent/Docker-socket mounts; permission-free Codex/Claude defaults inside the image only |
| Protected deployment | Dedicated non-login service identity, private key/ledger, group-controlled socket, root-owned runtime, two systemd services |
| Admission | Transactional reservations across investigations, GPU/pod/rate/image/storage limits, finite leases, healthy-watchdog requirement |
| Jobs | Immutable dirty-worktree snapshot, detached supervisor, bounded runtime, process identity, per-pod experiment bundle |
| Capacity contention | Detached collectors wait without model polling; finite queue timeout, no fair-share/preemptive scheduler |
| Recovery | Durable pre-create intent, no blind create retry, structured rejection/not-found handling, repeated stop confirmation |
| Collection | Bounded numerical artifacts and log tails, hashes and provisional status, explicit incomplete-collection reporting |
| Pod lifecycle | Reuse compatible retained pod, start, stop, confirm; no terminate operation |
| Research continuity | Shared journal/artifacts/spend; one bound native driver per investigation, explicit paused handoff |

## Budget and retention semantics

The global owner allowance is cumulative across the ledger. Agent-created
session policies can subdivide it but cannot reset it. Every allocation reserves
its whole lease plus shutdown allowance; the estimate includes provisioning,
downloads, runtime, collection, and conservative active storage. Confirmed stops
settle the lifetime estimate. Unknown create/stop outcomes remain liabilities
and block new spending while the watchdog reconciles them.

An optional review threshold stops/retains allocations and requires an actual
user continuation instruction. Resuming preserves cumulative spend. A reviewer
can stop slightly early when the next useful experiment doesn't fit. All-day
autonomy has no mandatory financial review gate unless requested.

Retained storage has its own standing GB ceiling and monthly estimate. Stopped
pods continue to incur storage charges and can't accept SSH until restarted.
Confirmed human deletion releases storage capacity; the controller never deletes
experiments to fit a cap. Owner-only local operations amend exhausted policies
or resolve a verified no-pod creation without erasing the financial history.

Dollar values remain **estimates**, not invoice reconciliation. The provider
adapter conservatively adds a storage allowance even when the returned price
may already include some storage. Live price semantics need verification.

## Native autonomy

Both modes use the visible conversation as the only code-writing driver.
`continuation_driver=native` reserves future turns for a supported vendor goal
or schedule; the Stop hook then does nothing. `hook` enables this repo's bounded
same-conversation continuation. Don't enable both drivers for one investigation.

Codex can use native Goals where supported or trusted Stop hooks. Claude's Stop
fallback has a native consecutive-turn limit, so long sessions need a supported
in-session schedule/task mechanism or ongoing bounded waits. Pi uses the current
`agent_before_settle` extension API and idle wakeups without starting another
model process. The adapters stop scheduling on the states/interruption signals
their native interfaces expose. See `integrations/README.md` for exact boundaries.

The hook allowance and failure/no-progress guards bound fallback continuations;
they are not a model-token cap or enforcement over native Goals. Every native
research iteration must read the controller state. Pausing must stop both the
native continuation mechanism and the controller investigation. An exhausted
subscription, agent crash, or machine reboot is not automatically recovered as
a model conversation; retained journals and outputs support deliberate resume.

## Validation and remaining acceptance work

Offline checks exercise shared reservations, review resumption, controller
restart, agent submission exit, timed-out creation, failed/lost stop responses,
source secrecy, archive boundaries, worker cancellation, installation ownership,
and same-conversation continuation decisions. The end-to-end test runs a small
CPU numerical experiment through real local services and detached processes,
collects its numbers, and verifies a retained stopped fake pod.

Before relying on a workday deployment, complete one explicitly funded live
pilot: authorize a small allowance, deploy on the chosen CPU host, start the
chosen native agent, run a tiny GPU experiment, disconnect the laptop, and verify
numerical outputs plus a confirmed stopped pod. Restart it and verify persistent
files, then check the account charges. Also exercise that agent's real trust,
continuation, progress-query, and pause behavior; mocks don't establish those.

The watchdog survives an agent or controller-process crash while the host lives.
It doesn't survive loss of the entire CPU host, nor can it guarantee immediate
shutdown during a provider outage. Network calls are bounded but serialize under
the controller's admission lock; slow provider requests can delay shutdown.
An off-host reaper would be a separate operational addition.

Direct provider tools/keys, unmanaged pods, privileged agents, and malicious
worker code are outside the controller's enforcement boundary. The shared
instructions route normal research through the controller; they do not revoke
previously granted provider access. Native hook trust is preserved.
The managed image replaces the agents' execution sandbox with its outer Docker
boundary; native onboarding and hook trust remain intact. Explicit permission
options suppress the launcher's bypass defaults. Remote Codex app-server
connections don't receive automatic bypass settings. The workspace can still
modify its own files and use credentials installed there. Containers sharing
the same mapped UID have the same controller owner identity.

Workspace tests inspect Docker plans and reuse validation with a fake daemon,
check agent argument defaults, and verify host settings survive installation.
The image has not been built or exercised on a real Docker host here; its shell,
native agent permissions, persistence, and socket access need that integration
check before deployment. No paid resources or real-home settings were changed.

## Legacy paths

`setup.sh` and `provision/` remain available for existing pod-local installations,
including nested projects and reproducible environment setup. `team/` remains an
optional legacy subsystem and is excluded from the new installation. Its example
SPEC now admits negative/inconclusive outcomes, but its old hooks are not part of
the supported protected workflow. The prior README is in `docs/legacy.md`.
