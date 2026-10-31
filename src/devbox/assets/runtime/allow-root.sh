#!/bin/sh
# Sent by the host to a root exec before any agent process starts. The policy
# lives on the instance's writable layer, not in an image or a host bind mount.
set -eu
directory=$1
install -d -m 0755 "$directory"
policy=$(mktemp "$directory/devbox-agent.XXXXXX")
trap 'rm -f "$policy"' EXIT
printf '%s\n' 'agent ALL=(ALL:ALL) NOPASSWD: ALL' > "$policy"
chmod 0440 "$policy"
visudo -cf "$policy" >/dev/null
mv -f "$policy" "$directory/devbox-agent"
