#!/usr/bin/env bash
# pod-setup.sh: run on your laptop to turn a Runpod pod you opened into a
# persistent agent host (Claude Code + Codex in tmux, with this repo's research
# profile, your private skills and instructions, and a project checkout).
#
#   scripts/pod-setup.sh 'ssh root@IP -p PORT' --repo URL [--branch BRANCH]
#       [--user NAME] [--subdir PATH] [--session NAME] [--key PUBKEY]
#       [--no-project-setup]
#
# The first argument is the SSH command the Runpod console shows (direct TCP,
# not the ssh.runpod.io proxy, which cannot run commands). Everything the pod
# needs travels over that connection, so nothing private is published: the
# agents checkout as it is on disk, the private skills listed in
# PRIVATE_SKILLS, the parts of ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md above
# the research-agents block, and Codex's model settings.
#
# On the pod, pod/bootstrap.sh does the setup. After a pod restart, run
# `bash /workspace/bootstrap.sh` there as root; this laptop is not needed.
set -euo pipefail

usage() {
    sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

[ $# -ge 1 ] || usage
read -r -a SSH_ARGS <<< "$1"
shift
[ "${SSH_ARGS[0]:-}" = ssh ] && SSH_ARGS=("${SSH_ARGS[@]:1}")
[ ${#SSH_ARGS[@]} -gt 0 ] || usage

REPO="" BRANCH="" USER_NAME="${USER:-researcher}" PROJECT_SUBDIR=""
TMUX_SESSION="research" PROJECT_SETUP=1 PUBKEY="$HOME/.ssh/id_ed25519.pub"
PRIVATE_SKILLS="${PRIVATE_SKILLS:-research-note write-technical-prose}"
while [ $# -gt 0 ]; do
    case "$1" in
        --repo) REPO="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        --user) USER_NAME="$2"; shift 2 ;;
        --subdir) PROJECT_SUBDIR="$2"; shift 2 ;;
        --session) TMUX_SESSION="$2"; shift 2 ;;
        --key) PUBKEY="$2"; shift 2 ;;
        --no-project-setup) PROJECT_SETUP=0; shift ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1" >&2; usage ;;
    esac
done
[ -n "$REPO" ] || { echo "--repo is required." >&2; exit 2; }
case "$USER_NAME" in root|"") echo "--user must name a non-root account." >&2; exit 2 ;; esac
case "$PROJECT_SUBDIR" in /*|../*|*/../*|*/..) echo "--subdir must be relative." >&2; exit 2 ;; esac
[ -f "$PUBKEY" ] || { echo "Public key not found: $PUBKEY (pass --key)." >&2; exit 2; }

AGENTS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/agents" "$STAGE/private/skills"

echo ">> packing the agents checkout ($(git -C "$AGENTS_DIR" rev-parse --abbrev-ref HEAD) @ $(git -C "$AGENTS_DIR" rev-parse --short HEAD))"
(cd "$AGENTS_DIR" && git ls-files -co --exclude-standard -z -- . ':!:.claude/' ':!:**/.DS_Store' ':!:.DS_Store') \
    | (cd "$AGENTS_DIR" && rsync -a --from0 --files-from=- ./ "$STAGE/agents/")

echo ">> packing private skills: $PRIVATE_SKILLS"
for skill in $PRIVATE_SKILLS; do
    source_dir="$HOME/.agents/skills/$skill"
    [ -f "$source_dir/SKILL.md" ] || { echo "   missing $source_dir/SKILL.md; skipped"; continue; }
    rsync -aL --exclude '.katex-cache' --exclude '.DS_Store' "$source_dir/" "$STAGE/private/skills/$skill/"
done

echo ">> packing personal instructions and Codex model settings"
# Everything above the research-agents block is the user's own text (writing
# style, Runpod rules); the block itself is regenerated on the pod.
python3 - "$STAGE/private" <<'PY'
import pathlib, re, sys
out = pathlib.Path(sys.argv[1])
home = pathlib.Path.home()
for name, path in (("claude-preamble.md", home / ".claude/CLAUDE.md"),
                   ("codex-preamble.md", home / ".codex/AGENTS.md")):
    text = path.read_text() if path.exists() else ""
    text = text.split("<!-- research-agents:begin -->")[0].rstrip() + "\n"
    (out / name).write_text(text if text.strip() else "")
config = home / ".codex/config.toml"
keep = [line for line in (config.read_text().splitlines() if config.exists() else [])
        if re.match(r"\s*(model|model_reasoning_effort|personality)\s*=", line)]
(out / "codex-config.toml").write_text("\n".join(keep) + ("\n" if keep else ""))
PY
cp "$PUBKEY" "$STAGE/private/authorized_keys"

{
    printf 'REPO=%q\n' "$REPO"
    printf 'BRANCH=%q\n' "$BRANCH"
    printf 'USER_NAME=%q\n' "$USER_NAME"
    printf 'PROJECT_SUBDIR=%q\n' "$PROJECT_SUBDIR"
    printf 'TMUX_SESSION=%q\n' "$TMUX_SESSION"
    printf 'PROJECT_SETUP=%q\n' "$PROJECT_SETUP"
} > "$STAGE/pod.env"

echo ">> copying to the pod's /workspace/.agent-setup"
# COPYFILE_DISABLE keeps macOS tar from adding ._ metadata files.
COPYFILE_DISABLE=1 tar -C "$STAGE" -czf - . | ssh "${SSH_ARGS[@]}" \
    'set -e; rm -rf /workspace/.agent-setup.new; mkdir -p /workspace/.agent-setup.new
     tar -xzf - -C /workspace/.agent-setup.new --no-same-owner
     rm -rf /workspace/.agent-setup; mv /workspace/.agent-setup.new /workspace/.agent-setup
     chmod -R go-rwx /workspace/.agent-setup'

echo ">> running the pod bootstrap"
ssh -t "${SSH_ARGS[@]}" 'bash /workspace/.agent-setup/agents/pod/bootstrap.sh'
