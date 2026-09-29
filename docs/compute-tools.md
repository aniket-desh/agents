# Internal tools and operator reference

These are commands for the coding agent or the person installing the host. The
daily human interface is the normal agent conversation described in the README.
All tools return JSON. `research-compute --help` lists exact argument names.

## A policy and a bounded experiment

First run `research-compute doctor` to inspect configured hardware/images, the
provider kind, and watchdog health. A fake provider never represents paid GPU
capacity. Limits below are examples; they must fit the actual owner allowance.

```json
{
  "objective": "Compare two backtracking settings against the pinned baseline",
  "mode": "interactive",
  "budget_usd": 10,
  "review_at_usd": 5,
  "max_gpus": 1,
  "max_pods": 1,
  "allowed_gpu_types": ["NVIDIA A40"],
  "max_hourly_usd": 2,
  "max_volume_gb": 40
}
```

Create once with `research-compute session create --policy policy.json`. Reuse
the returned ID throughout that investigation. Set `mode` to `auto` for requested
autonomy; an optional `deadline` must be an actual ISO timestamp with timezone,
for example an explicitly resolved 17:00 in the user's timezone. Don't invent a
new budget when a job retries or a conversation resumes.

Bind using the exact native conversation/workspace context injected by the
installed hook or Pi extension:

```sh
research-compute session bind SESSION_ID --agent codex \
  --conversation-id NATIVE_ID --workspace /exact/native/workspace
```

Use a separate mutable worktree/environment for independent coding conversations.
The binding workspace is the exact native context, while the experiment's `repo`
may point to a different isolated worktree. Changing a shell tool's working
directory doesn't necessarily change the native context; don't rebind to that
worktree unless the native agent itself supplies it as its new workspace.
For a new agent driver, pause the investigation, confirm stopped allocations,
then bind with `--take-over` and resume; don't run two writers concurrently.

The agent prepares a run specification such as:

```json
{
  "repo": "/absolute/research/worktree",
  "command": ["python3", "experiments/compare.py", "--seed", "0"],
  "outputs": ["results"],
  "timeout_seconds": 600,
  "request": {
    "gpu_type": "NVIDIA A40",
    "gpu_count": 1,
    "volume_gb": 40,
    "container_disk_gb": 20,
    "image": "OWNER_APPROVED_PINNED_IMAGE",
    "duration_seconds": 1200
  }
}
```

Use `template_id` instead of `image` when the owner configured a template. The
lease includes provisioning, dependencies, experiment execution, and collection;
the worker runtime is shortened when setup consumes that lease. The controller
also reserves a shutdown margin. Never submit an experiment that assumes the
full lease is available as Python runtime. For nested projects, use an existing
reproducible project command with its working-directory change, for example an
argv array beginning `bash`, `-lc`, followed by the complete project command.

`research-compute run --session SESSION_ID --spec run.json` snapshots tracked and
unignored working files before allocation, excluding known secret/cache paths.
It registers the job before returning, then runs a detached collector. Pending
capacity waits up to `queue_timeout_seconds` (default 3600); no paid pod is
reserved while waiting. This is a small admission queue, with no fairness,
preemption, or job-packing scheduler. `--foreground` waits for completion and
returns a nonzero exit on failure. `--local` is restricted to the fake provider.
Don't call `allocate` before `run`, which owns its own allocation.

Useful optional run fields are `ssh_key`, `exclude` (relative paths),
`max_output_bytes` (default 64 MiB), `collect_interval_seconds` (default 30), and
`queue_timeout_seconds`. `command` is an argv array, not an implicitly evaluated
shell string. No credentials belong in that command or specification. The
runner preserves worker-template dataset credentials, strips RunPod key/token
environment variables from child processes, and does not pass the host's agent
login to the pod. This does not prevent hostile code from reading its parent or
provider metadata; the runner is not an isolation boundary against hostile code.

## Results, status, and cancellation

Use `jobs --session SESSION_ID` or `wait --session SESSION_ID --timeout-seconds 60`.
Wait inside the agent's supported long-running tool/task mechanism instead of
repeatedly asking a model to poll. Read `session status SESSION_ID` for the full
journal, jobs, spend estimates, reservations, retained storage, and stop states.
`session journal SESSION_ID --text TEXT` saves evidence and the next action.

Each local job directory is under `~/.local/state/research-agents/runs/` unless
`RESEARCH_COMPUTE_RUNS_DIR` overrides it. It contains an immutable source archive
and hash manifest, `job.json`, supervisor logs, and a collected `outputs/` bundle.
Inside that bundle, `outputs/results/` contains the requested relative results;
`collection.json` records hashes and whether the copy was provisional. Partial
collections must not be reported as final results. The agent's experiment should
save seeds, configuration, data/model revisions, per-step/per-seed metrics, and
plotting inputs; the infrastructure cannot infer missing scientific metadata.
Collections are verified before replacing the prior owned bundle. Keep published
bundles unchanged and save reformatted plots beside them; collection refuses to
overwrite human edits or an unowned directory.

