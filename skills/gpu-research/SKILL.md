---
name: gpu-research
description: Run budgeted research experiments on retained RunPod GPU workers through the local research-compute controller, or over SSH on a pod the user provisioned when no controller is reachable. Use for GPU allocation, experiment submission, results, spend, pause or continuation, autonomous research requests, and any experiment on a pod whose SSH command the user hands over.
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
report a simulated allocation as a real rented GPU. If doctor cannot reach a
controller, follow [User-provisioned pods](#user-provisioned-pods) instead.

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

## User-provisioned pods

Without a reachable controller, never create, rent, start, resize, or stop pods
yourself. The user rents a pod in the Runpod console and hands over its SSH
command. That handoff authorizes experiment work on that one pod; its lifecycle
stays with the user.

Identify the pod before the first remote command. A proxy login
`<pod_id>-<suffix>@ssh.runpod.io` names the pod ID; a direct `root@IP -p PORT`
must match exactly one pod's public TCP mapping. Confirm with a read-only Runpod
pod lookup (list or get pod; no mutations) that its current name starts with
`aniket-`. A missing name, failed lookup, ambiguous match, or other prefix means
refuse; the user can rename their own pod in the console. Record the pod ID,
name, GPU type and count, image, data center, and hourly `cost` from that read,
and recheck the name when a later conversation resumes the work.

There is no ledger. Use the budget or runtime limit the user states, asking only
when none is given. Estimate spend as hourly cost times elapsed wall time since
handoff, report it at milestones and on completion, and stop launching new runs
before the limit. Disk and volume storage keep billing after the GPU stops.

Prepare code and a cheap local check before using the GPU, as with the
controller. Give each run an ID (UTC timestamp plus a short slug) and work under
`/workspace/research/RUN_ID/` on the pod. Check that `/workspace` is a mounted
volume before relying on it: the container disk is lost on stop, so put
environments, caches, and checkpoints that must survive on the volume. Upload an
immutable snapshot of tracked and unignored files (for example
`git ls-files -co --exclude-standard`) with rsync over SSH, recording the commit,
a hash of the uncommitted diff, and per-file hashes. Exclude secrets and caches.
Never put credentials in commands, files, or manifests; use variables the user
configured on the pod.

Launch detached (`tmux new-session -d` or `nohup`) with stdout and stderr in the
run directory, and write the exit code to a status file when the command ends.
Wait for that file from a background shell or monitor rather than repeated model
turns; a dropped SSH connection does not end a detached run. Collect small
outputs, logs, plotting inputs, and a `manifest.json` (run ID, pod ID and name,
GPU, image, source commit and hashes, argv, seeds, UTC start and end, exit code,
estimated cost) into the local project, under its existing results convention or
`runs/RUN_ID/`. Leave large checkpoints on the volume and record their remote
paths. Compare collected sizes or hashes with the remote copy, and never report
a partial or unverified collection as a final result.

On the pod, run only the experiment and its setup. Do not change account or pod
SSH keys, other users' processes, or other pods. When the work ends, report that
the pod is idle, the estimated spend, and what stopping retains, then offer to
stop it. Stop it only when the user says so: recheck the name, stop that pod by
ID, and read back until it reports stopped. Never terminate a pod.
