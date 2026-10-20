#!/bin/sh
# Cargo's target runner signs the rebuilt binary before replacing this process.
# exec preserves Tauri's normal quit/rebuild process supervision.
"$PERMAFROST_DEV_NODE" "$PERMAFROST_DEV_SIGN_SCRIPT" "$1" || exit $?
exec "$@"
