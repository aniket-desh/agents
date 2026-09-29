#!/usr/bin/env bash
# Explicit one-time Linux service deployment. Default: copy/configure, do not start.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
config=""
client_user=""
provider_env=""
stage_root=""
enable=0
replace_config=0
usage() {
    cat <<'USAGE'
Usage: sudo scripts/install-controller.sh --config FILE --client-user USER [--provider-env FILE] [--replace-config] [--enable]
       scripts/install-controller.sh --config FILE --stage-root DIRECTORY

Copies a protected standalone runtime, configures the service identity and socket
group, and stages systemd units. --enable explicitly enables and starts services.
--stage-root only writes a test directory; it never changes users or services.
USAGE
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config|--client-user|--provider-env|--stage-root)
            [[ $# -ge 2 ]] || { usage >&2; exit 2; }
            case "$1" in
                --config) config="$2" ;;
                --client-user) client_user="$2" ;;
                --provider-env) provider_env="$2" ;;
                --stage-root) stage_root="$2" ;;
            esac
            shift 2 ;;
        --enable) enable=1; shift ;;
        --replace-config) replace_config=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
[[ -n "$config" && -f "$config" ]] || { echo "A readable --config file is required." >&2; exit 2; }
extra=()
[[ -z "$provider_env" ]] || extra+=(--provider-env "$provider_env")
[[ "$replace_config" -eq 0 ]] || extra+=(--replace-config)
if [[ -n "$stage_root" ]]; then
    [[ "$enable" -eq 0 ]] || { echo "--enable cannot be used with --stage-root." >&2; exit 2; }
    exec python3 "$script_dir/install.py" --controller-stage "$stage_root" --config "$config" "${extra[@]}"
fi
[[ "$(uname -s)" == Linux && "$EUID" -eq 0 ]] || { echo "Actual deployment requires Linux and root; use --stage-root for inspection." >&2; exit 2; }
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
[[ -n "$client_user" && "$client_user" != research-compute && "$client_user" != root ]] || { echo "Provide an ordinary research user with --client-user." >&2; exit 2; }
id "$client_user" >/dev/null
command -v systemctl >/dev/null
if [[ "$enable" -eq 1 ]]; then
    python3 "$script_dir/install.py" --check-controller-runtime --config "$config"
fi
for directory in /opt /opt/research-agents /etc/research-compute /var/lib/research-compute; do
    [[ ! -L "$directory" ]] || { echo "Refusing symlinked system path: $directory" >&2; exit 2; }
done
if systemctl is-active --quiet research-compute.service || systemctl is-active --quiet research-compute-watchdog.service; then
    echo "Controller services are active. Pause experiments and stop both services before a deliberate upgrade." >&2
    exit 2
fi
getent group research-compute >/dev/null || groupadd --system research-compute
getent group research-compute-clients >/dev/null || groupadd --system research-compute-clients
if ! id research-compute >/dev/null 2>&1; then
    useradd --system --gid research-compute --home-dir /var/lib/research-compute --no-create-home --shell /usr/sbin/nologin research-compute
else
    service_shell="$(getent passwd research-compute | cut -d: -f7)"
    [[ "$(id -u research-compute)" != 0 && ( "$service_shell" == */nologin || "$service_shell" == */false ) ]] || {
        echo "Existing research-compute account is not a dedicated non-login service identity." >&2
        exit 2
    }
fi
usermod --append --groups research-compute-clients "$client_user"
install -d -o root -g research-compute -m 0750 /etc/research-compute
install -d -o research-compute -g research-compute -m 0700 /var/lib/research-compute
python3 "$script_dir/install.py" --controller-stage / --config "$config" "${extra[@]}"
chown -R root:root /opt/research-agents
chmod -R go-w /opt/research-agents
chown root:research-compute /etc/research-compute /etc/research-compute/config.json
chmod 0640 /etc/research-compute/config.json
if [[ -f /etc/research-compute/runpod.env ]]; then
    chown research-compute:research-compute /etc/research-compute/runpod.env
    chmod 0600 /etc/research-compute/runpod.env
fi
systemctl daemon-reload
if [[ "$enable" -eq 1 ]]; then
    systemctl enable --now research-compute.service research-compute-watchdog.service
fi
echo "Controller installed. Reconnect the research user's login to activate socket-group access."
[[ "$enable" -eq 1 ]] || echo "Services are stopped; --enable is required to start them during installation."
