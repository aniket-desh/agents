#!/usr/bin/env bash
# user-setup.sh: the user-side half of pod/bootstrap.sh. Everything it writes
# lives under the persistent home or /workspace, so each step is skipped or
# refreshed in place when it runs again after a restart.
set -euo pipefail

SETUP=/workspace/.agent-setup
# shellcheck disable=SC1091
. "$SETUP/pod.env"
USER_ROOT="/workspace/$USER_NAME"
export PATH="$HOME/.local/bin:$PATH" LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

echo ">> shell environment"
add_line() {
    for rc in "$HOME/.bashrc" "$HOME/.profile"; do
        touch "$rc"
        grep -qxF "$1" "$rc" || echo "$1" >> "$rc"
    done
}
add_line 'export PATH="$HOME/.local/bin:$PATH"'
add_line 'export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8'
add_line 'export HF_HOME=/workspace/.cache/huggingface'
# SSH sessions do not inherit the pod's ID and pod-scoped API key; export them
# so an agent can read this pod's cost and stop it when told to.
add_line "[ -r /etc/rp_environment ] && eval \"\$(grep -E '^(export )?RUNPOD_' /etc/rp_environment | sed -E 's/^(export )?/export /')\""
# An exported ANTHROPIC_API_KEY makes Claude Code bill API rates even when
# logged in to a subscription, so interactive launches drop it.
add_line "alias claude='env -u ANTHROPIC_API_KEY claude'"
# Launchers are redefined on every run, so drop earlier versions first.
for rc in "$HOME/.bashrc" "$HOME/.profile"; do
    sed -i -E '/^(claude-pod|claude-pod-yolo|codex-pod)\(\) \{/d' "$rc"
done
# claude-pod: auto mode lets routine work run without prompts while screening
# risky actions, and Remote Control lets the Claude app or phone drive the
# session that tmux keeps alive here. claude-pod-yolo skips all checks.
add_line "claude-pod() { env -u ANTHROPIC_API_KEY claude --permission-mode auto --remote-control \"pod-\$(basename \"\$PWD\")\" \"\$@\"; }"
add_line "claude-pod-yolo() { env -u ANTHROPIC_API_KEY claude --dangerously-skip-permissions \"\$@\"; }"
add_line "codex-pod() { codex --dangerously-bypass-approvals-and-sandbox \"\$@\"; }"
# Only the laptop key that ran pod-setup.sh may log in as this user; sshd's
# StrictModes rejects a group-writable home or key file.
chmod 755 "$HOME"
install -d -m 700 "$HOME/.ssh"
install -m 600 "$SETUP/private/authorized_keys" "$HOME/.ssh/authorized_keys"
mkdir -p /workspace/.cache/huggingface 2>/dev/null || echo "   WARNING: cannot create /workspace/.cache/huggingface" >&2
[ -f "$HOME/.tmux.conf" ] || cat > "$HOME/.tmux.conf" <<'EOF'
set -g default-terminal "tmux-256color"
set -ga terminal-overrides ",xterm-256color:Tc,xterm*:Tc"
set -g history-limit 50000
set -g mouse on
EOF

echo ">> agent CLIs"
if ! command -v claude >/dev/null 2>&1; then
    curl -fsSL https://claude.ai/install.sh | bash
fi
if ! command -v codex >/dev/null 2>&1; then
    curl -fsSL https://chatgpt.com/codex/install.sh | sh
fi
hash -r
for cli in claude codex; do
    command -v "$cli" >/dev/null 2>&1 || { echo "ERROR: $cli is not on PATH after install." >&2; exit 1; }
    echo "   $cli: $("$cli" --version 2>/dev/null | head -1)"
done

