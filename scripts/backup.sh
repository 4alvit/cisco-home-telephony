#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# -ne 2 ]]; then echo "Usage: $0 AGE_PUBLIC_RECIPIENT OUTPUT.age" >&2; exit 2; fi
recipient=$1
output=$2
[[ -d .local ]] || { echo 'No .local directory' >&2; exit 1; }
[[ ! -e "$output" ]] || { echo 'Output already exists' >&2; exit 1; }
command -v age >/dev/null
umask 077
tar -czf - .local | age -r "$recipient" -o "$output"
printf 'Encrypted backup written: %s\n' "$output"
