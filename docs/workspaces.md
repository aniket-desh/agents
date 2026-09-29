# Isolated research terminals

Use the managed workspace on the persistent Linux CPU host. Your laptop runs
the terminal; code, agent sessions, and collected results live in the workspace.
RunPod GPUs remain separate workers, so stopping one doesn't close your agent.

Inside this image, ordinary launches have these defaults:

| Command | Default |
|---|---|
| `codex` | `approval_policy="never"`, `sandbox_mode="danger-full-access"`, equivalent to full bypass; also applied to exec/resume/fork/review |
| `claude` | `--dangerously-skip-permissions`, including print/continue/resume sessions |
| `pi` | Shared research profile and continuation extension; install your chosen Pi distribution inside the workspace separately |

Explicit permission options override these defaults. For example, use
`codex --sandbox workspace-write --ask-for-approval on-request` or
`claude --permission-mode plan`. An explicit Codex profile/config permission
setting, Codex remote endpoint, or Claude settings file also suppresses the
automatic defaults. Login, management, help, and version commands pass through.
Native onboarding and Codex hook trust remain separate from tool permissions.

The [Codex approval documentation](https://learn.chatgpt.com/docs/agent-approvals-security)
distinguishes tool approval from sandboxing. Here Docker provides the outer
boundary, following its [container runtime controls](https://docs.docker.com/engine/containers/run/).
The wrappers are part of the image; the host profile installer never changes
your ordinary local `codex` or `claude` launch permissions.

## Set up once on the CPU host

First install the profile and protected controller as described in the README.
Docker must be installed on this Linux host and accessible to your ordinary
host user. The current launcher requires a local rootful daemon without UID
remapping, because the broker authenticates the client's numeric UID. Docker
access is host administration access; coding agents don't receive that socket.

From this checkout, build the image for your host user:

```sh
docker build -f workspace/Dockerfile \
  --build-arg USER_UID="$(id -u)" --build-arg USER_GID="$(id -g)" \
  -t research-agents-workspace:local .
research-workspace research
```

The build installs Codex 0.158.0 and Claude Code 2.1.205. Their versions are build
arguments; update deliberately and recheck native behavior. This image recipe
has not yet been built and exercised on a real Docker host. The offline tests
validate its launch policy and wrapper logic, not a successful deployment.

On first entry, the workspace installs the shared research instructions, skills,
and continuation integrations into its private home. Authenticate your agents
there with their native login flows, and clone research repositories under
`/workspace`. Give it dedicated GitHub/worker credentials scoped to the research
you intend it to access. Don't copy your whole personal home or forward your
personal SSH agent. Configure the dedicated worker SSH public key in RunPod;
the private key stays inside this workspace for its SSH collectors.

The RunPod account API key stays with the protected host controller. Don't
install a second full-access RunPod MCP/key inside the workspace, since that
would bypass the controller's spending and stop-only restrictions.

## Daily use

After SSHing to the host, `research-workspace research` opens a normal shell.
The same name reuses its persistent home, checkout, and running processes. In
that shell, use the workflow you already know:

```sh
tmux new-session -s backtracking
cd /workspace/temporal-crosscoders
codex
```

Detach with `Ctrl-b d` to return to the workspace shell. Open another session:

```sh
tmux new-session -s temporal-settings
cd /workspace/temporal-crosscoders
claude
```

Tell it the research objective, compute budget, and deadline. For concurrent
work in one repo, have each conversation use its own worktree. Add as many tmux
sessions as useful. Reattach with `tmux attach -t temporal-settings`; opening
another Ghostty tab and entering the same workspace also works. The CPU
container and its tmux sessions keep running after your laptop disconnects.

To hide the entry command entirely, configure a local SSH alias (substitute
your actual hostname, host username, and home path):

```sshconfig
Host research
    HostName YOUR_CPU_HOST
    User YOUR_HOST_USER
    RequestTTY yes
    ForwardAgent no
    RemoteCommand /home/YOUR_HOST_USER/.local/bin/research-workspace research
```

Then `ssh research` opens the isolated shell directly. Ask agents to commit and
push research branches when you want to review on GitHub or pull them into a
separate local clone; GitHub doesn't need to hold datasets or large checkpoints.

## What persists and what is accessible

Each workspace has private named Docker volumes for `/home/research` and
`/workspace`. The only host directory bound into it is `/run/research-compute`,
read-only, for the controller socket. Binding the directory lets socket
replacement survive a controller restart. No host home, checkout, SSH agent,
Docker socket, or RunPod account key is forwarded.

Processes run as a nonroot user with dropped capabilities, no privilege
escalation, and a read-only image filesystem. The launcher caps each workspace
at 4 CPU cores, 8 GiB RAM, no additional swap, and 1,024 processes. It validates
existing containers and volumes before reuse and refuses unexpected mounts or
permissions instead of replacing or deleting them. The host owner must review
image/policy changes and any deliberate migration; the launcher never prunes
containers, volumes, or GPU pods.

The research home and checkout are writable, so an agent can still delete its
own work. Keep Git checkpoints and independent backups outside the container.
It can also use any GitHub, model, or SSH credential placed inside. Outbound
networking remains enabled for model APIs and workers; this isn't a network
access firewall or a guarantee against container escapes. All workspaces mapped
to your UID share the controller owner identity and its API-visible research.

System package changes require rebuilding the image; Python environments,
Node packages, and caches can live in your writable home or workspace. `/tmp`
is a 256 MiB temporary filesystem; set `TMPDIR` to a directory under your home
for dependency builds that need more scratch space. A CPU-host reboot ends
running agent processes; re-enter the workspace and use the agent's native
resume command. Persistent files and conversations survive, but unattended
model turns aren't automatically restarted after a host failure.
