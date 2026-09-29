# Research agents and disposable GPU compute

Open a normal `codex`, `claude`, or `pi` conversation and describe the research.
This repo installs concise research guidance, GPU tools, and native continuation
integrations. A separate controller rents RunPod GPUs within shared limits,
collects small results, and **stops pods while retaining their volumes**.
It never terminates them. There is no `gpuc` dependency.

Install this repo once on your persistent host and build its isolated workspace
image. Inside that workspace, plain `codex` defaults to approval-free,
unsandboxed execution and plain `claude` adds `--dangerously-skip-permissions`.
The container supplies the filesystem boundary. Your ordinary host commands
and permission settings stay unchanged. Agents discover this repo's guidance
from their normal configuration while you work in your research repositories;
you don't paste this GitHub link into every conversation.

## An ordinary workday

SSH into your always-on Linux CPU host and enter your persistent workspace with
`research-workspace research`. This opens a shell inside the container; an SSH
alias can make that entry automatic. Then use ordinary tmux and agent commands:

```sh
tmux new-session -s backtracking
cd /workspace/temporal-crosscoders
codex
```

Say something like:

> Let's work through the backtracking experiments together. You can spend up
> to $10 on an A40, with a review at $5. Start with the smallest experiment that
> distinguishes the two hypotheses. Save the numerical inputs to the figures.

The agent writes code and runs a useful cheap check before renting. It uses the
installed tools internally; you don't manage experiment IDs or JSON yourself.
For a tiny SAE metric edit, a hand-checkable activation matrix may be the whole
validation. Money and data recovery machinery has more substantial tests.

Detach with `Ctrl-b d` and start another ordinary conversation:

```sh
tmux new-session -s temporal-settings
cd /workspace/temporal-crosscoders
codex
```

> Autoresearch temporal settings for this crosscoder until 5 pm today. You have
> $100 and up to two H100s. Arrange a separate worktree before editing because
> another conversation is working here. Compare against the existing baseline,
> choose useful follow-ups, and stop early when further runs aren't informative.

The agent records the actual date/timezone, budget, hardware, and objective,
then uses the native continuation integration. You can make as many independent
conversations as useful, including several autoresearch investigations. They
share the controller's GPU, hourly, storage, and cumulative spending limits;
submitted jobs wait when another investigation occupies the available capacity.
Each pod runs one experiment bundle at a time; a bundle can use multiple GPUs
or evaluate multiple seeds.

At lunch, detach or disconnect your laptop. The host keeps the agents, worker
collectors, and watchdog running. At 3 pm, reattach and say “How's it going?” or
“Pause after saving the current results.” Continue steering the same agent.
If the question is answered at 2 pm, the agent finishes early and stops its
pods. Ask for plot changes using the collected CPU-side results.

Replace `codex` with `claude` or `pi` to use the same research and compute tools.
Changing agents preserves the journal, artifacts, and budget, not the proprietary
chat transcript. Ask the old agent to pause before handing an investigation to
a new conversation; the binding prevents two autonomous drivers owning it.

## One-time installation

Use a Linux host with systemd, Python 3.11+, Git, and local Docker. Live RunPod
MCP also needs system-wide Node.js 20+ and npm/npx. The workspace image installs
Codex, Claude, tmux, Python, and SSH; authenticate the agents inside it.

```sh
git clone https://github.com/aniket-desh/agents.git ~/tools/agents
cd ~/tools/agents
./install.sh
```

The profile installer supports `--agents codex claude pi` or a subset. It adds
managed instruction blocks, installs two small skills, continuation adapters,
and `~/.local/bin/research-compute` plus the workspace entry command. It preserves existing instructions and
settings, backs up changes, and refuses to overwrite conflicting or locally
edited managed files. Ensure `~/.local/bin` is on your shell's PATH, then start
a new agent conversation. It doesn't install agent subscriptions or start GPU
services. `--target-home /tmp/research-profile` previews an isolated install.
It does not install permission-bypassing `codex`/`claude` wrappers on the host.
After deploying the controller below, follow [workspace setup](docs/workspaces.md)
to build the image and authenticate. The profile installs into its private home
automatically on first start.

**Autonomous continuation needs the agent's native support.** Codex may require
one-time hook trust through `/hooks`; don't skip that trust step. Codex native
goals are preferred when available. Claude's Stop hooks have a native consecutive
continuation limit, so all-day work needs its in-session scheduled-task support
or an ongoing wait/task; the skill explains this. Pi uses an in-process extension
that schedules a follow-up in the same conversation. Authentication failures,
usage limits, a closed agent, and a crashed host cannot be repaired by a prompt.
Bounded admitted experiments and GPU cleanup don't depend on further model turns.
Native login/onboarding and Codex hook trust remain separate one-time steps.
The managed workspace bypasses ordinary tool approvals by default; explicit
permission flags or a Codex profile override that default for a particular launch.

