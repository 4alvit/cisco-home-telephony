#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
site=.local/site.json
mode=local
with_tftp=0
firewall_reviewed=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --site) site=$2; shift 2 ;;
    --apply) mode=apply; shift ;;
    --server-dry-run) mode=server; shift ;;
    --with-tftp) with_tftp=1; shift ;;
    --firewall-reviewed) firewall_reviewed=1; shift ;;
    *) echo "Usage: $0 [--site FILE] [--server-dry-run | --apply --firewall-reviewed] [--with-tftp]" >&2; exit 2 ;;
  esac
done
args=(--site "$site")
if [[ "$with_tftp" == 1 ]]; then args+=(--with-tftp); fi
python3 scripts/preflight.py "${args[@]}"
python3 scripts/render_k3s.py "${args[@]}"
if [[ "$mode" == local ]]; then
  echo 'Local dry run complete. No cluster calls. Review private manifests; docs describe --server-dry-run / --apply.'
  exit 0
fi
python3 scripts/preflight.py "${args[@]}" --cluster
context=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["kube_context"])' "$site")
namespace=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["namespace"])' "$site")
rendered=$(python3 -c 'import pathlib,sys;print(pathlib.Path(sys.argv[1]).resolve().parent / "rendered")' "$site")
if [[ "$mode" == apply && "$firewall_reviewed" != 1 ]]; then
  echo 'Before apply, follow host firewall/socket checks and pass --firewall-reviewed. hostNetwork bypasses normal pod isolation.' >&2
  exit 2
fi
kube=(kubectl --context "$context" --request-timeout=30s)
if [[ "$mode" == server ]]; then
  # For a fresh namespace, Kubernetes cannot dry-run namespaced children until the namespace exists.
  if ! "${kube[@]}" get namespace "$namespace" >/dev/null 2>&1; then
    echo 'Server dry run needs an existing namespace. Local render passed. Review namespace labels, then create the namespace explicitly per docs.' >&2
    exit 2
  fi
  "${kube[@]}" apply --server-side --field-manager=cisco-home-telephony --dry-run=server -f "$rendered/core.yaml"
  if [[ "$with_tftp" == 1 ]]; then "${kube[@]}" apply --server-side --field-manager=cisco-home-telephony --dry-run=server -f "$rendered/tftp.yaml"; fi
  exit 0
fi
"${kube[@]}" apply --server-side --field-manager=cisco-home-telephony -f "$rendered/core.yaml"
"${kube[@]}" -n "$namespace" rollout status deployment/asterisk --timeout=180s
if [[ "$with_tftp" == 1 ]]; then
  # A completed/failed Job must be explicitly deleted before the next maintenance window.
  "${kube[@]}" apply --server-side --field-manager=cisco-home-telephony -f "$rendered/tftp.yaml"
  "${kube[@]}" -n "$namespace" wait --for=condition=Ready pod -l app=cisco-tftp --timeout=120s
  echo 'TFTP active for at most one hour. Delete job/cisco-tftp after provisioning; do not interrupt an active firmware flash.'
fi