echo ">> personal instructions, research profile, and skills"
mkdir -p "$HOME/.claude" "$HOME/.codex" "$HOME/.agents/skills" "$HOME/.claude/skills"
# The laptop's own text sits above the research-agents block in a marked
# block of its own, refreshed on every run; the installer then adds or
# refreshes its block below it.
python3 - "$SETUP/private" <<'PY'
import pathlib, sys
private = pathlib.Path(sys.argv[1])
begin, end = "<!-- pod-profile:begin -->", "<!-- pod-profile:end -->"
home = pathlib.Path.home()
for name, path in (("claude-preamble.md", home / ".claude/CLAUDE.md"),
                   ("codex-preamble.md", home / ".codex/AGENTS.md")):
    text = path.read_text() if path.exists() else ""
    if begin in text and end in text:
        text = text[:text.index(begin)] + text[text.index(end) + len(end):].lstrip("\n")
    preamble = (private / name).read_text().strip()
    block = f"{begin}\n{preamble}\n{end}\n\n" if preamble else ""
    path.write_text(block + text)
PY
python3 "$SETUP/agents/scripts/install.py" --agents claude codex >/dev/null
for skill_dir in "$SETUP"/private/skills/*/; do
    [ -d "$skill_dir" ] || continue
    skill="$(basename "$skill_dir")"
    rsync -a --delete "$skill_dir" "$HOME/.agents/skills/$skill/"
    link="$HOME/.claude/skills/$skill"
    if [ -e "$link" ] && [ ! -L "$link" ]; then
        echo "   kept existing $link (not a link)"
    else
        ln -sfn "$HOME/.agents/skills/$skill" "$link"
    fi
done
if [ ! -f "$HOME/.codex/config.toml" ] && [ -s "$SETUP/private/codex-config.toml" ]; then
    cp "$SETUP/private/codex-config.toml" "$HOME/.codex/config.toml"
fi
echo "   skills: $(ls "$HOME/.claude/skills" | tr '\n' ' ')"

echo ">> project"
REPO_DIR="$USER_ROOT/$(basename "$REPO" .git)"
clone() {
    # Fail fast instead of prompting for a password GitHub would reject.
    if [ -n "$BRANCH" ]; then
        GIT_TERMINAL_PROMPT=0 git clone --branch "$BRANCH" "$REPO" "$REPO_DIR"
    else
        GIT_TERMINAL_PROMPT=0 git clone "$REPO" "$REPO_DIR"
    fi
}
cloned=1
if [ ! -d "$REPO_DIR/.git" ]; then
    if ! clone; then
        # A private repository needs GitHub sign-in, which needs a person at a
        # terminal; without one, finish the rest and let bootstrap.sh explain.
        if [ -t 0 ] && ! gh auth status >/dev/null 2>&1; then
            echo "   Clone failed; sign in to GitHub (device code, stored in this home):"
            gh auth login --hostname github.com --git-protocol https --web || true
        fi
        if ! { gh auth status >/dev/null 2>&1 && gh auth setup-git && clone; }; then
            cloned=0
            echo "   WARNING: could not clone $REPO; continuing without the project." >&2
        fi
    fi
else
    # Never move an existing checkout: a running investigation may depend on it.
    echo "   existing checkout left as is: $(git -C "$REPO_DIR" status --short --branch | head -1)"
fi
PROJECT_DIR="$REPO_DIR${PROJECT_SUBDIR:+/$PROJECT_SUBDIR}"
marker="$USER_ROOT/.project-setup-done-$(basename "$REPO_DIR")"
if [ "$cloned" = 1 ] && [ "$PROJECT_SETUP" = 1 ] && [ ! -f "$marker" ]; then
    helper="$PROJECT_DIR/scripts/runpod_setup.sh"
    [ -f "$helper" ] || helper="$SETUP/agents/provision/runpod_setup.sh"
    if (cd "$PROJECT_DIR" && RUNPOD_PROJECT_DIR="$PROJECT_DIR" bash "$helper"); then
        touch "$marker"
    else
        echo "   WARNING: project setup failed; the agent can finish it from the project directory." >&2
    fi
fi
echo "   project: $PROJECT_DIR"
