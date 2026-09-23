#!/bin/sh
set -eu

# Nothing to install for the add-ins any more: a device's token is created when
# its pairing code is redeemed and only its hash is stored, in the data volume.
#
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