Remote jobs live under `/workspace/research/SESSION_ID/JOB_ID/`. Save large
checkpoints there and write important outputs atomically. A SIGTERM-aware training
script can checkpoint during the worker's ten-second grace; the controller's
default checkpoint grace is thirty seconds and the allocation lease still wins.
Collection failure doesn't extend the GPU lease. It is recorded explicitly and
the retained volume remains available for later recovery.

Use `session pause`, `review`, `block`, or `finish` with the ID and an optional
`--reason`. A review gate can be entered slightly early when another useful
experiment won't fit. These operations cover every allocation in the investigation.
Check each allocation for `STOPPED`; `STOPPING` means shutdown is unconfirmed.
Resume a review only after an actual user instruction, using
`session resume SESSION_ID --user-confirmed`. That flag records the instruction;
it is not a cryptographic proof that a human approved it.

`collect JOB_DIR` retries collection from an accessible existing worker. For a
stopped pod, the agent first obtains a new budgeted allocation for that same
`pod_id`, refreshes the saved SSH coordinates in its local job record, retries
collection, and stops the recovery allocation. This may fail when the original
GPU is unavailable; use the provider's documented recovery options with the human.
No automatic deletion or replacement of missing data is allowed.

## Native continuation

Set `continuation_driver` in the policy, or use `session driver SESSION_ID native`
or `hook`. `native` means a supported vendor goal/schedule owns future turns and
the installed Stop hook does nothing. `hook` permits the installed Stop hook or
Pi extension to request a same-conversation continuation. Never run two drivers.
Hooks use `session continuation` internally, not a new model subprocess.

The hook driver has a finite `max_iterations` allowance (default 50), detects
three failed experiments or continuations without recorded progress, and waits
without consuming iterations while jobs are pending. This bounds fallback
continuation requests, not native goal turns or model-token spending. A native
goal/schedule must read the controller status before each research iteration,
honor blocked/review/deadline states, and retire itself when the investigation
ends. Pause both the native driver and the controller when the user asks to stop.

Native hook trust and execution permissions are separate from compute policy.
The managed workspace image defaults to permission-free Codex/Claude execution
inside its container boundary; it preserves native hook trust. The standalone
profile installer leaves host permissions unchanged. If using that profile
outside the managed workspace, approve the installed tool through the native
approval flow when necessary. See [workspace settings](workspaces.md).

## Owner operations

Session ceilings cannot be raised through the agent socket. To deliberately
extend an exhausted investigation, pause/confirm stops, create a patch such as
`{"budget_usd": 30, "review_at_usd": null}`, then run on the controller host:

```sh
sudo -u research-compute /opt/research-agents/bin/research-compute owner-amend \
  --config /etc/research-compute/config.json \
  --state-dir /var/lib/research-compute --session SESSION_ID --patch /path/to/patch.json
```

The patch must be readable by the service account. It can change total budget,
deadline, review threshold, and hook iteration allowance; it preserves prior
spend and leaves the session paused. Increasing the account allowance itself
requires an owner config update and restart of both services after all work is
paused/stopped. It remains a cumulative total, not a fresh balance. Don't erase
the ledger to obtain a new allowance.

An ambiguous creation with no known pod ID deliberately blocks new allocation.
The watchdog searches for its unique `research-ALLOCATION_ID` name; it does not
blindly create a replacement. If a human has checked the settled provider state
and confirmed no pod was ever created, the local owner can release that
reservation with `owner-resolve-no-pod --config FILE --state-dir DIRECTORY
--allocation ID --confirmed-no-pod --reason EVIDENCE`. Use the same service-owner
execution as `owner-amend`. This attestation never clears an allocation with a
known pod ID; ordinary clients cannot perform it. A transient empty listing is
not sufficient evidence while creation might still be pending.

Inspect service problems with `systemctl status research-compute.service
research-compute-watchdog.service` and their journalctl logs. Updating an existing
deployment requires stopping both services after stopping experiments, then
rerunning the installer; use `--replace-config` only deliberately. Retain the
ledger directory. The controller checks its watchdog heartbeat before admitting
paid work, and unfinished create requests retain their liability until resolved.

The owner config/keys belong outside the research user's access. Remove or move
the original deployment credential copy to an owner-only location after installing
it if the research user could otherwise read it. Existing direct provider MCP
tools and unmanaged pods are outside this controller's accounting.
