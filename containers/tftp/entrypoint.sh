#!/bin/sh
set -eu

# Existing standalone image use retains the original daemon invocation.
if [ -z "${CISCO_TFTP_LOCK:-}" ]; then
    exec /usr/sbin/in.tftpd "$@"
fi

exec python3 -I /usr/local/lib/cisco-tftp/runtime.py "$@"