Set up the protected controller separately. Copy `config.example.json`, then
choose owner limits for *all* investigations combined. The example deliberately
uses a fake provider and cannot rent anything. For live use replace its provider
section with:

```json
{
  "kind": "mcp",
  "command": ["npx", "-y", "@runpod/mcp-server@4.0.0"],
  "env_file": "/etc/research-compute/runpod.env",
  "timeout_seconds": 45
}
```

Replace `research/fake:local` with an owner-approved, pinned GPU image or template
in `allowed_images` / `allowed_templates`. The worker needs Python 3.11+, direct
SSH, and an actual persistent mount at `/workspace`; the runner checks that mount.
Configure a dedicated RunPod account SSH public key and keep its private key
inside the workspace's private home, where its SSH collectors run.
Project dependencies belong in the image or a reproducible setup command, with
environments/caches/checkpoints under `/workspace` if they must survive stopping.
Trusted template data credentials must be visible to the SSH worker process;
don't put keys in commands, source files, or manifests.

Create a private `runpod.env` file containing `RUNPOD_API_KEY=...`, then deploy:

```sh
chmod 600 runpod.env
sudo ./scripts/install-controller.sh \
  --config my-controller.json --provider-env runpod.env \
  --client-user "$USER" --enable
```

Reconnect your login to pick up the socket group. The deployment copies the
runtime into `/opt/research-agents`, protects the key and SQLite ledger under
a separate service identity, and starts controller and watchdog systemd services.
Nothing runs as an interactive coding agent under that service identity. The
first MCP call can download the pinned npm package; preinstall/cache it if needed.
Omit `--enable` to stage without starting; `--stage-root /tmp/controller-preview`
previews the system files without root or system changes.

Have the agent run its installed doctor check, then explicitly authorize a tiny
paid pilot to verify SSH, rates, collection, and stop/restart persistence on your
actual RunPod account. The repository tests use fake providers; they are not
evidence of a successful live GPU deployment.

## What the limits mean

The protected owner allowance is cumulative across this controller's ledger,
including previous investigations. It isn't reset by a new conversation or a
new day. Session budgets subdivide it; all starts, retries, setup time, runtime,
and collection are estimated from pod lifetime, with an active-storage allowance
and reserved shutdown time. A $5 review inside a $20 budget resumes within the
same $20. An owner can deliberately amend an exhausted grant without erasing
spend; see [operator details](docs/compute-tools.md).

Retained storage has a separate standing GB cap and displayed monthly estimate.
It keeps billing after an experiment ends. Reusable stopped pods are preferred;
only you delete obsolete pods in RunPod. The watchdog recognizes confirmed
deletions and eventually releases their storage allowance. Stopped pods can't
accept SSH until restarted, and the original GPU capacity may be unavailable.

These are conservative estimates, not invoice reconciliation or an unconditional
provider-enforced dollar cap. RunPod API outages, slow provider calls, or failure
of the entire CPU host can delay stopping; an off-host reaper would be needed for
host-failure protection. Worker timeouts kill the experiment, not the GPU rental.
The independent watchdog handles agent/controller-process failure on a living host.

Your existing direct RunPod MCP installation can remain installed, but direct
rentals are outside this ledger. Installed guidance routes managed research
through this controller, which invokes the official MCP server itself. Strong
enforcement requires agents to lack alternative account keys/tools or permission
to change the protected service. The workspace mounts only the controller socket
directory from the host, with separate named volumes for its home and code. It
doesn't mount your personal home, SSH agent, or Docker socket. Agents can still
delete their own research files and use credentials you place inside that
workspace, so keep Git checkpoints and backups outside it. Workspaces sharing
your host UID also share the controller's owner identity; they aren't separate
untrusted tenants.

## Code and validation

- `guidance/` and `skills/` contain shared scientific coding and GPU workflows.
- `integrations/` contains native conversation adapters; no separate model API.
- `research_runtime/` contains the stdlib Python controller, MCP transport,
  detached SSH worker, collection, and agent-facing tools.
- `deploy/` and `scripts/` install the protected services and user profile.
- `workspace/` contains the isolated agent image and permission defaults.
- `tests/` exercises spending, recovery, installation, and offline execution.

Run the focused offline suite from this checkout:

```sh
python3 -m unittest discover -s tests -v
```

It launches temporary local services/processes and simulated pods, never paid
RunPod resources. One integration check computes mean L0 from `[0, 2]`, collects
the numerical result after the submitting command exits, and confirms a stopped,
retained fake pod. A restrictive macOS sandbox may need permission for local
sockets and process supervision to run that check.

[`PLAN.md`](PLAN.md) records implementation boundaries. The old `setup.sh`,
`provision/`, and optional `team/` bundle remain available under the
[legacy workflow](docs/legacy.md); the new installer does not activate them.
