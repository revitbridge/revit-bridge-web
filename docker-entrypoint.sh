#!/bin/sh
set -eu

# Slot token files arrive as root-owned bind mounts. Copy each configured one
# into a directory only appuser can read, point the variable at the copy, then
# drop privileges. The token never enters the image or the process environment.
runtime_dir="/run/revit-bridge-web-secrets"
install -d -o appuser -g appuser -m 0700 "$runtime_dir"

slot="1"
max_slots="${MAX_SLOTS:-5}"
while [ "$slot" -le "$max_slots" ]; do
    eval "source_file=\${MCP_BRIDGE_SLOT_TOKEN_FILE_$slot:-}"
    if [ -n "$source_file" ]; then
        if [ ! -s "$source_file" ]; then
            echo "Slot token file for slot $slot is missing or empty: $source_file" >&2
            exit 1
        fi
        runtime_file="$runtime_dir/slot-$slot.token"
        install -o appuser -g appuser -m 0400 "$source_file" "$runtime_file"
        eval "export MCP_BRIDGE_SLOT_TOKEN_FILE_$slot=\$runtime_file"
    fi
    slot=$((slot + 1))
done

# The data volume may be created root-owned by Docker on first start; the app
# runs as appuser and must write it. Nothing is copied into it: this host
# keeps skills and interaction logs under DATA_DIR, and the revit-bridge
# package creates capabilities/ and evidence/ under REVIT_BRIDGE_DATA_DIR
# itself when it first writes there. Both default to /app/data.
for data_dir in "${DATA_DIR:-/app/data}" "${REVIT_BRIDGE_DATA_DIR:-/app/data}"; do
    if [ -d "$data_dir" ]; then
        chown -R appuser:appuser "$data_dir"
        chmod -R u+rwX "$data_dir"
    fi
done

exec setpriv --reuid=appuser --regid=appuser --init-groups "$@"
