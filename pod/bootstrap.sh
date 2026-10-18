#!/usr/bin/env bash
# bootstrap.sh: runs on the pod as root. scripts/pod-setup.sh on the laptop
# copies /workspace/.agent-setup and calls this; after a pod restart, run
# `bash /workspace/bootstrap.sh`, which calls it again.
#
# Runpod keeps only the /workspace volume across a stop. The user's home is
# therefore /workspace/<user>/home, so logins, the claude and codex binaries,
# skills, and settings persist; this script rebuilds what the container loses
# (packages, the account itself, sudo, locale) and then runs the user-side
# setup, which skips anything already present.
set -euo pipefail

SETUP=/workspace/.agent-setup
[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }
[ -f "$SETUP/pod.env" ] || { echo "Missing $SETUP/pod.env; run scripts/pod-setup.sh from the laptop." >&2; exit 1; }
# shellcheck disable=SC1091
. "$SETUP/pod.env"
USER_ROOT="/workspace/$USER_NAME"
HOME_DIR="$USER_ROOT/home"

if ! mountpoint -q /workspace; then
    echo "WARNING: /workspace is not a mounted volume, so nothing here survives a pod stop." >&2
    echo "         Add a volume (pod volume or network volume at /workspace) for persistence." >&2
fi

echo "=== [1/4] system packages ==="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq tmux git gh curl ca-certificates rsync jq ripgrep locales less vim nano sudo python3 >/dev/null
locale-gen en_US.UTF-8 >/dev/null 2>&1 || true
update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 >/dev/null 2>&1 || true

echo "=== [2/4] account '$USER_NAME' with home on the volume ==="
if ! id "$USER_NAME" >/dev/null 2>&1; then
    if [ -d "$HOME_DIR" ]; then
        # Reuse the home's owner so files written before the restart stay ours.
        uid="$(stat -c %u "$HOME_DIR")"
        gid="$(stat -c %g "$HOME_DIR")"
    else
        uid=1001
        while getent passwd "$uid" >/dev/null || getent group "$uid" >/dev/null; do uid=$((uid + 1)); done
        gid="$uid"
    fi
    getent group "$gid" >/dev/null || groupadd -g "$gid" "$USER_NAME"
    useradd -u "$uid" -g "$gid" -d "$HOME_DIR" -M -s /bin/bash "$USER_NAME"
fi
echo "$USER_NAME ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-$USER_NAME"
chmod 440 "/etc/sudoers.d/90-$USER_NAME"

# Runpod volumes are NFS: root can neither chown there nor enter the user's
# private files, while files the user creates are the user's. So the user
# creates its own directories, and the setup bundle stays world-readable.
if [ -e "$USER_ROOT" ] && [ "$(stat -c %u "$USER_ROOT")" != "$(id -u "$USER_NAME")" ]; then
    echo "ERROR: $USER_ROOT exists but is not owned by $USER_NAME; move it aside and rerun." >&2
    exit 1
fi
su "$USER_NAME" -s /bin/bash -c "mkdir -p '$HOME_DIR' && chmod 755 '$USER_ROOT' '$HOME_DIR'"
chmod -R u=rwX,go=rX "$SETUP"

echo "=== [3/4] agents, profile, and project (as $USER_NAME) ==="
su - "$USER_NAME" -c "bash $SETUP/agents/pod/user-setup.sh"

echo "=== [4/4] restart script ==="
cat > /workspace/bootstrap.sh <<EOF
#!/usr/bin/env bash
# Rebuild this pod's container side after a Runpod stop and start. Run as root.
exec bash $SETUP/agents/pod/bootstrap.sh
EOF
chmod 755 /workspace/bootstrap.sh

host="${RUNPOD_PUBLIC_IP:-}" port="${RUNPOD_TCP_PORT_22:-}"
if [ -f /etc/rp_environment ] && { [ -z "$host" ] || [ -z "$port" ]; }; then
    # shellcheck disable=SC1091
    host="$(. /etc/rp_environment; echo "${RUNPOD_PUBLIC_IP:-}")"
    # shellcheck disable=SC1091
    port="$(. /etc/rp_environment; echo "${RUNPOD_TCP_PORT_22:-}")"
fi
cat <<EOF

Setup complete. From the laptop:

    ssh $USER_NAME@${host:-<pod-ip>} -p ${port:-<port>}
    tmux new -A -s $TMUX_SESSION

First time only, inside tmux: run \`claude\` and finish /login, then
\`codex login --device-auth\`. Both logins persist on the volume.

Then start agents from the project directory (they run without approval
prompts on this dedicated pod; plain \`claude\` and \`codex\` still ask):

    claude-pod        # or: codex-pod

Detach with Ctrl-b d and close the laptop; reattach with the two commands above.
After a pod stop and start, SSH in as root and run: bash /workspace/bootstrap.sh
EOF
