---
name: gpu-research
description: Run budgeted research experiments on retained RunPod GPU workers through the local research-compute controller. Use for GPU allocation, experiment submission, results, spend, pause or continuation, and autonomous research requests.
---

# Budgeted GPU research

Keep the user in the normal conversation. Translate their question, hardware,
budget, review point, deadline, and autonomy into a session policy; reuse the
existing session for continuation. State the interpreted limits briefly. Existing
authorization covers ordinary allocations and retries within those limits.
Ask only for a material missing limit that standing policy cannot resolve.

Use `research-compute --help` and operation help for the installed JSON tool
contract; the installed instructions link to `docs/compute-tools.md` with concrete
policy and run-spec examples. Start with `research-compute doctor`. The client
connects to the local controller; `RESEARCH_COMPUTE_SOCKET` overrides its default
socket. Do not start
your own controller, change owner configuration, use direct RunPod commands, or
put provider credentials in research code.
If doctor reports the fake provider, treat runs as local validation and do not
report a simulated allocation as a real rented GPU.

Use `session create --policy FILE` for a new investigation; use session status,
list, journal, pause, resume, and finish for its existing record. Policies include
objective, budget_usd, max_gpus, max_pods, allowed_gpu_types, max_hourly_usd, and
max_volume_gb; review_at_usd and a timezone-qualified deadline are optional.
Use `quote --session ID --request FILE`, then `run --session ID --spec FILE`;
run allocates its own worker, so do not allocate a second one first. A run spec
contains repo, command as an argv array, request, outputs, and timeout_seconds.
The request specifies gpu_type, gpu_count, volume_gb, container_disk_gb, image,
and duration_seconds. Use jobs, wait, and collect to observe and retrieve results.
Read operation help for exact fields. Direct allocate is for explicit lifecycle
work, not a prerequisite to run.
Do not reset an allowance by creating another session for the same investigation.
Use the exact agent, conversation_id, and workspace supplied by the native
SessionStart hook or Pi context to bind the investigation with `session bind`.
Never guess an ID or borrow another conversation's binding. Set session mode
auto only for user-requested autonomous research; interactive work remains
interactive until the user asks for continuation.

Prepare code and an informative cheap check before renting. Submit an immutable
source snapshot and save small numerical outputs, plot inputs, plotting code,
logs, and enough metadata to reproduce the run. Large datasets stay remote.
Preserve checkpoints needed for interrupted work on the verified persistent
volume. Separate independent conversations' mutable worktrees and environments.

Pause or finish through the controller so all of the session's jobs and pods
are covered. Verify status: a stop request is not a confirmed stopped pod, and
an interrupted upload is not a collected result. Never terminate a pod. Stopped
storage continues to cost money and cannot accept SSH until restarted; inspect
collected outputs locally when possible.

For unattended research, use the installed vendor continuation integration only
if it is supported and available. tmux preserves a process, not research intent
or a spending guarantee. Keep one driver per investigation, persist findings and
next actions in the journal, and wait on job state without busy model polling.
Pause when blocked, at a review gate, or when further experiments are not useful.
Never fall back to separately billed model APIs without authorization.

Choose one continuation driver with `session driver ID native|hook`:

- Codex: prefer its supported native Goal capability for an explicitly requested
  persistent objective; choose `native` before activating the Goal. The Goal
  should answer the question with defensible evidence within the session limits,
  allowing negative or inconclusive results. If that capability is unavailable,
  use the installed Stop hook with driver `hook`; do not run both mechanisms.
- Claude Code: use `native` for an available in-session scheduled task that
  checks the investigation and continues useful work. Its prompt must first read
  session status and respect pauses, review gates, budget, and deadline; cancel
  the task when work ends. Otherwise use `hook`, explaining that Claude currently
  permits only eight consecutive Stop-hook continuations. Do not promise all-day
  continuation from that fallback alone.
- Pi: use `hook`. The installed extension requires Pi's current
  `agent_before_settle` API and continues the same conversation. It polls the
  controller while admitted jobs run, without model turns.

For Codex/Claude hooks, a running GPU job is not a reason to keep generating
turns. Wait using the available tool/task mechanism before yielding, or arrange a
supported native wakeup that will collect its result. A Stop hook alone cannot
wake an already idle conversation. If neither path is available, state the
limitation; the bounded job and watchdog still finish or stop safely.

On a request to pause, stop the controller session and its native Goal/task.
On resumption, reuse the existing budget record and resume only the selected
driver. Never switch to a hook to defeat a paused native Goal. Hook iteration and
no-progress counters apply to hook-driven work, not native model usage; compute
limits remain enforced in either mode. A native conversation crash or exhausted
subscription requires user recovery, not another hidden agent.
