#!/usr/bin/env bash
# The old curl-pipe pod bootstrap installed a single agent without the research
# profile, skills, or personal instructions. It is replaced by a command run on
# the laptop, which carries those private pieces to the pod over SSH:
#
#   scripts/pod-setup.sh 'ssh root@IP -p PORT' --repo URL [--branch BRANCH]
#
# After a pod restart, run `bash /workspace/bootstrap.sh` on the pod as root.
echo "setup.sh is retired; run scripts/pod-setup.sh from your laptop instead (see README)." >&2
exit 1
