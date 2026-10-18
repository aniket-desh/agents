---
name: gpu-research
description: Run budgeted research experiments on RunPod GPUs. Picks the placement per experiment with no mode setting - a pod whose SSH command the user gives, else the local research-compute controller when reachable, else a pod it creates with the official Runpod tools under a self-stop timer. Use for any GPU experiment, allocation, results, spend, pause or continuation, and autonomous research requests.
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
your own controller, change owner configuration, or put provider credentials in
research code. While a controller is reachable, do not use direct RunPod
commands for its work.
If doctor reports the fake provider, treat runs as local validation and do not
report a simulated allocation as a real rented GPU.

## Where an experiment runs

There is no mode setting; decide for each experiment and say which in one line:

0. You are running on a Runpod pod (`RUNPOD_POD_ID` is set, or
   `/etc/rp_environment` exists): this pod is the GPU host; follow
   [This pod](#this-pod) and create no other pods unless the user asks.
1. The user gave an SSH command or named a pod: run there, as a
   [handed-over pod](#handed-over-pods).
2. Otherwise, if `research-compute doctor` reaches a live controller, use the
   controller workflow below.
3. Otherwise, [create a pod](#created-pods) with the official Runpod tools.

Every new pod must have a name starting with `aniket-`. Only access, execute on,
start, or stop pods whose current provider name starts with that exact prefix.
Never rename or adopt other users' pods. The controller enforces this prefix
and requires ledger ownership for reuse; outside the controller the prefix is
your own check, not a provider permission.

## Controller workflow

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

## Pods outside the controller

### This pod

The user set this pod up as a persistent agent host (`scripts/pod-setup.sh`),
so you, your tmux session, and the experiments share one machine. Confirm the
GPUs with `nvidia-smi`, and read the pod's name and hourly cost with
`runpodctl get pod "$RUNPOD_POD_ID"` (login shells export the variables) to
estimate spend. Run jobs locally with the same detached-run, status-file, and
manifest procedure below, keeping run directories, environments, caches, and
checkpoints under `/workspace`, since the container disk is lost on stop.

Do not arm a stop timer or stop this pod on your own: stopping it ends your
session and the user's. If the user sets an end time or asks you to stop
when done, first collect results, write the manifest and next steps to
`/workspace`, and report, then stop it with `runpodctl stop pod
"$RUNPOD_POD_ID"` (or `pod stop` on runpodctl 2.x). A job you launched
detached keeps running if your session ends; check for it before starting
another on resumption.

### Handed-over pods

The user rented the pod and handed over its SSH command. That authorizes
experiment work on that one pod; its lifecycle stays with the user. Identify it
before the first remote command: a proxy login `<pod_id>-<suffix>@ssh.runpod.io`
names the pod ID, and a direct `root@IP -p PORT` must match exactly one pod's
public TCP mapping in a read-only Runpod pod lookup. Confirm the current name
starts with `aniket-`; a missing name, failed lookup, ambiguous match, or other
prefix means refuse (the user can rename their own pod in the console). Use the
budget or runtime limit the user states. At the end, report the pod idle with
its estimated spend and offer to stop it; stop only when the user says so.

### Created pods

Create a pod only for an experiment the user asked to run, with a dollar budget
they stated or standing policy supplies; ask for a material missing limit. That
request is the go: quote the hourly price from a catalog read in the same reply
and create, without a separate confirmation. Verify the authenticated account
with a read before the first create, as the user's instructions require.

Pick the smallest GPU that fits the job from live stock (`list-gpu-types`,
`get-capacity`), and stay within the hourly price the budget allows for the
expected runtime. Create with `create-pod`: name
`aniket-<project>-<short-slug>`; a pinned image (default
`runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404` unless the project needs
another); `gpu` with `id`, `count`, and `minCudaVersion: "12.8"`; `cloud:
"SECURE"`; `ports: ["22/tcp"]`; `startSsh: true`, which injects the account's
registered SSH keys; a container `disk` near 30 GB; and
`mounts.persistent` at `/workspace` sized for environments, data, and
checkpoints. Read the pod back until it is `RUNNING` with a direct SSH address,
and confirm the name and GPU you set.

Arm a self-stop timer as the first remote command, so the pod stops even if
this machine sleeps or the conversation ends:

```sh
mkdir -p /workspace/research
nohup sh -c '. /etc/rp_environment; export RUNPOD_API_KEY RUNPOD_POD_ID
  sleep SECONDS
  runpodctl stop pod "$RUNPOD_POD_ID" || runpodctl pod stop "$RUNPOD_POD_ID"' \
  < /dev/null > /workspace/research/stop-timer.log 2>&1 &
```

SSH sessions do not inherit the pod's environment: `RUNPOD_POD_ID` and the
pod-scoped `RUNPOD_API_KEY` live in `/etc/rp_environment`, so source it. The
`runpodctl` in Runpod images may be 1.x (`stop pod ID`) or 2.x (`pod stop ID`);
the fallback covers both. Redirect stdin as shown, or the SSH command never
returns. Confirm the key works with `runpodctl get pod "$RUNPOD_POD_ID"` after
sourcing. Set `SECONDS` to the smaller of the runtime limit and the remaining
budget divided by the hourly cost, less a margin for collection. Without
`runpodctl` or a working key, say the pod has no self-stop and keep the run
short. To extend, kill that timer and arm a new one within the budget.

Check the pod's SSH host key against the fingerprints its container log prints
at startup (`stream-pod-logs`) when they are available. Every start creates a
new container, which brings a new host key and usually a new SSH port, so read
the pod again after each start.

Before creating another pod, reuse a stopped `aniket-` pod you created for the
same investigation (its ID is in the run manifests) by starting it, recognizing
that its GPU may be unavailable. When the work ends, stop the pod yourself,
read it back until it reports `EXITED`, and report the spend and the volume
storage that keeps billing. Never terminate a pod.

### Running on either kind

There is no ledger. Record the pod ID, name, GPU type and count, image, data
center, and hourly `cost` from a pod read. Estimate spend as hourly cost times
elapsed wall time since creation or handoff, report it at milestones and on
completion, and stop launching runs before the limit. Recheck the name when a
later conversation resumes the work.

Prepare code and a cheap local check before using the GPU. Give each run an ID
(UTC timestamp plus a short slug) and work under `/workspace/research/RUN_ID/`.
Check that `/workspace` is a mounted volume before relying on it: the container
disk is lost on stop, so put environments, caches, and checkpoints that must
survive on the volume. Upload an immutable snapshot of tracked and unignored
files (for example `git ls-files -co --exclude-standard`) with rsync over SSH,
recording the commit, a hash of the uncommitted diff, and per-file hashes.
Exclude secrets and caches. Never put credentials in commands, files, or
manifests; use variables configured on the pod.

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

On the pod, run only the experiment, its setup, and the stop timer. Do not
change account or pod SSH keys, other users' processes, or other pods.
