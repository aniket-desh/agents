#!/usr/bin/env bash
set -euo pipefail
[[ "$(id -u)" -ne 0 && -f /.dockerenv ]] || { echo "A nonroot Docker workspace is required." >&2; exit 1; }
[[ "$(cat /etc/research-workspace)" == research-agents-isolated-v1 ]] || { echo "The workspace image marker is missing." >&2; exit 1; }
[[ "$HOME" == /home/research && -w "$HOME" && -w /workspace ]] || { echo "Persistent volumes must be writable by the image's research UID." >&2; exit 1; }
umask 077
if [[ ! -f "$HOME/.research-profile-ready" ]]; then
    /opt/research-agents/install.sh --agents codex claude pi
    touch "$HOME/.research-profile-ready"
fi
touch /tmp/research-ready
exec sleep infinity
