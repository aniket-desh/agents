## Legacy pod-local agent setup

This older workflow remains available for existing installations. New installs
use the [persistent-host workflow](../README.md). The optional team bundle and
its hooks are not installed by the new `install.sh`.

A two-part setup repo for running AI research on rented GPUs:

1. **`provision/`** — one curl-pipe-bash that takes a fresh RunPod from absolute
   zero (root ssh, blank `/workspace`) to "tmux + Codex running as a non-root
   user inside a uv venv." (This was the whole `autoresearch` repo; it's now one
   subsystem.)
2. **`team/` + the Claude Code bundle** — a **team-of-agents-run-by-an-agent**
   workflow retained as an optional legacy layer.

The current design is recorded in [`PLAN.md`](../PLAN.md).

---

## quickstart

ssh into a fresh RunPod as root:

```bash
curl -fsSL https://raw.githubusercontent.com/aniket-desh/agents/main/setup.sh \
  | REPO=https://github.com/<owner>/<project>.git \
    BRANCH=<branch> \
    USER_NAME=<user> \
    PROJECT_SUBDIR=<optional/python-project-subdir> \
    AGENT_CLI=codex \
    INSTALL_TEAM=0 \
    bash
```

Then:

```bash
su - <user>
cd /workspace/<user>/<project>/<optional-subdir>
export RUNPOD_PROJECT_DIR="$PWD"
source /workspace/<user>/.agents/provision/runpod_activate.sh
codex login --device-auth
tmux new-session -A -s <user>
# inside tmux, from the Git repository root:
codex --sandbox workspace-write --approve-for-me
```

Codex's credentials and a global RunPod research-lead `AGENTS.md` live under
`/workspace/<user>/.codex`, so authentication and subagent guidance survive pod
restarts. Codex's built-in subagents are capped at four concurrent workers.

For the current Temporal Crosscoders branch, the exact root-side bootstrap is:

```bash
curl -fsSL https://raw.githubusercontent.com/aniket-desh/agents/main/setup.sh \
  | REPO=https://github.com/chainik1125/temp_xc.git \
    BRANCH=neurips-aniket \
    USER_NAME=aniket \
    TMUX_SESSION=lead \
    PROJECT_SUBDIR=purified \
    AGENT_CLI=codex \
    INSTALL_TEAM=0 \
    bash
```

To use the retained Claude team implementation instead, set
`AGENT_CLI=claude INSTALL_TEAM=1`, then follow the printed `team` instructions.

---

## legacy Claude team billing

You run interactive agents on your **Claude subscription**, and reserve the
`ANTHROPIC_API_KEY` for **headless autointerp / LLM-judge calls only**.

> If `ANTHROPIC_API_KEY` is exported when you launch `claude`, Claude Code bills
> every interactive turn at API pay-as-you-go rates **even while you're logged
> in on a subscription** — the key silently takes precedence. `provision/runpod_activate.sh`
> fixes this: it stashes the key as `AUTOINTERP_ANTHROPIC_API_KEY` and aliases
> `claude` to launch with the key stripped (`env -u ANTHROPIC_API_KEY claude`).
> The judge hook re-injects the stashed key only for its own headless calls.

---

## repo structure

```
setup.sh                          one-shot pod bootstrap (Codex by default; team optional)
PLAN.md                           full design + rationale

codex/
  AGENTS.md                       persistent lead/subagent/GPU coordination guidance

provision/                        SUBSYSTEM 1 — the RunPod bootstrap
  runpod_setup.sh                 uv sync + .env template
  runpod_activate.sh              sources .env, activates venv, configures caches

team/                             SUBSYSTEM 2 — the agent-team layer
  team.sh                         launcher: name-sample + GPU-partition + native --worktree/--tmux
  msg.sh                          agent-to-agent messaging (tmux send-keys)
  names.txt                       243 mathematician/physicist surnames (sampled w/o replacement)
  install.sh                      installs the bundle below into CLAUDE_CONFIG_DIR + launchers on PATH
  bundle/                         the Claude Code config that lands on the pod
    agents/{main,peer}.md         orchestrator + worker roles
    commands/{howto,peers}.md     custom slash commands (/howto cheat-sheet, /peers dashboard)
    skills/thermonuclear-review/  deep adversarial review skill
    hooks/                        guard / judge / postcompact / mailbox-drain / worktree-link
    settings.json                 permissions.deny backstop + hook wiring
```

> The pod bundle lives under `team/bundle/` (not the repo's own `.claude/`) on
> purpose — so working on *this* repo doesn't activate the pod hooks. `install.sh`
> copies it into `CLAUDE_CONFIG_DIR` on the pod.

---

## provisioning details

`setup.sh` is idempotent and handles real-pod gotchas (locale, agent install,
`/workspace/.cache` permissions, PATH inheritance). After a pod stop/restart it
re-bootstraps from a stashed `/workspace/bootstrap.sh` with your env baked in:

```bash
bash /workspace/bootstrap.sh
```

If your project repo ships its own `scripts/runpod_setup.sh` /
`scripts/runpod_activate.sh`, those are used; otherwise the bundled
`provision/` versions are fetched under `/workspace/<user>/.agents/`, outside
the checkout. This keeps a freshly cloned research tree clean.

| var | required | default | what it does |
|---|---|---|---|
| `REPO` | yes | — | git url to clone |
| `BRANCH` | yes | — | branch to check out |
| `USER_NAME` | no | `researcher` | non-root user to create + run the agent CLI as |
| `TMUX_SESSION` | no | `${USER_NAME}` | tmux session name printed in instructions |
| `REPO_DIR_NAME` | no | `basename(REPO)` | clone target dir under `/workspace/<user>/` |
| `PROJECT_SUBDIR` | no | empty | nested directory containing `pyproject.toml`, such as `purified` |
| `AGENT_CLI` | no | `codex` | install `codex` or `claude` |
| `INSTALL_TEAM` | no | `0` | install the legacy Claude team layer; requires `AGENT_CLI=claude` |
| `AGENTS_RAW` | no | this repo's raw URL | override to install from a fork/branch |

---

## the agent-team layer

The topology is two layers (see `PLAN.md` §3–4 for the full rationale):

```
you ── talk to ──► main (orchestrator)
        ┌────────────┼────────────┐
     Hilbert       Gauss       Poincaré      ← persistent peers, one idea each
        │            │            │
   each "use a workflow…" → ephemeral subagents (impl → 2 verifiers → fixer)
```

Key commands once `team` is up:

- `msg <name> "<text>"` — inject a message into another agent's session
- `claude agents` — the native control plane (sessions grouped by status)
- `/goal <condition>` — keep a peer working until its acceptance criteria pass
- `/loop <interval> <prompt>` — overnight cadence on the pod (not `/schedule`,
  which runs on Anthropic infra and can't see your GPUs)

**Watch-outs:** token burn is multiplicative across peers × their workflows, so
run one peer's workflow at a time and size the team to your plan limit, not the
GPU count. Keep research workflow subagents non-isolated (they read + run evals,
not edit code in parallel).

---

## license

mit. use it for whatever.
